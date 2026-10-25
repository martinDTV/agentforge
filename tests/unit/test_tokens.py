"""OIDC token verification: pinned algorithms, issuer/audience/expiry, claim mapping."""

import json
import time
from pathlib import Path

import jwt
import pytest

from app.security import AuthenticationError, ClaimMapping, JwtVerifier, VerificationRules
from app.security.dev_keys import DevIssuer
from app.security.tokens import DisabledAuthentication, JwksFileKeys, StaticKeys


@pytest.fixture
def issuer() -> DevIssuer:
    return DevIssuer()


def verifier_for(
    issuer: DevIssuer, mapping: ClaimMapping | None = None, **rules: object
) -> JwtVerifier:
    return JwtVerifier(
        StaticKeys(issuer.jwks),
        VerificationRules(issuer=issuer.issuer, audience=issuer.audience, **rules),  # type: ignore[arg-type]
        mapping,
    )


def test_valid_token_yields_a_principal(issuer: DevIssuer):
    token = issuer.token("user-1", roles=["operator", "approver"], projects=["demo"])
    principal = verifier_for(issuer).verify(token)
    assert principal.subject == "user-1" and principal.kind == "user"
    assert principal.roles == {"operator", "approver"} and principal.projects == {"demo"}
    assert principal.issuer == issuer.issuer
    assert principal.expires_at is not None and principal.expires_at.tzinfo is not None
    assert principal.token_id and principal.has_role("approver")


def test_service_principals_come_from_the_kind_claim(issuer: DevIssuer):
    assert verifier_for(issuer).verify(issuer.token("svc", kind="service")).kind == "service"
    assert verifier_for(issuer).verify(issuer.token("u", kind="anything")).kind == "user"


def test_expired_tokens_are_rejected_but_leeway_is_honoured(issuer: DevIssuer):
    expired = issuer.token("u", ttl_seconds=60, issued_at=int(time.time()) - 120)
    with pytest.raises(AuthenticationError) as info:
        verifier_for(issuer, leeway_seconds=0).verify(expired)
    assert info.value.reason == "expired_token"
    recently = issuer.token("u", ttl_seconds=60, issued_at=int(time.time()) - 70)
    assert verifier_for(issuer, leeway_seconds=30).verify(recently).subject == "u"


@pytest.mark.parametrize(
    "mutation",
    [
        {"audience": "someone-else"},
        {"issuer": "https://evil.example"},
        {"algorithm": "RS512"},
    ],
    ids=["audience", "issuer", "algorithm"],
)
def test_wrong_audience_issuer_or_algorithm_is_invalid(issuer: DevIssuer, mutation: dict[str, str]):
    with pytest.raises(AuthenticationError) as info:
        verifier_for(issuer).verify(issuer.token("u", **mutation))  # type: ignore[arg-type]
    assert info.value.reason == "invalid_token"


def test_tokens_from_an_unknown_key_are_invalid(issuer: DevIssuer):
    other = DevIssuer(issuer=issuer.issuer, audience=issuer.audience)
    with pytest.raises(AuthenticationError) as info:
        verifier_for(issuer).verify(other.token("u"))
    assert info.value.reason == "invalid_token"


def test_symmetric_and_none_algorithms_are_never_accepted(issuer: DevIssuer):
    claims = {
        "sub": "u",
        "iss": issuer.issuer,
        "aud": issuer.audience,
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
    }
    none_token = jwt.encode(claims, key="", algorithm="none", headers={"kid": issuer.kid})
    hs_token = jwt.encode(claims, key="s" * 48, algorithm="HS256", headers={"kid": issuer.kid})
    for token in (none_token, hs_token):
        with pytest.raises(AuthenticationError) as info:
            verifier_for(issuer).verify(token)
        assert info.value.reason == "invalid_token"


def test_tokens_without_required_claims_are_invalid(issuer: DevIssuer):
    bare = jwt.encode(
        {"sub": "u", "iss": issuer.issuer, "aud": issuer.audience},
        issuer._key,
        algorithm="RS256",
        headers={"kid": issuer.kid},
    )
    with pytest.raises(AuthenticationError) as info:
        verifier_for(issuer).verify(bare)
    assert info.value.reason == "invalid_token"


def test_garbage_and_missing_kid_are_invalid(issuer: DevIssuer):
    for token in ("not-a-jwt", "a.b.c", ""):
        with pytest.raises(AuthenticationError):
            verifier_for(issuer).verify(token)


def test_roles_and_projects_accept_nested_paths_and_space_separated_strings(issuer: DevIssuer):
    mapping = ClaimMapping(roles="realm_access.roles", projects="scope")
    token = issuer.token(
        "u", extra={"realm_access": {"roles": ["admin", "viewer"]}, "scope": "demo billing"}
    )
    principal = verifier_for(issuer, mapping).verify(token)
    assert principal.roles == {"admin", "viewer"} and principal.projects == {"demo", "billing"}
    assert (
        verifier_for(issuer, ClaimMapping(roles="absent")).verify(issuer.token("u")).roles == set()
    )


def test_key_rotation_keeps_both_keys_valid(issuer: DevIssuer):
    new = DevIssuer(issuer=issuer.issuer, audience=issuer.audience)
    merged = {"keys": issuer.jwks["keys"] + new.jwks["keys"]}
    verifier = JwtVerifier(
        StaticKeys(merged), VerificationRules(issuer=issuer.issuer, audience=issuer.audience)
    )
    assert verifier.verify(issuer.token("old")).subject == "old"
    assert verifier.verify(new.token("new")).subject == "new"


def test_errors_never_echo_the_token(issuer: DevIssuer):
    token = issuer.token("u", audience="wrong")
    with pytest.raises(AuthenticationError) as info:
        verifier_for(issuer).verify(token)
    assert token not in str(info.value) and token[:20] not in repr(info.value)


def test_jwks_file_is_reloaded_when_rotated(issuer: DevIssuer, tmp_path: Path):
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(issuer.jwks))
    verifier = JwtVerifier(
        JwksFileKeys(path), VerificationRules(issuer=issuer.issuer, audience=issuer.audience)
    )
    assert verifier.verify(issuer.token("u")).subject == "u"
    rotated = DevIssuer(issuer=issuer.issuer, audience=issuer.audience)
    time.sleep(0.01)
    path.write_text(json.dumps(rotated.jwks))
    import os

    os.utime(path, None)
    assert verifier.verify(rotated.token("v")).subject == "v"
    with pytest.raises(AuthenticationError):
        verifier.verify(issuer.token("u"))


def test_dev_issuer_roundtrips_through_disk(issuer: DevIssuer, tmp_path: Path):
    jwks_path, private_path = issuer.write(tmp_path)
    assert jwks_path.exists() and private_path.exists()
    assert oct(private_path.stat().st_mode)[-3:] == "600"
    loaded = DevIssuer.load(tmp_path, issuer=issuer.issuer, audience=issuer.audience)
    assert verifier_for(issuer).verify(loaded.token("disk")).subject == "disk"


def test_disabled_authentication_is_the_local_admin():
    principal = DisabledAuthentication().verify("ignored")
    assert principal.subject == "local-dev" and "platform_admin" in principal.roles

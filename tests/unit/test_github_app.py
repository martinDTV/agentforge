"""GitHub App credentials, installation tokens and identity checks against a
simulated GitHub API. No network, no real App."""

import logging
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import pytest

from app.git.remote import RemoteSpec
from app.infrastructure.github import (
    PULLS_READ_PERMISSIONS,
    READ_ONLY_PERMISSIONS,
    GitHubAppCredentials,
    GitHubClient,
    GitHubError,
    InstallationTokenProvider,
    RepositoryIdentity,
    verify_identity,
)
from app.infrastructure.github.credentials import JWT_TTL_SECONDS
from tests.unit.github_fixtures import (
    APP_ID,
    INSTALLATION_ID,
    NAME,
    OWNER,
    REPOSITORY_ID,
    FakeGitHub,
    app_key_pair,
    repository_payload,
)

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def setup(clock: Clock | None = None):
    clock = clock or Clock()
    fake = FakeGitHub(clock=clock)
    client = GitHubClient(
        "https://api.github.test", transport=fake.transport(), sleep=lambda s: None
    )
    credentials = GitHubAppCredentials(APP_ID, fake.private_key, clock=clock)
    provider = InstallationTokenProvider(client, credentials, clock=clock)
    return fake, client, credentials, provider, clock


def spec(**overrides) -> RemoteSpec:
    base = dict(
        owner=OWNER,
        name=NAME,
        github_repository_id=REPOSITORY_ID,
        installation_id=INSTALLATION_ID,
        default_branch="main",
    )
    return RemoteSpec(**{**base, **overrides})


def test_the_app_jwt_is_short_lived_and_signed_by_the_app_key():
    fake, _client, credentials, _provider, clock = setup()
    token = credentials.app_jwt()
    claims = jwt.decode(
        token,
        fake.public_key,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},  # fixed test clock
    )
    now = int(clock().timestamp())
    assert claims["iss"] == str(APP_ID)
    assert claims["iat"] == now - 60  # clock skew allowance, as GitHub recommends
    assert claims["exp"] == now + JWT_TTL_SECONDS and JWT_TTL_SECONDS <= 9 * 60
    assert claims["exp"] - claims["iat"] <= 600  # GitHub's hard limit
    _private, other_public = app_key_pair()
    assert other_public == fake.public_key  # same session key; a different key would fail
    with pytest.raises(jwt.PyJWTError):
        jwt.decode(
            token + "x",
            fake.public_key,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )


def test_installation_tokens_are_narrowed_cached_and_renewed_before_expiry():
    fake, _client, _credentials, provider, clock = setup()
    token = provider.token_for(INSTALLATION_ID, NAME)
    assert token.repository == NAME and token.permissions == dict(READ_ONLY_PERMISSIONS)
    assert token.expires_at == clock() + timedelta(hours=1)
    mint = fake.requests[0]
    assert (
        mint.method == "POST"
        and mint.url.path == f"/app/installations/{INSTALLATION_ID}/access_tokens"
    )
    import json

    assert json.loads(mint.read()) == {
        "repositories": [NAME],
        "permissions": {"contents": "read", "metadata": "read"},
    }
    assert provider.token_for(INSTALLATION_ID, NAME) is token and provider.minted == 1  # cached
    clock.advance(minutes=30)
    assert provider.token_for(INSTALLATION_ID, NAME) is token  # still fresh
    clock.advance(minutes=20)  # 50 min: past 80% of the hour
    renewed = provider.token_for(INSTALLATION_ID, NAME)
    assert renewed is not token and provider.minted == 2
    provider.invalidate(renewed)
    assert provider.token_for(INSTALLATION_ID, NAME) is not renewed and provider.minted == 3
    # Another repository or permission set never shares a token.
    fake.installation_repositories["other"] = repository_payload(github_id=1, name="other")
    other = provider.token_for(INSTALLATION_ID, "other")
    assert other.reveal() != provider.token_for(INSTALLATION_ID, NAME).reveal()


def test_write_permissions_never_reach_github():
    fake, _client, _credentials, provider, _clock = setup()
    with pytest.raises(GitHubError) as exc:
        provider.token_for(INSTALLATION_ID, NAME, permissions={"contents": "write"})
    assert exc.value.code == "permission_not_allowed"
    with pytest.raises(GitHubError) as exc:
        provider.token_for(INSTALLATION_ID, NAME, permissions={"pull_requests": "write"})
    assert exc.value.code == "permission_not_allowed"
    assert fake.requests == []  # refused locally, before any HTTP request
    # Reading pull requests (integration evidence for goals, ADR 0012) is allowed without
    # writes, but the installation itself must grant it: the fake does not here.
    with pytest.raises(GitHubError) as exc:
        provider.token_for(INSTALLATION_ID, NAME, permissions={"pull_requests": "read"})
    assert exc.value.code == "token_request_rejected"


def test_github_failures_map_to_stable_codes_without_payloads():
    fake, client, credentials, provider, _clock = setup()
    fake.reject_app_jwt = True
    with pytest.raises(GitHubError) as exc:
        provider.token_for(INSTALLATION_ID, NAME)
    assert exc.value.code == "app_auth_failed" and "simulated" not in str(exc.value)
    fake.reject_app_jwt = False
    with pytest.raises(GitHubError) as exc:
        provider.token_for(INSTALLATION_ID + 1, NAME)
    assert exc.value.code == "installation_not_found"
    with pytest.raises(GitHubError) as exc:  # repository outside the installation: 422
        provider.token_for(INSTALLATION_ID, "not-installed")
    assert exc.value.code == "token_request_rejected" and not exc.value.retryable
    original = fake.handler

    def no_permissions(request: httpx.Request) -> httpx.Response:
        # The App was created without repository permissions (real case, 2026-10-26).
        if request.method == "POST" and "/access_tokens" in request.url.path:
            return httpx.Response(
                422,
                json={"message": "The permissions requested are not granted to this installation."},
            )
        return original(request)

    strict = GitHubClient("https://api.github.test", transport=httpx.MockTransport(no_permissions))
    with pytest.raises(GitHubError) as exc:
        strict.create_installation_token(
            credentials.app_jwt(),
            INSTALLATION_ID,
            repositories=[NAME],
            permissions=dict(READ_ONLY_PERMISSIONS),
        )
    assert exc.value.code == "token_request_rejected" and "granted" not in str(exc.value)
    token = provider.token_for(INSTALLATION_ID, NAME)
    fake.revoked.add(token.reveal())
    with pytest.raises(GitHubError) as exc:
        client.repository(token.reveal(), OWNER, NAME)
    assert exc.value.code == "token_invalid"
    fake.revoked.clear()
    fake.fail_next = [403]
    with pytest.raises(GitHubError) as exc:
        client.repository(token.reveal(), OWNER, NAME)
    assert exc.value.code == "installation_forbidden"
    # One retry on 5xx, then success; two 5xx in a row surface as retryable failure.
    fake.fail_next = [502]
    assert client.repository(token.reveal(), OWNER, NAME).github_id == REPOSITORY_ID
    fake.fail_next = [500, 503]
    with pytest.raises(GitHubError) as exc:
        client.repository(token.reveal(), OWNER, NAME)
    assert exc.value.code == "github_failed" and exc.value.retryable
    with pytest.raises(GitHubError) as exc:
        client.repository(token.reveal(), OWNER, "missing")
    assert exc.value.code == "repository_not_found"

    def unreachable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    offline = GitHubClient(
        "https://api.github.test", transport=httpx.MockTransport(unreachable), sleep=lambda s: None
    )
    with pytest.raises(GitHubError) as exc:
        offline.repository(token.reveal(), OWNER, NAME)
    assert exc.value.code == "github_unreachable" and exc.value.retryable


def test_tokens_granted_with_more_than_requested_are_refused():
    fake, client, credentials, _provider, _clock = setup()

    def generous(request: httpx.Request) -> httpx.Response:
        response = fake.handler(request)
        if request.method == "POST":
            body = response.json()
            body["permissions"] = {"contents": "write", "metadata": "read"}
            return httpx.Response(201, json=body)
        return response

    client = GitHubClient("https://api.github.test", transport=httpx.MockTransport(generous))
    with pytest.raises(GitHubError) as exc:
        client.create_installation_token(
            credentials.app_jwt(),
            INSTALLATION_ID,
            repositories=[NAME],
            permissions=dict(READ_ONLY_PERMISSIONS),
        )
    assert exc.value.code == "permission_not_allowed"


def test_repository_identity_and_installation_listing_are_parsed():
    fake, client, _credentials, provider, _clock = setup()
    token = provider.token_for(INSTALLATION_ID, NAME)
    identity = client.repository(token.reveal(), OWNER, NAME)
    assert identity == RepositoryIdentity(
        github_id=REPOSITORY_ID,
        owner=OWNER,
        name=NAME,
        full_name=f"{OWNER}/{NAME}",
        default_branch="main",
        private=True,
        archived=False,
        node_id=f"R_{REPOSITORY_ID}",
    )
    assert [r.github_id for r in client.installation_repositories(token.reveal())] == [
        REPOSITORY_ID
    ]
    broken = GitHubClient(
        "https://api.github.test",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"id": "x"})),
    )
    with pytest.raises(GitHubError) as exc:
        broken.repository("t", OWNER, NAME)
    assert exc.value.code == "github_protocol_error"


def test_identity_verification_refuses_substitution_transfer_and_branch_changes():
    ok = RepositoryIdentity(
        github_id=REPOSITORY_ID,
        owner=OWNER,
        name=NAME,
        full_name=f"{OWNER}/{NAME}",
        default_branch="main",
    )
    verify_identity(spec(), ok, installation_repository_ids=[REPOSITORY_ID])
    verify_identity(spec(), ok.model_copy(update={"full_name": f"{OWNER.upper()}/{NAME}"}))
    cases = {
        "repository_substituted": ok.model_copy(update={"github_id": REPOSITORY_ID + 1}),
        "repository_transferred": ok.model_copy(update={"full_name": f"someone-else/{NAME}"}),
        "repository_identity_mismatch": ok.model_copy(
            update={"github_id": 7, "full_name": "someone-else/other"}
        ),
        "default_branch_changed": ok.model_copy(update={"default_branch": "develop"}),
        "repository_archived": ok.model_copy(update={"archived": True}),
    }
    for code, observed in cases.items():
        with pytest.raises(GitHubError) as exc:
            verify_identity(spec(), observed)
        assert exc.value.code == code, code
    with pytest.raises(GitHubError) as exc:
        verify_identity(spec(), ok, installation_repository_ids=[1, 2])
    assert exc.value.code == "repository_not_in_installation"


def test_remote_spec_validates_names_and_derives_the_only_clone_url():
    remote = spec()
    assert remote.full_name == f"{OWNER}/{NAME}"
    assert remote.clone_url == f"https://github.com/{OWNER}/{NAME}.git"
    for bad in ({"name": "demo.git"}, {"name": ".hidden"}, {"name": ".."}, {"owner": "-x"}):
        with pytest.raises(ValueError):
            spec(**bad)
    for bad in ({"github_repository_id": 0}, {"installation_id": -1}, {"provider": "gitlab"}):
        with pytest.raises(ValueError):
            spec(**bad)
    with pytest.raises(ValueError):
        spec(default_branch="refs/heads/main")
    with pytest.raises(ValueError):
        RemoteSpec.model_validate({**spec().model_dump(), "clone_url": "https://evil"})


def test_credentials_come_only_from_a_private_key_file_with_mode_0600(tmp_path: Path):
    from app.settings import Settings

    private, _public = app_key_pair()
    key = tmp_path / "app.pem"
    key.write_bytes(private)
    os.chmod(key, 0o644)

    class Config:
        github_app_id: int | None = APP_ID
        github_app_private_key_file: Path | None = key

    with pytest.raises(GitHubError) as exc:
        GitHubAppCredentials.from_settings(Config())  # type: ignore[arg-type]
    assert exc.value.code == "github_unconfigured"
    os.chmod(key, 0o600)
    credentials = GitHubAppCredentials.from_settings(Config())  # type: ignore[arg-type]
    assert credentials is not None and credentials.app_id == APP_ID
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    Config.github_app_id = None
    assert GitHubAppCredentials.from_settings(Config()) is None  # type: ignore[arg-type]
    with pytest.raises(GitHubError):
        GitHubAppCredentials(0, private)
    assert isinstance(Settings.model_fields["github_app_private_key_file"].default, type(None))


def test_tokens_never_appear_in_repr_str_or_logs(caplog: pytest.LogCaptureFixture):
    fake, _client, _credentials, provider, _clock = setup()
    with caplog.at_level(logging.DEBUG):
        token = provider.token_for(INSTALLATION_ID, NAME)
    secret = token.reveal()
    assert secret.startswith("ghs_") and secret in fake.tokens
    for text in (repr(token), str(token), repr(provider), f"{token!r}", caplog.text):
        assert secret not in text
    assert "InstallationToken(" in repr(token) and str(INSTALLATION_ID) in repr(token)


def test_installation_details_and_branch_heads_are_read_as_the_app_and_the_installation():
    fake, client, credentials, provider, _clock = setup()
    info = client.installation(credentials.app_jwt(), INSTALLATION_ID)
    assert (info.id, info.app_id, info.account) == (INSTALLATION_ID, APP_ID, OWNER)
    assert info.repository_selection == "selected" and not info.suspended
    assert info.permissions == {"contents": "read", "metadata": "read"}
    with pytest.raises(GitHubError) as exc:
        client.installation(credentials.app_jwt(), INSTALLATION_ID + 1)
    assert exc.value.code == "installation_not_found"
    with pytest.raises(GitHubError) as exc:
        client.installation("not-a-jwt", INSTALLATION_ID)
    assert exc.value.code == "app_auth_failed"
    token = provider.token_for(INSTALLATION_ID, NAME)
    fake.branch_heads = {"main": "a" * 40}
    assert client.branch_head(token.reveal(), OWNER, NAME, "main") == "a" * 40
    with pytest.raises(GitHubError) as exc:
        client.branch_head(token.reveal(), OWNER, NAME, "missing")
    assert exc.value.code == "repository_not_found"
    fake.branch_heads = {"main": "not-a-sha"}
    with pytest.raises(GitHubError) as exc:
        client.branch_head(token.reveal(), OWNER, NAME, "main")
    assert exc.value.code == "github_protocol_error"


def test_pull_request_merge_state_is_read_with_a_read_only_token():
    """Integration evidence for goals (ADR 0012): ``merged_at`` and ``merge_commit_sha`` come
    from GitHub; with a squash merge the merge commit differs from the head."""
    fake, client, _credentials, provider, _clock = setup()
    fake.app_permissions = {"contents": "read", "metadata": "read", "pull_requests": "read"}
    fake.branches["agentforge/run"] = "a" * 40
    fake.pulls[1] = {
        "number": 1,
        "html_url": "https://github.test/pull/1",
        "state": "open",
        "draft": True,
        "head": {"ref": "agentforge/run", "sha": "a" * 40},
        "base": {"ref": "main"},
    }
    token = provider.token_for(INSTALLATION_ID, NAME, permissions=PULLS_READ_PERMISSIONS)
    assert token.permissions == dict(PULLS_READ_PERMISSIONS)
    pr = client.get_pull_request(token.reveal(), OWNER, NAME, 1)
    assert pr.state == "open" and not pr.merged and pr.merge_commit_sha is None
    fake.merge_pull_request(1, merge_commit_sha="c" * 40, squash=True)
    pr = client.get_pull_request(token.reveal(), OWNER, NAME, 1)
    assert pr.merged and pr.merged_at is not None and pr.merge_commit_sha == "c" * 40
    assert pr.merge_commit_sha != pr.head_sha
    fake.close_pull_request(1)
    pr = client.get_pull_request(token.reveal(), OWNER, NAME, 1)
    assert pr.state == "closed" and not pr.merged
    # Writes are still refused when the provider has no write scopes.
    with pytest.raises(GitHubError) as exc:
        provider.token_for(INSTALLATION_ID, NAME, permissions={"pull_requests": "write"})
    assert exc.value.code == "permission_not_allowed"

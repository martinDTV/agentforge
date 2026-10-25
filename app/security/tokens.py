"""OIDC access-token verification (ADR 0005).

AgentForge never issues credentials. It verifies JWT access tokens from an
identity provider: asymmetric algorithms only (pinned list), issuer, audience,
expiry and issued-at required, small clock leeway. Signing keys come from the
provider's JWKS endpoint (cached) or, outside production, from a static JWKS
file. Any failure becomes ``AuthenticationError`` with a closed reason; the
token and the library message never leave this module.
"""

import json
import logging
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import jwt
from jwt import PyJWK, PyJWKClient, PyJWKSet
from pydantic import BaseModel, ConfigDict, Field

from app.settings import Settings

from .errors import AuthenticationError
from .principal import Principal, PrincipalKind

logger = logging.getLogger(__name__)

REQUIRED_CLAIMS = ("exp", "iat", "sub", "iss", "aud")


class TokenVerifier(Protocol):
    # True only for the local/test "disabled" mode: requests without a token
    # still get a principal. Real verifiers always require one.
    allows_anonymous: bool

    def verify(self, token: str) -> Principal: ...


class SigningKeys(Protocol):
    """Resolve the public key that signed ``token`` (by ``kid``)."""

    def key_for(self, token: str) -> PyJWK: ...


class VerificationRules(BaseModel):
    model_config = ConfigDict(frozen=True)

    issuer: str
    audience: str
    algorithms: tuple[str, ...] = ("RS256", "ES256")
    leeway_seconds: int = Field(default=30, ge=0)


class ClaimMapping(BaseModel):
    """Where roles, projects and the principal kind live in the token.

    Dotted paths walk nested claims (``realm_access.roles``). Values may be a
    list or a space-separated string, as providers differ.
    """

    model_config = ConfigDict(frozen=True)

    roles: str = "roles"
    projects: str = "projects"
    kind: str = "kind"


def _claim(claims: Mapping[str, Any], path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _as_strings(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(part for part in value.split() if part)
    if isinstance(value, list | tuple | set | frozenset):
        return frozenset(str(item) for item in value if str(item))
    return frozenset()


class JwksUrlKeys:
    """Keys from the provider's JWKS endpoint, cached by PyJWT.

    The fetch is blocking HTTP on cache miss: callers run verification off the
    event loop (FastAPI does so for plain ``def`` dependencies).
    """

    def __init__(self, url: str, *, cache_seconds: int = 300, timeout_seconds: float = 5.0):
        self._client = PyJWKClient(
            url,
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=max(1, cache_seconds),
            timeout=timeout_seconds,
        )

    def key_for(self, token: str) -> PyJWK:
        return self._client.get_signing_key_from_jwt(token)


class JwksFileKeys:
    """Keys from a static JWKS document (local development and tests only).

    The file is re-read when it changes so rotating the dev key does not need
    a restart. Settings forbid this source in staging and production.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._loaded_at: float | None = None
        self._keys: PyJWKSet | None = None

    def _load(self) -> PyJWKSet:
        with self._lock:
            mtime = self._path.stat().st_mtime
            if self._keys is None or self._loaded_at != mtime:
                self._keys = PyJWKSet.from_dict(json.loads(self._path.read_text()))
                self._loaded_at = mtime
            return self._keys

    def key_for(self, token: str) -> PyJWK:
        kid = jwt.get_unverified_header(token).get("kid")
        if not kid:
            raise jwt.InvalidTokenError("token sin kid")
        return self._load()[kid]


class StaticKeys:
    """Keys from an in-memory JWKS dict (unit tests)."""

    def __init__(self, jwks: Mapping[str, Any]) -> None:
        self._keys = PyJWKSet.from_dict(dict(jwks))

    def key_for(self, token: str) -> PyJWK:
        kid = jwt.get_unverified_header(token).get("kid")
        if not kid:
            raise jwt.InvalidTokenError("token sin kid")
        return self._keys[kid]


class JwtVerifier:
    allows_anonymous = False

    def __init__(
        self,
        keys: SigningKeys,
        rules: VerificationRules,
        mapping: ClaimMapping | None = None,
    ) -> None:
        self._keys = keys
        self._rules = rules
        self._mapping = mapping or ClaimMapping()

    @classmethod
    def from_settings(cls, settings: Settings) -> "JwtVerifier":
        assert settings.oidc_issuer and settings.oidc_audience  # enforced by Settings
        keys: SigningKeys
        if settings.oidc_jwks_file is not None:
            keys = JwksFileKeys(settings.oidc_jwks_file)
        else:
            assert settings.oidc_jwks_url
            keys = JwksUrlKeys(
                settings.oidc_jwks_url, cache_seconds=settings.oidc_jwks_cache_seconds
            )
        return cls(
            keys,
            VerificationRules(
                issuer=settings.oidc_issuer,
                audience=settings.oidc_audience,
                algorithms=tuple(settings.oidc_algorithms),
                leeway_seconds=settings.oidc_leeway_seconds,
            ),
            ClaimMapping(
                roles=settings.oidc_roles_claim,
                projects=settings.oidc_projects_claim,
                kind=settings.oidc_kind_claim,
            ),
        )

    def verify(self, token: str) -> Principal:
        try:
            key = self._keys.key_for(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=list(self._rules.algorithms),
                audience=self._rules.audience,
                issuer=self._rules.issuer,
                leeway=self._rules.leeway_seconds,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("expired_token") from exc
        except (jwt.PyJWTError, KeyError, ValueError, OSError) as exc:
            # Unknown kid, bad signature, wrong aud/iss/alg, malformed token,
            # unreachable JWKS: all the same to the client.
            logger.info("Token rechazado: %s", type(exc).__name__)
            raise AuthenticationError("invalid_token") from exc
        return self._principal(claims)

    def _principal(self, claims: Mapping[str, Any]) -> Principal:
        kind_claim = _claim(claims, self._mapping.kind)
        kind: PrincipalKind = "service" if kind_claim == "service" else "user"
        exp = claims.get("exp")
        return Principal(
            subject=str(claims["sub"]),
            kind=kind,
            issuer=str(claims["iss"]),
            roles=_as_strings(_claim(claims, self._mapping.roles)),
            projects=_as_strings(_claim(claims, self._mapping.projects)),
            expires_at=datetime.fromtimestamp(float(exp), tz=UTC) if exp is not None else None,
            token_id=str(claims["jti"]) if claims.get("jti") else None,
        )


class DisabledAuthentication:
    """``AUTH_MODE=disabled``: every request is the local administrator.

    Only constructible outside staging/production (Settings refuses otherwise).
    """

    LOCAL_SUBJECT = "local-dev"
    allows_anonymous = True

    def __init__(self, issuer: str = "agentforge-local") -> None:
        self._issuer = issuer

    def verify(self, token: str) -> Principal:
        return self.principal()

    def principal(self) -> Principal:
        return Principal(
            subject=self.LOCAL_SUBJECT,
            kind="user",
            issuer=self._issuer,
            roles=frozenset({"platform_admin"}),
            projects=frozenset({"default"}),
            expires_at=datetime.fromtimestamp(time.time() + 3600, tz=UTC),
        )

"""GitHub App credentials and short-lived installation tokens, platform-only.

The private key signs a JWT valid for minutes; the JWT buys an installation
token narrowed to one repository and read-only permissions, valid for at most
an hour and kept only in this process's memory. Tokens are never logged,
serialised or shown by ``repr``; callers get them through ``reveal()`` and
hand them to exactly one Git subprocess through ``GIT_ASKPASS``.
"""

import logging
import os
import stat
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import jwt

from .client import GitHubClient
from .errors import GitHubError

if TYPE_CHECKING:
    from app.settings import Settings

logger = logging.getLogger(__name__)

READ_ONLY_PERMISSIONS: Mapping[str, str] = {"contents": "read", "metadata": "read"}
# Publication (ADR 0011 §4): two separate tokens, each with the least it needs.
PUSH_PERMISSIONS: Mapping[str, str] = {"contents": "write", "metadata": "read"}
PULLS_PERMISSIONS: Mapping[str, str] = {"pull_requests": "write", "metadata": "read"}
PUBLISH_PERMISSIONS: Mapping[str, str] = {
    "contents": "write",
    "pull_requests": "write",
    "metadata": "read",
}
JWT_SKEW_SECONDS = 60
JWT_TTL_SECONDS = 9 * 60  # GitHub allows 10 minutes; stay under it
RENEW_FRACTION = 0.8
MIN_REMAINING_SECONDS = 120
_LEVELS = {"read": 0, "write": 1}


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GitHubAppCredentials:
    """The App's identity: id plus private key. Signs JWTs; holds no tokens."""

    def __init__(
        self, app_id: int, private_key_pem: bytes, *, clock: Callable[[], datetime] = _utcnow
    ) -> None:
        if app_id <= 0 or not private_key_pem:
            raise GitHubError("github_unconfigured", "GitHub App sin identificador o sin clave")
        self.app_id = app_id
        self._key = private_key_pem
        self._clock = clock

    @classmethod
    def from_settings(cls, settings: "Settings") -> "GitHubAppCredentials | None":
        """``None`` when the App is not configured; refuses an unsafe key file."""
        if settings.github_app_id is None or settings.github_app_private_key_file is None:
            return None
        path = Path(settings.github_app_private_key_file)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise GitHubError(
                "github_unconfigured",
                "la clave privada de la GitHub App debe ser un archivo regular con modo 0600",
            )
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise GitHubError(
                "github_unconfigured",
                "la clave privada de la GitHub App debe pertenecer al proceso",
            )
        return cls(int(settings.github_app_id), path.read_bytes())

    def app_jwt(self) -> str:
        now = int(self._clock().timestamp())
        claims = {
            "iat": now - JWT_SKEW_SECONDS,
            "exp": now + JWT_TTL_SECONDS,
            "iss": str(self.app_id),
        }
        return jwt.encode(claims, self._key, algorithm="RS256")

    def __repr__(self) -> str:
        return f"GitHubAppCredentials(app_id={self.app_id})"


class InstallationToken:
    """An installation token and what it is good for. The value is private."""

    __slots__ = ("_value", "expires_at", "installation_id", "permissions", "repository")

    def __init__(
        self,
        value: str,
        *,
        expires_at: datetime,
        installation_id: int,
        repository: str,
        permissions: Mapping[str, str],
    ) -> None:
        self._value = value
        self.expires_at = expires_at
        self.installation_id = installation_id
        self.repository = repository
        self.permissions = dict(permissions)

    def reveal(self) -> str:
        """The secret itself: hand it to one Git subprocess and nowhere else."""
        return self._value

    def remaining(self, now: datetime) -> timedelta:
        return self.expires_at - now

    def __repr__(self) -> str:
        return (
            f"InstallationToken(installation_id={self.installation_id}, "
            f"repository={self.repository!r}, expires_at={self.expires_at.isoformat()})"
        )

    __str__ = __repr__


class InstallationTokenProvider:
    """Mints and caches installation tokens, narrowed per repository and permission set.

    Only permissions listed in ``allowed`` may be requested (read-only in this
    phase); a request for anything else never reaches GitHub. Concurrent callers
    share one mint through a lock. ``invalidate`` drops a token GitHub refused.
    """

    def __init__(
        self,
        client: GitHubClient,
        credentials: GitHubAppCredentials,
        *,
        allowed: Mapping[str, str] | None = None,
        write_enabled: bool = False,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._client = client
        self._credentials = credentials
        # Read-only by default. Write scopes exist only when the operator turned
        # GITHUB_WRITE_ENABLED on; even then a request may name one level per
        # scope (read tokens for mirrors, write tokens for the Publisher).
        if allowed is None:
            allowed = dict(READ_ONLY_PERMISSIONS)
            if write_enabled:
                allowed.update(PUBLISH_PERMISSIONS)
        self._allowed = dict(allowed)
        self.write_enabled = write_enabled
        self._clock = clock
        self._cache: dict[tuple[int, str, tuple[tuple[str, str], ...]], InstallationToken] = {}
        self._lock = threading.Lock()
        self.minted = 0  # counter for audit and tests; never the tokens themselves

    def token_for(
        self,
        installation_id: int,
        repository: str,
        *,
        permissions: Mapping[str, str] | None = None,
    ) -> InstallationToken:
        wanted = dict(permissions or READ_ONLY_PERMISSIONS)
        for scope, level in wanted.items():
            granted = self._allowed.get(scope)
            if granted is None or _LEVELS.get(level, 99) > _LEVELS.get(granted, -1):
                raise GitHubError(
                    "permission_not_allowed",
                    f"permiso {scope}:{level} no permitido en esta fase",
                )
        key = (int(installation_id), repository, tuple(sorted(wanted.items())))
        with self._lock:
            cached = self._cache.get(key)
            now = self._clock()
            if cached is not None and self._fresh(cached, now):
                return cached
            value, expires_at = self._client.create_installation_token(
                self._credentials.app_jwt(),
                installation_id,
                repositories=[repository],
                permissions=wanted,
            )
            token = InstallationToken(
                value,
                expires_at=expires_at,
                installation_id=int(installation_id),
                repository=repository,
                permissions=wanted,
            )
            self._cache[key] = token
            self.minted += 1
            logger.info(
                "Token de instalación emitido: installation=%s repository=%s expira=%s",
                installation_id,
                repository,
                expires_at.isoformat(),
            )
            return token

    def revoke(self, token: InstallationToken) -> bool:
        """Ask GitHub to kill the token now and forget it locally (publication cleanup)."""
        self.invalidate(token)
        return self._client.revoke_installation_token(token.reveal())

    def invalidate(self, token: InstallationToken) -> None:
        with self._lock:
            for key, cached in list(self._cache.items()):
                if cached is token:
                    del self._cache[key]

    def _fresh(self, token: InstallationToken, now: datetime) -> bool:
        issued_ttl = timedelta(hours=1)
        remaining = token.remaining(now)
        if remaining <= timedelta(seconds=MIN_REMAINING_SECONDS):
            return False
        return remaining > issued_ttl * (1 - RENEW_FRACTION)

    def __repr__(self) -> str:
        return (
            f"InstallationTokenProvider(app_id={self._credentials.app_id}, "
            f"cached={len(self._cache)})"
        )

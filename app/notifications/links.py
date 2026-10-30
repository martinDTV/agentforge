"""Links a mail may carry to the platform itself.

The API is not published on the Internet, so a link only makes sense when the
operator configured ``PUBLIC_APP_URL``: an ``https`` destination that is
reachable from where the mails are read (a reverse proxy, a private network
with TLS). Without it, mails show how to look at the run from the CLI. Links
never carry tokens and never decide anything: they open a read-only resource
that still requires the reader to authenticate.
"""

import logging
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from .sanitize import safe_https_url

logger = logging.getLogger(__name__)


def validate_public_app_url(value: str) -> str:
    """Strict form of ``PUBLIC_APP_URL``: https, host, no credentials, query or fragment."""
    text = value.strip()
    parts = urlsplit(text)
    if parts.query or parts.fragment:
        raise ValueError("PUBLIC_APP_URL no admite query ni fragmento")
    if safe_https_url(text) is None:
        raise ValueError(
            "PUBLIC_APP_URL debe ser una URL https hacia un host accesible (no loopback) "
            "y sin credenciales"
        )
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class PublicLinks:
    """Builds the platform links of a mail, or none at all."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base = validate_public_app_url(base_url) if base_url else None

    @classmethod
    def resolve(cls, public_app_url: str | None, *, legacy: str | None = None) -> "PublicLinks":
        """``PUBLIC_APP_URL`` wins; the historical ``AGENTFORGE_PUBLIC_URL`` is used only when
        it already satisfies the strict rules (a loopback address never does)."""
        if public_app_url:
            return cls(public_app_url)
        if legacy:
            try:
                return cls(legacy)
            except ValueError:
                logger.warning(
                    "AGENTFORGE_PUBLIC_URL no es una URL pública válida; los correos no "
                    "llevarán enlaces a la plataforma (usa PUBLIC_APP_URL con https)"
                )
        return cls(None)

    @property
    def configured(self) -> bool:
        return self._base is not None

    @property
    def base_url(self) -> str | None:
        return self._base

    def run_url(self, run_id: UUID | str) -> str | None:
        return f"{self._base}/api/v1/runs/{run_id}" if self._base else None

    def goal_url(self, goal_id: UUID | str) -> str | None:
        return f"{self._base}/api/v1/goals/{goal_id}" if self._base else None

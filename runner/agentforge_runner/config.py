"""Runner settings (``RUNNER_*`` environment). Images are pinned by digest;
the bind address must be loopback or a tailnet address; there is no knob to
relax isolation."""

import ipaddress
import re
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .protocol import MiB

TAILNET = ipaddress.ip_network("100.64.0.0/10")
_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")


def is_private_bind(host: str) -> bool:
    """Loopback, tailnet (CGNAT), RFC 1918 or ULA: never the wildcard, never global."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or host.endswith(".ts.net")
    if address.is_unspecified or address.is_multicast:
        return False
    return address.is_loopback or address.is_private or address in TAILNET


class RunnerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RUNNER_", env_file=".env", extra="ignore")

    runner_id: str = Field(min_length=1, max_length=128)
    # sha256 (hex) of the bearer token the API presents. The token itself is
    # never stored here: generate both with ``agentforge-runner token``.
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Optional second credential for operators (inspect/reconcile/resolve
    # uncertain executions). Without it the admin endpoints do not exist.
    admin_token_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    bind_host: str = "127.0.0.1"
    # Global addresses are refused unless the operator terminates TLS in front
    # and opts in explicitly (cloud deployments behind a private load balancer).
    allow_public_bind: bool = False
    port: int = Field(default=8787, ge=1, le=65535)
    state_dir: Path = Path("/var/lib/agentforge-runner")
    # profile -> image reference pinned by digest, e.g.
    # {"python-3.13": "localhost/agentforge/python-3.13@sha256:..."}
    images: dict[str, str]
    podman_binary: str = "podman"
    max_concurrent: int = Field(default=2, ge=1, le=16)
    # Ceilings the runner enforces on top of what each request asks for.
    max_cpu_seconds: float = Field(default=600.0, gt=0)
    max_memory_bytes: int = Field(default=4096 * MiB, ge=64 * MiB)
    max_pids: int = Field(default=1024, ge=16)
    max_wall_seconds: float = Field(default=900.0, gt=0)
    max_storage_bytes: int = Field(default=2048 * MiB, ge=16 * MiB)
    max_output_bytes: int = Field(default=4 * MiB, ge=4096)
    # Workspace reception.
    max_archive_bytes: int = Field(default=50 * MiB, ge=1024)
    max_files: int = Field(default=20_000, ge=1)
    max_file_bytes: int = Field(default=8 * MiB, ge=1)
    # Defense in depth, never the only check: the bearer token is.
    allowed_client_cidrs: list[str] = Field(
        default_factory=lambda: ["100.64.0.0/10", "127.0.0.1/32", "::1/128"]
    )
    kill_grace_seconds: float = Field(default=5.0, ge=0)
    health_cache_seconds: float = Field(default=30.0, ge=0)
    # The isolation probe starts a container: cache it longer than plain info.
    probe_cache_seconds: float = Field(default=600.0, ge=0)

    @field_validator("images")
    @classmethod
    def _images_are_pinned(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("RUNNER_IMAGES debe declarar al menos un perfil")
        for profile, reference in value.items():
            if not _DIGEST_RE.search(reference):
                raise ValueError(
                    f"la imagen del perfil {profile} debe fijarse por @sha256:<digest>"
                )
        return value

    @field_validator("allowed_client_cidrs")
    @classmethod
    def _cidrs_parse(cls, value: list[str]) -> list[str]:
        for cidr in value:
            ipaddress.ip_network(cidr)
        return value

    @model_validator(mode="after")
    def _binds_privately(self) -> "RunnerSettings":
        if self.allow_public_bind:
            try:
                if ipaddress.ip_address(self.bind_host).is_unspecified:
                    raise ValueError("RUNNER_BIND_HOST no puede ser la dirección comodín")
            except ValueError as exc:
                if "comodín" in str(exc):
                    raise
            return self
        if not is_private_bind(self.bind_host):
            raise ValueError(
                "RUNNER_BIND_HOST debe ser loopback, tailnet o una dirección privada "
                "(nunca 0.0.0.0); RUNNER_ALLOW_PUBLIC_BIND=true solo detrás de TLS privado"
            )
        return self

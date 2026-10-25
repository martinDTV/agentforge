"""``SandboxPort`` over HTTP to the AgentForge Runner on the Linux VM (ADR 0007).

Protocol (v1), served by the runner over the tailnet:

- ``GET  /v1/health``                -> RunnerHealth JSON (identity, backend, profiles)
- ``POST /v1/executions``            -> multipart: ``request`` (SandboxRequest JSON) +
                                         ``workspace`` (tar.gz); ``Idempotency-Key`` =
                                         execution id; 200 SandboxResult, 409 when the
                                         same execution is already running, 401/403 on
                                         bad credentials, 422 when the runner rejects
                                         the plan (its own allowlist is authoritative)
- ``DELETE /v1/executions/{id}``     -> stops the container; 204 / 404

Safety properties of this client: it never runs anything locally; it refuses
to execute until the runner's identity and backend were verified through
``/v1/health``; it ignores proxy variables and ``.netrc`` (``trust_env=False``);
it only speaks plain HTTP to tailnet addresses; a read timeout triggers a
cancel on the runner before reporting ``runner_timeout``.
"""

import ipaddress
import logging
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.sandbox import (
    RunnerHealth,
    SandboxError,
    SandboxRequest,
    SandboxResult,
    WorkspaceArchive,
)
from app.settings import Settings

logger = logging.getLogger(__name__)

TAILNET_CGNAT = ipaddress.ip_network("100.64.0.0/10")
EXPECTED_BACKEND = "podman-rootless"
USER_AGENT = "agentforge-sandbox-client/1"


def is_tailnet_host(host: str) -> bool:
    """Overlay (CGNAT, ``*.ts.net``) or loopback: links that are already
    encrypted and authenticated below HTTP."""
    if host.endswith(".ts.net") or host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address in TAILNET_CGNAT


def is_private_lan_host(host: str) -> bool:
    """RFC 1918 / ULA addresses: a local VM or a VPC. Plain HTTP here needs an
    explicit opt-in because nothing encrypts the hop."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private and not address.is_loopback and address not in TAILNET_CGNAT


class RunnerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_url: str = Field(min_length=8, max_length=512)
    token: SecretStr
    expected_runner_id: str = Field(min_length=1, max_length=128)
    ca_file: Path | None = None
    # Plain HTTP to a LAN/VPC address (a local VM, a cloud subnet): opt-in only,
    # never in production-like environments (Settings refuses it there).
    allow_private_http: bool = False
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    health_cache_seconds: float = Field(default=60.0, ge=0)
    max_response_bytes: int = Field(default=8 * 1024 * 1024, ge=4096)

    @model_validator(mode="after")
    def _private_transport_only(self) -> "RunnerConfig":
        parts = urlsplit(self.base_url)
        host = parts.hostname or ""
        if parts.scheme == "https":
            return self
        if parts.scheme == "http" and is_tailnet_host(host):
            return self  # the overlay already encrypts and authenticates the hop
        if parts.scheme == "http" and self.allow_private_http and is_private_lan_host(host):
            return self
        raise ValueError(
            "SANDBOX_RUNNER_URL debe ser https://, http:// hacia loopback o una red superpuesta "
            "privada, o http:// hacia una dirección privada con SANDBOX_RUNNER_ALLOW_PRIVATE_HTTP"
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "RunnerConfig":
        assert settings.sandbox_runner_url and settings.sandbox_runner_token
        assert settings.sandbox_runner_id
        return cls(
            base_url=settings.sandbox_runner_url,
            token=settings.sandbox_runner_token,
            expected_runner_id=settings.sandbox_runner_id,
            ca_file=settings.sandbox_runner_ca_file,
            allow_private_http=settings.sandbox_runner_allow_private_http,
        )


class RemoteSandboxAdapter:
    def __init__(
        self,
        config: RunnerConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._clock = clock
        self._verified_at: float | None = None
        self._client = httpx.Client(
            base_url=config.base_url,
            headers={
                "Authorization": f"Bearer {config.token.get_secret_value()}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(config.connect_timeout_seconds),
            verify=str(config.ca_file) if config.ca_file else True,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "RemoteSandboxAdapter":
        return cls(RunnerConfig.from_settings(settings))

    # -- health and verification ----------------------------------------------------------

    def health(self) -> RunnerHealth:
        try:
            response = self._client.get("/v1/health")
        except httpx.TransportError as exc:
            raise SandboxError(
                "runner_unreachable", f"runner inaccesible: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code in (401, 403):
            raise SandboxError("runner_auth_failed", "el runner rechazó la credencial")
        if response.status_code != 200:
            raise SandboxError("runner_protocol_error", f"health devolvió {response.status_code}")
        try:
            reported = RunnerHealth.model_validate(response.json())
        except ValueError as exc:
            raise SandboxError("runner_protocol_error", "health ilegible") from exc
        verified = (
            reported.runner_id == self._config.expected_runner_id
            and reported.backend == EXPECTED_BACKEND
            and reported.rootless
        )
        self._verified_at = self._clock() if verified else None
        return reported.model_copy(update={"verified": verified})

    def _ensure_verified(self) -> None:
        fresh = (
            self._verified_at is not None
            and self._clock() - self._verified_at <= self._config.health_cache_seconds
        )
        if fresh:
            return
        health = self.health()
        if not health.verified:
            raise SandboxError(
                "runner_unverified",
                "el runner no acreditó identidad, backend podman rootless o modo sin privilegios",
            )

    # -- execution ------------------------------------------------------------------------

    def run(self, request: SandboxRequest, archive: WorkspaceArchive) -> SandboxResult:
        self._ensure_verified()
        timeout = httpx.Timeout(
            connect=self._config.connect_timeout_seconds,
            read=request.limits.wall_seconds + 30.0,
            write=60.0,
            pool=5.0,
        )
        files: dict[str, tuple[str, bytes | str, str]] = {
            "request": ("request.json", request.model_dump_json(), "application/json"),
            "workspace": ("workspace.tar.gz", archive.content, "application/gzip"),
        }
        try:
            response = self._client.post(
                "/v1/executions",
                files=files,
                headers={"Idempotency-Key": str(request.execution_id)},
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            self.cancel(request.execution_id)
            raise SandboxError(
                "runner_timeout", "el runner no respondió dentro del límite; cancelación enviada"
            ) from exc
        except httpx.TransportError as exc:
            raise SandboxError(
                "runner_unreachable", f"runner inaccesible: {type(exc).__name__}", retryable=True
            ) from exc
        return self._parse_execution(request, response)

    def _parse_execution(self, request: SandboxRequest, response: httpx.Response) -> SandboxResult:
        status = response.status_code
        if status in (401, 403):
            raise SandboxError("runner_auth_failed", "el runner rechazó la credencial")
        if status == 409:
            code = _code_of(response)
            if code not in ("execution_conflict", "execution_uncertain"):
                code = "execution_in_progress"
            raise SandboxError(code, f"el runner no repite esta invocación: {code}")
        if status in (400, 413, 422):
            raise SandboxError(
                "runner_rejected", f"el runner rechazó el plan: {_code_of(response)}"
            )
        if status == 503:
            code = _code_of(response)
            if code not in ("runner_busy", "isolation_unavailable"):
                code = "runner_failed"
            raise SandboxError(code, f"el runner no puede ejecutar ahora: {code}", retryable=True)
        if status >= 500:
            raise SandboxError("runner_failed", f"fallo del runner ({status})", retryable=True)
        if status not in (200, 201):
            raise SandboxError("runner_protocol_error", f"respuesta inesperada {status}")
        if len(response.content) > self._config.max_response_bytes:
            raise SandboxError("runner_protocol_error", "respuesta del runner demasiado grande")
        try:
            result = SandboxResult.model_validate(response.json())
        except ValueError as exc:
            raise SandboxError("runner_protocol_error", "resultado ilegible") from exc
        if result.execution_id != request.execution_id:
            raise SandboxError(
                "runner_protocol_error", "el runner devolvió otro identificador de ejecución"
            )
        if result.runner_id != self._config.expected_runner_id:
            raise SandboxError("runner_unverified", "el resultado no procede del runner esperado")
        return result

    def cancel(self, execution_id: UUID) -> bool:
        try:
            response = self._client.delete(f"/v1/executions/{execution_id}")
        except httpx.HTTPError as exc:
            logger.warning("No se pudo cancelar %s en el runner: %s", execution_id, exc)
            return False
        return response.status_code in (200, 202, 204)

    def close(self) -> None:
        self._client.close()


def _code_of(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return "sin detalle"
    if isinstance(data, dict):
        return str(data.get("code") or data.get("detail") or "sin detalle")[:200]
    return "sin detalle"

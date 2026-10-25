"""Contracts of isolated execution (Fase 5, Paso 3; ADR 0007).

A ``SandboxRequest`` is built entirely by the application from a validated
plan: the model never sees argv, images, limits or network policy. The only
adapter that executes anything is a remote runner on a dedicated Linux VM;
the API host never runs untrusted code.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.contracts.tools import STEP_PATTERN, AgentKind

from .programs import ProgramId

PROFILE_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
ENV_KEY_PATTERN = r"^[A-Z][A-Z0-9_]{0,63}$"
# Prefixes that would hand the sandbox a credential or alter the loader.
FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = (
    "GIT_",
    "SSH_",
    "AWS_",
    "GITHUB_",
    "GOOGLE_",
    "ANTHROPIC_",
    "AZURE_",
    "LD_",
    "DYLD_",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
)
DEFAULT_SANDBOX_ENV: dict[str, str] = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "CI": "1",
    "LANG": "C.UTF-8",
}

MiB = 1024 * 1024


class SandboxStatus(StrEnum):
    COMPLETED = "completed"  # the program exited; see exit_code
    TIMED_OUT = "timed_out"  # killed at the wall-clock limit
    KILLED = "killed"  # killed by a resource limit (memory, pids, storage)
    CANCELLED = "cancelled"  # cancelled by the platform
    FAILED = "failed"  # the runner could not run it (image, volume, backend)
    REJECTED = "rejected"  # the runner refused the request


class SandboxLimits(BaseModel):
    """Every limit is explicit and bounded; the runner enforces them with cgroups."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_seconds: float = Field(default=60.0, gt=0, le=3600)
    memory_bytes: int = Field(default=1024 * MiB, ge=64 * MiB, le=16 * 1024 * MiB)
    pids: int = Field(default=256, ge=16, le=4096)
    wall_seconds: float = Field(default=120.0, gt=0, le=3600)
    storage_bytes: int = Field(default=512 * MiB, ge=16 * MiB, le=20 * 1024 * MiB)
    max_output_bytes: int = Field(default=256_000, ge=4096, le=16 * MiB)


NetworkPolicy = Literal["none"]  # the only policy that exists today


class WorkspaceArchive(BaseModel):
    """The run's work tree as the sandbox will see it. ``content`` is never
    serialized: it travels as a multipart body, not inside JSON."""

    model_config = ConfigDict(frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    file_count: int = Field(ge=0)
    format: Literal["tar.gz"] = "tar.gz"
    content: bytes = Field(repr=False, exclude=True)


class SandboxRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: UUID  # == the tool invocation id: stable across retries
    run_id: UUID
    attempt: int = Field(ge=0)
    step: str = Field(pattern=STEP_PATTERN)
    project_id: str = Field(min_length=1, max_length=64)
    agent: AgentKind
    actor_subject: str = Field(min_length=1, max_length=255)
    repository_id: str = Field(min_length=1, max_length=64)
    branch: str = Field(min_length=1, max_length=200)
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")
    program: ProgramId
    argv: list[str] = Field(min_length=1, max_length=128)
    profile: str = Field(pattern=PROFILE_PATTERN)
    limits: SandboxLimits
    network: NetworkPolicy = "none"
    env: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_SANDBOX_ENV))
    workspace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_bytes: int = Field(ge=0)
    # Every program is untrusted code, including linters and test runners.
    untrusted: Literal[True] = True

    @field_validator("argv")
    @classmethod
    def _argv_is_plain(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item or len(item) > 512 or any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
                raise ValueError("argv contiene un elemento vacío, demasiado largo o con control")
        return value

    @field_validator("env")
    @classmethod
    def _env_is_an_allowlist(cls, value: dict[str, str]) -> dict[str, str]:
        import re

        for key, val in value.items():
            if not re.fullmatch(ENV_KEY_PATTERN, key) or key.startswith(FORBIDDEN_ENV_PREFIXES):
                raise ValueError(f"variable de entorno no permitida en el sandbox: {key}")
            if len(val) > 256 or "\x00" in val:
                raise ValueError(f"valor de entorno inválido para {key}")
        return value


class ResourceUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    cpu_ms: int | None = Field(default=None, ge=0)
    max_rss_bytes: int | None = Field(default=None, ge=0)
    oom_killed: bool = False


class SandboxResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_id: UUID
    status: SandboxStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration_ms: int = Field(ge=0)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    runner_id: str = Field(min_length=1, max_length=128)
    backend: str = Field(
        default="", max_length=64
    )  # reported by the runner, verified by the adapter
    usage: ResourceUsage | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is SandboxStatus.COMPLETED and self.exit_code == 0


class RunnerHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    runner_id: str
    version: str = ""
    backend: str = ""
    rootless: bool = False
    profiles: list[str] = Field(default_factory=list)
    verified: bool = False  # set by the adapter after identity and backend checks
    ready: bool = True  # the runner's own preflight; absent in older runners


class SandboxPort(Protocol):
    """Isolated execution of one validated plan. Synchronous: tools run in a thread."""

    def health(self) -> RunnerHealth: ...

    def run(self, request: SandboxRequest, archive: WorkspaceArchive) -> SandboxResult: ...

    def cancel(self, execution_id: UUID) -> bool:
        """Terminate the real process or container; True when something was stopped."""
        ...

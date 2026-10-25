"""Protocol v1 as the runner sees it: the same JSON as ``app.sandbox``.

Kept as an independent copy on purpose (the runner never imports the API);
a conformance test in the API repository checks both sides round-trip.
"""

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

STEP_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$"
PROFILE_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
ENV_KEY_PATTERN = r"^[A-Z][A-Z0-9_]{0,63}$"
FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = (
    "GIT_",
    "SSH_",
    "AWS_",
    "GITHUB_",
    "GOOGLE_",
    "ANTHROPIC_",
    "AZURE_",
    "TAILSCALE_",
    "LD_",
    "DYLD_",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
)
MiB = 1024 * 1024


class ProgramId(StrEnum):
    RUFF_CHECK = "ruff_check"
    RUFF_FORMAT_CHECK = "ruff_format_check"
    PYTEST = "pytest"


class SandboxStatus(StrEnum):
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    KILLED = "killed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    REJECTED = "rejected"


class SandboxLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_seconds: float = Field(default=60.0, gt=0, le=3600)
    memory_bytes: int = Field(default=1024 * MiB, ge=64 * MiB, le=16 * 1024 * MiB)
    pids: int = Field(default=256, ge=16, le=4096)
    wall_seconds: float = Field(default=120.0, gt=0, le=3600)
    storage_bytes: int = Field(default=512 * MiB, ge=16 * MiB, le=20 * 1024 * MiB)
    max_output_bytes: int = Field(default=256_000, ge=4096, le=16 * MiB)


class SandboxRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: UUID
    run_id: UUID
    attempt: int = Field(ge=0)
    step: str = Field(pattern=STEP_PATTERN)
    project_id: str = Field(min_length=1, max_length=64)
    agent: str = Field(pattern=r"^[a-z][a-z_]{0,31}$")
    actor_subject: str = Field(min_length=1, max_length=255)
    repository_id: str = Field(min_length=1, max_length=64)
    branch: str = Field(min_length=1, max_length=200)
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")
    program: ProgramId
    argv: list[str] = Field(min_length=1, max_length=128)
    profile: str = Field(pattern=PROFILE_PATTERN)
    limits: SandboxLimits
    network: Literal["none"] = "none"
    env: dict[str, str] = Field(default_factory=dict)
    workspace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_bytes: int = Field(ge=0)
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
        for key, val in value.items():
            if not re.fullmatch(ENV_KEY_PATTERN, key) or key.startswith(FORBIDDEN_ENV_PREFIXES):
                raise ValueError(f"variable de entorno no permitida: {key}")
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
    backend: str = "podman-rootless"
    usage: ResourceUsage | None = None


class IsolationCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    ok: bool
    detail: str = ""


class RunnerHealth(BaseModel):
    """What the API verifies: identity, backend and rootless mode. ``ready``
    and ``checks`` are informative extras the API ignores."""

    model_config = ConfigDict(frozen=True)

    runner_id: str
    version: str
    backend: str
    rootless: bool
    profiles: list[str]
    ready: bool
    checks: list[IsolationCheck] = Field(default_factory=list)


ExecutionState = Literal["pending", "running", "finished", "uncertain"]


class ExecutionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_id: UUID
    state: ExecutionState
    program: ProgramId
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested: bool = False
    error_code: str | None = None
    resolution: str | None = None
    container_name: str | None = None
    result: SandboxResult | None = None


class ResolveRequest(BaseModel):
    """An operator may only declare an uncertain execution *failed*; success
    needs evidence from the engine (``/reconcile``), never a human's word."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["failed"]
    reason: str = Field(min_length=3, max_length=500)


class Problem(BaseModel):
    code: str
    detail: str

"""What an isolation backend must provide. Only Podman rootless implements it
for real; ``FakeBackend`` exists for tests."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .protocol import IsolationCheck, SandboxLimits, SandboxStatus


@dataclass(frozen=True)
class RunSpec:
    container_name: str
    image: str  # pinned by digest
    argv: list[str]
    env: dict[str, str]
    archive: bytes  # validated tar.gz, streamed to the container's stdin
    limits: SandboxLimits
    output_cap: int


@dataclass(frozen=True)
class RunOutcome:
    status: SandboxStatus
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int
    started_at: datetime
    finished_at: datetime
    oom_killed: bool = False
    detail: str = ""


@dataclass(frozen=True)
class ContainerState:
    """What the engine itself knows about a container: the evidence used to
    reconcile executions nobody supervised."""

    name: str
    running: bool
    exit_code: int | None
    oom_killed: bool
    finished_at: datetime | None


@dataclass(frozen=True)
class IsolationReport:
    ok: bool
    rootless: bool
    backend: str
    checks: list[IsolationCheck] = field(default_factory=list)


class Backend(Protocol):
    name: str

    def preflight(self, images: dict[str, str]) -> IsolationReport: ...

    def run(self, spec: RunSpec) -> RunOutcome: ...

    def kill(self, container_name: str) -> bool: ...

    def inspect(self, container_name: str) -> ContainerState | None: ...

    def remove(self, container_name: str) -> None: ...

    def list_containers(self) -> list[str]: ...

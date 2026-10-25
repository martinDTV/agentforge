"""Scripted ``SandboxPort`` for tests. Executes nothing, ever."""

import time
from collections.abc import Callable
from uuid import UUID

from app.sandbox import (
    ProgramId,
    RunnerHealth,
    SandboxError,
    SandboxRequest,
    SandboxResult,
    SandboxStatus,
    WorkspaceArchive,
)

Scripted = SandboxResult | SandboxError | Callable[[SandboxRequest], SandboxResult]


class FakeSandbox:
    def __init__(self, *, runner_id: str = "fake-runner", verified: bool = True) -> None:
        self.runner_id = runner_id
        self.verified = verified
        self.requests: list[tuple[SandboxRequest, WorkspaceArchive]] = []
        self.cancelled: list[UUID] = []
        self.scripts: dict[ProgramId, Scripted] = {}
        self.fail_with: SandboxError | None = None
        self.delay_seconds: float = 0.0  # simulates a long run (the fake just sleeps)

    def script(self, program: ProgramId, outcome: Scripted) -> None:
        self.scripts[program] = outcome

    def health(self) -> RunnerHealth:
        return RunnerHealth(
            runner_id=self.runner_id,
            version="fake",
            backend="fake",
            rootless=True,
            profiles=["python-3.13"],
            verified=self.verified,
        )

    def run(self, request: SandboxRequest, archive: WorkspaceArchive) -> SandboxResult:
        self.requests.append((request, archive))
        if self.fail_with is not None:
            raise self.fail_with
        if not self.verified:
            raise SandboxError("runner_unverified", "runner no verificado")
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        scripted = self.scripts.get(request.program)
        if isinstance(scripted, SandboxError):
            raise scripted
        if callable(scripted):
            return scripted(request)
        if scripted is not None:
            return scripted.model_copy(update={"execution_id": request.execution_id})
        return SandboxResult(
            execution_id=request.execution_id,
            status=SandboxStatus.COMPLETED,
            exit_code=0,
            stdout=f"{request.program.value}: ok\n",
            stderr="",
            duration_ms=5,
            runner_id=self.runner_id,
            backend="fake",
        )

    def cancel(self, execution_id: UUID) -> bool:
        self.cancelled.append(execution_id)
        return True

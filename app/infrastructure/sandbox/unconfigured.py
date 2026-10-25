"""The sandbox the platform has when no runner is configured: it refuses.

There is deliberately no local fallback. An unreachable or missing runner
means ``exec`` tools fail with a clear code, never that code runs on the host.
"""

from uuid import UUID

from app.sandbox import RunnerHealth, SandboxError, SandboxRequest, SandboxResult, WorkspaceArchive


class UnconfiguredSandbox:
    def health(self) -> RunnerHealth:
        return RunnerHealth(runner_id="", backend="none", verified=False)

    def run(self, request: SandboxRequest, archive: WorkspaceArchive) -> SandboxResult:
        raise SandboxError(
            "sandbox_unconfigured",
            "no hay un runner de sandbox configurado; la ejecución aislada está deshabilitada",
        )

    def cancel(self, execution_id: UUID) -> bool:
        return False

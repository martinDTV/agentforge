"""``exec.run``: a program from the allowlist, on a copy of the run's worktree,
inside the remote sandbox. Risk class ``exec``.

Nothing here runs locally. The tool validates the plan, packs the worktree,
builds a ``SandboxRequest`` whose execution id is the tool invocation id
(stable across retries, so the runner can refuse a duplicate), and hands it
to the ``SandboxPort``. Without a configured and verified runner the port
refuses and the tool reports it; it never falls back to the host.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.tools import RiskClass
from app.git import GitOperationError, GitRepositoryPort
from app.sandbox import (
    DEFAULT_SANDBOX_ENV,
    ProgramId,
    SandboxError,
    SandboxLimits,
    SandboxPort,
    SandboxRequest,
    SandboxStatus,
    build_plan,
    pack_worktree,
)
from app.settings import Settings
from app.workspaces import WorktreeManager

from .errors import ToolExecutionError
from .managed import managed_worktree
from .ports import ToolContext, ToolSpec
from .redaction import redact

CANCEL_GRACE_SECONDS = 30.0


class ExecutionPolicy(BaseModel):
    """Platform-side bounds for sandbox executions (never chosen by a model)."""

    model_config = ConfigDict(frozen=True)

    limits: SandboxLimits = SandboxLimits()
    profile: str = Field(default="python-3.13", pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    max_archive_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    env: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_SANDBOX_ENV))

    @classmethod
    def from_settings(cls, settings: Settings) -> "ExecutionPolicy":
        mib = 1024 * 1024
        return cls(
            limits=SandboxLimits(
                cpu_seconds=settings.sandbox_cpu_seconds,
                memory_bytes=settings.sandbox_memory_mb * mib,
                pids=settings.sandbox_pids,
                wall_seconds=settings.sandbox_wall_seconds,
                storage_bytes=settings.sandbox_storage_mb * mib,
                max_output_bytes=settings.sandbox_max_output_bytes,
            ),
            profile=settings.sandbox_profile,
            max_archive_bytes=settings.sandbox_max_archive_mb * mib,
        )


class ExecRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    program: ProgramId
    paths: list[str] = Field(default_factory=list, max_length=50)
    keyword: str | None = Field(default=None, max_length=100, description="pytest -k")
    fail_fast: bool = False


class ExecRunOutput(BaseModel):
    execution_id: UUID
    program: ProgramId
    argv: list[str]
    status: SandboxStatus
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int
    runner_id: str
    redactions: int = 0


class ExecRunTool:
    def __init__(
        self,
        sandbox: SandboxPort,
        workspaces: WorktreeManager,
        *,
        git: GitRepositoryPort | None = None,
        policy: ExecutionPolicy | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._workspaces = workspaces
        self._git = git
        self.policy = policy or ExecutionPolicy()
        self.spec = ToolSpec(
            name="exec.run",
            description=(
                "Ejecuta un programa permitido (ruff check, ruff format --check, pytest) "
                "sobre una copia del worktree dentro del sandbox remoto."
            ),
            risk=RiskClass.EXEC,
            input_model=ExecRunInput,
            output_model=ExecRunOutput,
            timeout_seconds=self.policy.limits.wall_seconds + CANCEL_GRACE_SECONDS,
        )

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        args = ExecRunInput.model_validate(arguments, from_attributes=True)
        record, repository = managed_worktree(self._workspaces, context)
        if context.invocation is None:
            raise ToolExecutionError(
                "invocation_required", "exec.run solo se ejecuta a través del gateway"
            )
        try:
            plan = build_plan(
                args.program,
                args.paths,
                worktree=record.path,
                authorized_test_paths=repository.authorized_test_paths,
                keyword=args.keyword,
                fail_fast=args.fail_fast,
            )
            archive = pack_worktree(record.path, max_bytes=self.policy.max_archive_bytes)
        except SandboxError as exc:
            raise ToolExecutionError(exc.code, exc.detail) from exc
        head_sha: str | None = None
        if self._git is not None:
            try:
                head_sha = self._git.head(record.path).sha
            except GitOperationError:
                head_sha = None
        request = SandboxRequest(
            execution_id=context.invocation.id,
            run_id=context.run_id,
            attempt=context.attempt,
            step=context.invocation.step,
            project_id=context.project_id,
            agent=context.agent,
            actor_subject=context.principal.subject,
            repository_id=repository.id,
            branch=record.branch,
            head_sha=head_sha,
            program=plan.program,
            argv=plan.argv,
            profile=self.policy.profile,
            limits=self.policy.limits,
            env=dict(self.policy.env),
            workspace_sha256=archive.sha256,
            workspace_bytes=archive.size_bytes,
        )
        try:
            result = self._sandbox.run(request, archive)
        except SandboxError as exc:
            raise ToolExecutionError(exc.code, exc.detail) from exc
        stdout, n_out = redact(result.stdout)
        stderr, n_err = redact(result.stderr)
        return ExecRunOutput(
            execution_id=result.execution_id,
            program=plan.program,
            argv=plan.argv,
            status=result.status,
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            duration_ms=result.duration_ms,
            runner_id=result.runner_id,
            redactions=n_out + n_err,
        )

    def cancel(self, context: ToolContext) -> bool:
        if context.invocation is None:
            return False
        return self._sandbox.cancel(context.invocation.id)

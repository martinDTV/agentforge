"""Git tools over the run's managed worktree.

Read tools (``git.log``, ``git.diff``, ``git.status``) expose history,
differences and metadata through nominal arguments; a model never passes a
git command line. Write contracts (``git.commit``, ``git.push``) exist so the
policy and the audit already know them, but their risk classes are not
enabled and the adapter refuses pushes outright.

Every tool first proves the context's ``workspace_root`` is a worktree this
platform created for this run and project, backed by a registered
repository; anything else is ``workspace_not_managed`` / ``workspace_mismatch``.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.tools import RiskClass
from app.git import (
    CommitInfo,
    DiffResult,
    GitOperationError,
    GitRepositoryPort,
    RepositoryConfig,
    StatusResult,
)
from app.workspaces import WorktreeManager, WorktreeRecord

from .errors import ToolExecutionError
from .limits import ToolLimits
from .managed import managed_worktree
from .ports import ToolContext, ToolPort, ToolSpec

_REF_FIELD = Field(default=None, max_length=200)
_PATH_FIELD = Field(default=None, max_length=1_024)


def _git_error(exc: GitOperationError) -> ToolExecutionError:
    return ToolExecutionError(exc.code, exc.detail)


class GitTools:
    """Factory bound to the adapter, the worktree manager and the limits."""

    def __init__(
        self, git: GitRepositoryPort, workspaces: WorktreeManager, limits: ToolLimits
    ) -> None:
        self.git = git
        self.workspaces = workspaces
        self.limits = limits

    def tools(self) -> list[ToolPort]:
        return [
            GitLogTool(self),
            GitDiffTool(self),
            GitStatusTool(self),
            GitCommitTool(self),
            GitPushTool(self),
        ]

    def managed(self, context: ToolContext) -> tuple[WorktreeRecord, RepositoryConfig]:
        return managed_worktree(self.workspaces, context)


# -- git.log ------------------------------------------------------------------------------


class GitLogInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_count: int = Field(default=20, ge=1, le=100)
    ref: str | None = _REF_FIELD
    path: str | None = _PATH_FIELD


class GitLogOutput(BaseModel):
    branch: str | None
    commits: list[CommitInfo]


class GitLogTool:
    spec = ToolSpec(
        name="git.log",
        description="Historial reciente del worktree de la ejecución (acotado).",
        risk=RiskClass.READ,
        input_model=GitLogInput,
        output_model=GitLogOutput,
    )

    def __init__(self, owner: GitTools) -> None:
        self._owner = owner

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        args = GitLogInput.model_validate(arguments, from_attributes=True)
        record, _ = self._owner.managed(context)
        try:
            commits = self._owner.git.log(
                record.path, ref=args.ref, max_count=args.max_count, path=args.path
            )
            branch = self._owner.git.current_branch(record.path)
        except GitOperationError as exc:
            raise _git_error(exc) from exc
        return GitLogOutput(branch=branch, commits=commits)


# -- git.diff -----------------------------------------------------------------------------


class GitDiffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base: str | None = _REF_FIELD
    target: str | None = _REF_FIELD
    path: str | None = _PATH_FIELD


class GitDiffTool:
    spec = ToolSpec(
        name="git.diff",
        description=(
            "Diferencias del worktree frente a HEAD, o entre dos referencias, acotadas en bytes."
        ),
        risk=RiskClass.READ,
        input_model=GitDiffInput,
        output_model=DiffResult,
    )

    def __init__(self, owner: GitTools) -> None:
        self._owner = owner

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        args = GitDiffInput.model_validate(arguments, from_attributes=True)
        record, _ = self._owner.managed(context)
        try:
            return self._owner.git.diff(
                record.path,
                base=args.base,
                target=args.target,
                path=args.path,
                max_bytes=self._owner.limits.max_read_bytes,
            )
        except GitOperationError as exc:
            raise _git_error(exc) from exc


# -- git.status ---------------------------------------------------------------------------


class GitStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GitStatusTool:
    spec = ToolSpec(
        name="git.status",
        description="Rama, HEAD y archivos modificados del worktree de la ejecución.",
        risk=RiskClass.READ,
        input_model=GitStatusInput,
        output_model=StatusResult,
    )

    def __init__(self, owner: GitTools) -> None:
        self._owner = owner

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        record, _ = self._owner.managed(context)
        try:
            return self._owner.git.status(record.path, max_entries=self._owner.limits.max_entries)
        except GitOperationError as exc:
            raise _git_error(exc) from exc


# -- write contracts (risk classes not enabled in this step) -----------------------------


class GitCommitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2_000)
    paths: list[str] | None = Field(default=None, max_length=200)


class GitCommitOutput(BaseModel):
    sha: str
    short_sha: str
    branch: str


class GitCommitTool:
    spec = ToolSpec(
        name="git.commit",
        description="Confirma los cambios del worktree en la rama run/<run_id> de la ejecución.",
        risk=RiskClass.WRITE,
        input_model=GitCommitInput,
        output_model=GitCommitOutput,
    )

    def __init__(self, owner: GitTools) -> None:
        self._owner = owner

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        args = GitCommitInput.model_validate(arguments, from_attributes=True)
        record, repository = self._owner.managed(context)
        try:
            commit = self._owner.git.commit(record.path, repository, args.message, paths=args.paths)
        except GitOperationError as exc:
            raise _git_error(exc) from exc
        return GitCommitOutput(sha=commit.sha, short_sha=commit.short_sha, branch=record.branch)


class GitPushInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GitPushOutput(BaseModel):
    branch: str
    pushed: bool


class GitPushTool:
    spec = ToolSpec(
        name="git.push",
        description="Publica la rama run/<run_id> en el remoto (deshabilitado en esta fase).",
        risk=RiskClass.NETWORK,
        input_model=GitPushInput,
        output_model=GitPushOutput,
    )

    def __init__(self, owner: GitTools) -> None:
        self._owner = owner

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        record, repository = self._owner.managed(context)
        try:
            self._owner.git.push(repository, record.branch)
        except GitOperationError as exc:
            raise _git_error(exc) from exc
        return GitPushOutput(branch=record.branch, pushed=True)

"""Proof that a context's workspace is a worktree this platform created for
this run and project, backed by a registered repository of the project."""

from app.git import GitOperationError, RepositoryConfig
from app.workspaces import WorktreeManager, WorktreeRecord

from .errors import ToolExecutionError
from .ports import ToolContext


def managed_worktree(
    workspaces: WorktreeManager, context: ToolContext
) -> tuple[WorktreeRecord, RepositoryConfig]:
    if context.workspace_root is None:
        raise ToolExecutionError(
            "workspace_unavailable", "esta ejecución no tiene un workspace asignado"
        )
    record = workspaces.record_at(context.workspace_root)
    if record is None:
        raise ToolExecutionError(
            "workspace_not_managed", "el workspace no es un worktree administrado"
        )
    if record.run_id != context.run_id or record.project_id != context.project_id:
        raise ToolExecutionError(
            "workspace_mismatch", "el worktree pertenece a otra ejecución o proyecto"
        )
    try:
        repository = workspaces.repositories.require(record.repository_id, context.project_id)
    except GitOperationError as exc:
        raise ToolExecutionError(exc.code, exc.detail) from exc
    return record, repository

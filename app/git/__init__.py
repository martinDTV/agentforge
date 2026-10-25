"""Managed Git repositories (Fase 5, Paso 2): nominal operations, protected refs,
trusted registry. The adapter that runs the binary lives in ``app/infrastructure/git``."""

from .errors import GitOperationError
from .ports import (
    CommitInfo,
    DiffResult,
    FileChange,
    GitIdentity,
    GitRepositoryPort,
    RepositoryConfig,
    RepositoryRegistry,
    StatusEntry,
    StatusResult,
    WorktreeInfo,
)
from .refs import (
    DEFAULT_PROTECTED_REFS,
    RUN_BRANCH_PREFIX,
    is_protected,
    is_run_branch,
    run_branch,
    validate_branch_name,
    validate_pathspec,
    validate_ref,
)

__all__ = [
    "DEFAULT_PROTECTED_REFS",
    "RUN_BRANCH_PREFIX",
    "CommitInfo",
    "DiffResult",
    "FileChange",
    "GitIdentity",
    "GitOperationError",
    "GitRepositoryPort",
    "RepositoryConfig",
    "RepositoryRegistry",
    "StatusEntry",
    "StatusResult",
    "WorktreeInfo",
    "is_protected",
    "is_run_branch",
    "run_branch",
    "validate_branch_name",
    "validate_pathspec",
    "validate_ref",
]

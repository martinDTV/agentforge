"""What a managed repository is and what can be done to it.

``RepositoryConfig`` is trusted configuration written by the operator: a
model only ever names a repository by id and never chooses a path or a
remote. ``GitRepositoryPort`` is the nominal vocabulary of operations; the
only adapter runs the ``git`` binary with arguments built by the application.
"""

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.execution import PROJECT_ID_PATTERN

from .errors import GitOperationError
from .refs import DEFAULT_PROTECTED_REFS, is_protected

REPOSITORY_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class GitIdentity(BaseModel):
    """The service identity commits are made with. Never a person's."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(default="AgentForge", min_length=1, max_length=100)
    email: str = Field(default="agentforge@noreply.agentforge.local", pattern=r"^[^@\s]+@[^@\s]+$")


class RepositoryConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=REPOSITORY_ID_PATTERN)
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    path: Path
    default_branch: str = Field(default="main", min_length=1, max_length=200)
    protected_refs: frozenset[str] = DEFAULT_PROTECTED_REFS
    identity: GitIdentity = GitIdentity()
    # Where test programs (pytest) may be pointed at inside this repository.
    authorized_test_paths: frozenset[str] = frozenset({"tests"})

    @property
    def protected(self) -> frozenset[str]:
        return self.protected_refs | {self.default_branch}

    def is_protected(self, ref: str) -> bool:
        return is_protected(ref, self.protected)


def _looks_like_git_repository(path: Path) -> bool:
    for candidate in (path, path / ".git"):
        if (candidate / "HEAD").is_file() and (candidate / "objects").is_dir():
            return True
    return False


class RepositoryRegistry:
    """Trusted set of repositories, all living under one root AgentForge owns."""

    def __init__(self, root: Path, configs: Iterable[RepositoryConfig] = ()) -> None:
        if not root.is_absolute():
            raise GitOperationError(
                "path_outside_root", "la raíz de repositorios debe ser absoluta"
            )
        self._root = root.resolve()
        self._configs: dict[str, RepositoryConfig] = {}
        for config in configs:
            self.register(config)

    @property
    def root(self) -> Path:
        return self._root

    def register(self, config: RepositoryConfig) -> RepositoryConfig:
        if config.id in self._configs:
            raise GitOperationError("repository_invalid", f"repositorio duplicado: {config.id}")
        if not config.path.is_absolute():
            raise GitOperationError("path_outside_root", f"ruta no absoluta: {config.path}")
        try:
            resolved = config.path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise GitOperationError("repository_invalid", f"no existe: {config.path}") from exc
        if resolved != self._root and not resolved.is_relative_to(self._root):
            raise GitOperationError(
                "path_outside_root", f"el repositorio {config.id} está fuera de {self._root}"
            )
        if not _looks_like_git_repository(resolved):
            raise GitOperationError(
                "repository_invalid", f"{config.path} no parece un repositorio Git"
            )
        stored = config.model_copy(update={"path": resolved})
        self._configs[config.id] = stored
        return stored

    def get(self, repository_id: str) -> RepositoryConfig | None:
        return self._configs.get(repository_id)

    def require(self, repository_id: str, project_id: str) -> RepositoryConfig:
        config = self._configs.get(repository_id)
        if config is None:
            raise GitOperationError(
                "repository_unknown", f"repositorio no registrado: {repository_id}"
            )
        if config.project_id != project_id:
            raise GitOperationError(
                "repository_mismatch",
                f"el repositorio {repository_id} no pertenece al proyecto {project_id}",
            )
        return config

    def for_project(self, project_id: str) -> list[RepositoryConfig]:
        return [c for c in self._configs.values() if c.project_id == project_id]

    def ids(self) -> list[str]:
        return sorted(self._configs)

    @classmethod
    def from_file(cls, root: Path, file: Path) -> "RepositoryRegistry":
        """``{"repositories": [{"id": ..., "project_id": ..., "path": ...}, ...]}``."""
        data = json.loads(file.read_text(encoding="utf-8"))
        entries = data.get("repositories", []) if isinstance(data, dict) else data
        return cls(root, [RepositoryConfig.model_validate(entry) for entry in entries])


# -- values returned by read operations -------------------------------------------------


class CommitInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    sha: str
    short_sha: str
    author_name: str
    author_email: str
    authored_at: datetime
    subject: str


class FileChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    additions: int | None = None  # None for binary files
    deletions: int | None = None


class DiffResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    base: str
    target: str
    files: list[FileChange]
    patch: str
    truncated: bool = False


class StatusEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    state: str  # the two-letter porcelain code, e.g. " M", "??", "A "


class StatusResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    branch: str | None
    head: str | None
    clean: bool
    entries: list[StatusEntry]
    truncated: bool = False


class WorktreeInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path
    branch: str | None = None
    head: str | None = None
    bare: bool = False
    prunable: bool = False


class GitRepositoryPort(Protocol):
    """Nominal Git operations. No method accepts raw arguments or a shell string."""

    # -- read ----------------------------------------------------------------------------

    def head(self, worktree: Path) -> CommitInfo: ...

    def current_branch(self, worktree: Path) -> str | None: ...

    def log(
        self,
        worktree: Path,
        *,
        ref: str | None = None,
        max_count: int = 20,
        path: str | None = None,
    ) -> list[CommitInfo]: ...

    def diff(
        self,
        worktree: Path,
        *,
        base: str | None = None,
        target: str | None = None,
        path: str | None = None,
        max_bytes: int = 64_000,
    ) -> DiffResult: ...

    def status(self, worktree: Path, *, max_entries: int = 500) -> StatusResult: ...

    def rev_parse(self, repository: RepositoryConfig, ref: str) -> str: ...

    def branch_exists(self, repository: RepositoryConfig, branch: str) -> bool: ...

    # -- worktrees (used by the WorktreeManager, never by a model) ----------------------

    def worktree_add(
        self, repository: RepositoryConfig, path: Path, branch: str, *, start_point: str
    ) -> None: ...

    def worktree_list(self, repository: RepositoryConfig) -> list[WorktreeInfo]: ...

    def worktree_prune(self, repository: RepositoryConfig) -> None: ...

    # -- write contracts (protected refs refused; not granted to agents yet) -------------

    def create_branch(
        self, repository: RepositoryConfig, name: str, *, start_point: str
    ) -> None: ...

    def commit(
        self,
        worktree: Path,
        repository: RepositoryConfig,
        message: str,
        *,
        paths: Sequence[str] | None = None,
    ) -> CommitInfo: ...

    def push(self, repository: RepositoryConfig, branch: str) -> None: ...

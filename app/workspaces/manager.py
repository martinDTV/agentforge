"""One Git worktree per run, on its own ``run/<run_id>`` branch.

Layout: ``<root>/<project_id>/<run_id>/`` holds a marker file, written last
and atomically, next to the checkout in ``tree/``. The marker is outside the
work tree on purpose: it never shows up in ``git status`` nor in a commit. A
run directory without a valid marker is a partial creation and is rebuilt.
Everything is derived from identifiers that are validated before
they become path components, so neither a model nor a bug can point the
manager outside its root. Symlinks are never followed when removing.

Lifecycle: ``ensure`` (idempotent create or recover) -> ``release(status)``
when the run stops -> ``sweep`` removes released worktrees once their
retention expired. Interrupted runs keep their worktree much longer so a
manual resume finds it. Quotas: a maximum of worktrees per project and a
minimum of free disk space, both checked at creation.
"""

import json
import logging
import os
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.execution import PROJECT_ID_PATTERN
from app.contracts.lifecycle import RunLifecycleStatus
from app.git import GitOperationError, GitRepositoryPort, RepositoryConfig, RepositoryRegistry
from app.git.refs import run_branch
from app.settings import Settings

from .errors import WorkspaceError

logger = logging.getLogger(__name__)

MARKER_NAME = "worktree.json"
TREE_NAME = "tree"
MARKER_VERSION = 1
_PROJECT_RE = re.compile(PROJECT_ID_PATTERN)
Clock = Callable[[], datetime]


class WorktreePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Finished runs (approved, rejected, failed) keep their worktree this long.
    retention_seconds: float = Field(default=3600.0, ge=0)
    # Interrupted runs may be resumed by an operator: keep much longer.
    interrupted_retention_seconds: float = Field(default=7 * 24 * 3600.0, ge=0)
    max_per_project: int = Field(default=20, ge=1)
    min_free_bytes: int = Field(default=256 * 1024 * 1024, ge=0)

    @classmethod
    def from_settings(cls, settings: Settings) -> "WorktreePolicy":
        return cls(
            retention_seconds=settings.worktree_retention_seconds,
            interrupted_retention_seconds=settings.worktree_interrupted_retention_seconds,
            max_per_project=settings.worktree_max_per_project,
            min_free_bytes=settings.worktree_min_free_bytes,
        )


class WorktreeRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    project_id: str
    repository_id: str
    branch: str
    path: Path
    created_at: datetime
    released_at: datetime | None = None
    release_status: str | None = None

    @property
    def released(self) -> bool:
        return self.released_at is not None

    @property
    def run_dir(self) -> Path:
        return self.path.parent

    @property
    def marker_path(self) -> Path:
        return self.path.parent / MARKER_NAME


class WorktreeSweepReport(BaseModel):
    removed: list[Path] = Field(default_factory=list)
    retained: list[Path] = Field(default_factory=list)
    orphans_removed: list[Path] = Field(default_factory=list)


class WorktreeManager:
    def __init__(
        self,
        root: Path,
        git: GitRepositoryPort,
        repositories: RepositoryRegistry,
        *,
        policy: WorktreePolicy | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not root.is_absolute():
            raise WorkspaceError("storage_invalid", "la raíz de worktrees debe ser absoluta")
        root.mkdir(parents=True, exist_ok=True)
        self._root = root.resolve()
        repo_root = repositories.root
        if self._root.is_relative_to(repo_root) or repo_root.is_relative_to(self._root):
            raise WorkspaceError(
                "storage_invalid", "worktrees y repositorios no pueden anidarse entre sí"
            )
        self.git = git
        self.repositories = repositories
        self.policy = policy or WorktreePolicy()
        self._clock: Clock = clock or (lambda: datetime.now(UTC))

    @property
    def root(self) -> Path:
        return self._root

    # -- paths ----------------------------------------------------------------------------

    def path_for(self, project_id: str, run_id: UUID) -> Path:
        """The run directory: marker beside the checkout."""
        if not _PROJECT_RE.fullmatch(project_id):
            raise WorkspaceError("invalid_identifier", f"project_id inválido: {project_id!r}")
        path = self._root / project_id / str(run_id)
        if path.parent.parent != self._root:  # defensive: identifiers are validated above
            raise WorkspaceError("path_outside_storage", "ruta fuera del almacenamiento")
        return path

    def tree_for(self, project_id: str, run_id: UUID) -> Path:
        """The checkout itself: what becomes ``ToolContext.workspace_root``."""
        return self.path_for(project_id, run_id) / TREE_NAME

    def _inside_root(self, path: Path) -> Path:
        """``path`` must be exactly ``<root>/<project>/<run>`` and not a symlink."""
        if path.is_symlink():
            raise WorkspaceError("path_outside_storage", f"enlace simbólico rechazado: {path}")
        resolved = path.resolve()
        if (
            not resolved.is_relative_to(self._root)
            or len(resolved.relative_to(self._root).parts) != 2
        ):
            raise WorkspaceError("path_outside_storage", f"ruta fuera del almacenamiento: {path}")
        return resolved

    def _run_dir_of(self, path: Path) -> Path | None:
        """Run directory for a run dir or its ``tree``; None for anything else."""
        if path.is_symlink():
            return None
        resolved = path.resolve()
        if not resolved.is_relative_to(self._root):
            return None
        parts = resolved.relative_to(self._root).parts
        if len(parts) == 2:
            return resolved
        if len(parts) == 3 and parts[2] == TREE_NAME:
            return resolved.parent
        return None

    # -- markers --------------------------------------------------------------------------

    def record_at(self, path: Path) -> WorktreeRecord | None:
        """The record of the managed worktree at ``path`` (run dir or checkout), else None."""
        run_dir = self._run_dir_of(path)
        if run_dir is None:
            return None
        marker = run_dir / MARKER_NAME
        if marker.is_symlink() or not marker.is_file():
            return None
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if data.get("version") != MARKER_VERSION:
                return None
            record = WorktreeRecord.model_validate({**data, "path": run_dir / TREE_NAME})
        except (ValueError, OSError):
            return None
        try:
            expected = self.tree_for(record.project_id, record.run_id)
        except WorkspaceError:
            return None
        if expected.resolve() != record.path:
            return None  # a marker copied from another run or project
        return record

    def _write_marker(self, record: WorktreeRecord) -> None:
        payload = record.model_dump(mode="json", exclude={"path"}) | {"version": MARKER_VERSION}
        tmp = record.run_dir / (MARKER_NAME + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(tmp, record.marker_path)

    def _healthy(self, record: WorktreeRecord, repository: RepositoryConfig) -> bool:
        gitfile = record.path / ".git"
        if not gitfile.is_file():
            return False
        try:
            content = gitfile.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if not content.startswith("gitdir: "):
            return False
        gitdir = Path(content.removeprefix("gitdir: "))
        try:
            return gitdir.resolve().is_relative_to(repository.path)
        except OSError:
            return False

    # -- lifecycle ------------------------------------------------------------------------

    def get(self, run_id: UUID, project_id: str) -> WorktreeRecord | None:
        record = self.record_at(self.path_for(project_id, run_id))
        if record is None or record.run_id != run_id or record.project_id != project_id:
            return None
        return record

    def ensure(self, run_id: UUID, project_id: str, repository_id: str) -> WorktreeRecord:
        """Create the run's worktree, or return it if it already exists and is healthy."""
        repository = self.repositories.require(repository_id, project_id)
        run_dir = self.path_for(project_id, run_id)
        existing = self.get(run_id, project_id)
        if existing is not None:
            if existing.repository_id != repository_id:
                raise WorkspaceError(
                    "worktree_corrupt",
                    f"el worktree de {run_id} pertenece al repositorio {existing.repository_id}",
                )
            if self._healthy(existing, repository):
                return existing
            logger.warning("Worktree dañado para %s; se reconstruye", run_id)
            self._remove_path(run_dir, repository)
        elif run_dir.exists() or run_dir.is_symlink():
            logger.warning("Creación parcial detectada para %s; se reconstruye", run_id)
            self._remove_path(run_dir, repository)
        self._check_quota(project_id)
        branch = run_branch(run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        tree = run_dir / TREE_NAME
        self.git.worktree_prune(repository)
        self.git.worktree_add(repository, tree, branch, start_point=repository.default_branch)
        record = WorktreeRecord(
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            branch=branch,
            path=tree.resolve(),
            created_at=self._clock(),
        )
        self._write_marker(record)
        return record

    def release(
        self, run_id: UUID, project_id: str, *, status: RunLifecycleStatus
    ) -> WorktreeRecord:
        record = self.get(run_id, project_id)
        if record is None:
            raise WorkspaceError("worktree_corrupt", f"no hay worktree para {run_id}")
        released = record.model_copy(
            update={"released_at": self._clock(), "release_status": status.value}
        )
        self._write_marker(released)
        return released

    def remove(self, run_id: UUID, project_id: str) -> bool:
        path = self.path_for(project_id, run_id)
        if not path.exists() and not path.is_symlink():
            return False
        record = self.record_at(path)
        repository = self.repositories.get(record.repository_id) if record is not None else None
        self._remove_path(path, repository)
        return True

    def sweep(self, now: datetime | None = None) -> WorktreeSweepReport:
        """Remove released worktrees past their retention and stale partial creations."""
        now = now or self._clock()
        report = WorktreeSweepReport()
        for project_dir in sorted(self._root.iterdir()):
            if project_dir.is_symlink() or not project_dir.is_dir():
                continue
            for run_dir in sorted(project_dir.iterdir()):
                if run_dir.is_symlink() or not run_dir.is_dir():
                    continue
                record = self.record_at(run_dir)
                if record is None:
                    age = now.timestamp() - run_dir.stat().st_mtime
                    if age >= self.policy.retention_seconds:
                        self._remove_path(run_dir, None, prune_project=project_dir.name)
                        report.orphans_removed.append(run_dir)
                    else:
                        report.retained.append(run_dir)
                    continue
                if not record.released or record.released_at is None:
                    report.retained.append(run_dir)
                    continue
                ttl = (
                    self.policy.interrupted_retention_seconds
                    if record.release_status == RunLifecycleStatus.INTERRUPTED.value
                    else self.policy.retention_seconds
                )
                if (now - record.released_at).total_seconds() >= ttl:
                    self._remove_path(run_dir, self.repositories.get(record.repository_id))
                    report.removed.append(run_dir)
                else:
                    report.retained.append(run_dir)
        return report

    def count(self, project_id: str) -> int:
        project_dir = self._root / project_id
        if not project_dir.is_dir():
            return 0
        return sum(1 for p in project_dir.iterdir() if p.is_dir() and not p.is_symlink())

    # -- internals ------------------------------------------------------------------------

    def _check_quota(self, project_id: str) -> None:
        if self.count(project_id) >= self.policy.max_per_project:
            raise WorkspaceError(
                "worktree_quota_exceeded",
                f"el proyecto {project_id} ya tiene {self.policy.max_per_project} worktrees",
            )
        free = shutil.disk_usage(self._root).free
        if free < self.policy.min_free_bytes:
            raise WorkspaceError(
                "disk_space_low", f"quedan {free} bytes libres; mínimo {self.policy.min_free_bytes}"
            )

    def _remove_path(
        self,
        path: Path,
        repository: RepositoryConfig | None,
        *,
        prune_project: str | None = None,
    ) -> None:
        resolved = self._inside_root(path)
        # rmtree does not follow symlinks inside the tree: files of other runs
        # reachable through a planted link are never touched.
        shutil.rmtree(resolved, ignore_errors=False)
        repositories = (
            [repository]
            if repository is not None
            else self.repositories.for_project(prune_project or "")
        )
        for repo in repositories:
            try:
                self.git.worktree_prune(repo)
            except GitOperationError as exc:  # the directory is gone either way
                logger.warning("No se pudo podar el repositorio %s: %s", repo.id, exc.detail)

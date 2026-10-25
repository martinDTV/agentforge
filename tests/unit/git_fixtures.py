"""Temporary, isolated Git repositories for the unit suite (never the host's repos).

Seeding uses the ``git`` binary with a scratch HOME and no global or system
configuration, so the operator's identity, credential helpers and hooks
never leak into the fixtures. Everything lives under ``tmp_path``.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.git import GitIdentity, RepositoryConfig, RepositoryRegistry
from app.infrastructure.git import LocalGitAdapter
from app.workspaces import WorktreeManager, WorktreePolicy

SEED_IDENTITY = ("Seed User", "seed@example.com")
DEFAULT_FILES = {"README.md": "# Demo\n", "src/app.py": "print('hi')\n"}


def seed_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": SEED_IDENTITY[0],
        "GIT_AUTHOR_EMAIL": SEED_IDENTITY[1],
        "GIT_COMMITTER_NAME": SEED_IDENTITY[0],
        "GIT_COMMITTER_EMAIL": SEED_IDENTITY[1],
    }


def git(*args: str, cwd: Path, home: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=seed_env(home),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def seed_repository(
    work: Path, *, home: Path, default_branch: str = "main", files: dict[str, str] | None = None
) -> Path:
    """A non-bare repository with one commit on ``default_branch``."""
    work.mkdir(parents=True)
    git("init", "-q", "-b", default_branch, cwd=work, home=home)
    for relative, content in (files or DEFAULT_FILES).items():
        target = work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git("add", "-A", cwd=work, home=home)
    git("commit", "-q", "-m", "seed", cwd=work, home=home)
    return work


def seed_bare_repository(
    repos_root: Path, name: str = "demo", *, home: Path, files: dict[str, str] | None = None
) -> Path:
    """A bare repository under ``repos_root`` seeded from a throwaway work tree."""
    work = seed_repository(home / f"{name}-seed", home=home, files=files)
    target = repos_root / f"{name}.git"
    git("clone", "-q", "--bare", str(work), str(target), cwd=repos_root, home=home)
    shutil.rmtree(work)
    return target


@dataclass
class GitSandbox:
    home: Path
    repos_root: Path
    worktrees_root: Path
    adapter: LocalGitAdapter
    registry: RepositoryRegistry
    config: RepositoryConfig
    manager: WorktreeManager

    def new_manager(self, **overrides: object) -> WorktreeManager:
        """A fresh manager over the same storage, as after a process restart."""
        return WorktreeManager(
            self.worktrees_root,
            self.adapter,
            self.registry,
            policy=overrides.get("policy", self.manager.policy),  # type: ignore[arg-type]
            clock=overrides.get("clock"),  # type: ignore[arg-type]
        )


def make_sandbox(
    tmp_path: Path,
    *,
    project: str = "alpha",
    repository: str = "demo",
    policy: WorktreePolicy | None = None,
    clock: "object | None" = None,
) -> GitSandbox:
    home = tmp_path / "home"
    repos_root = tmp_path / "repos"
    worktrees_root = tmp_path / "worktrees"
    home.mkdir()
    repos_root.mkdir()
    bare = seed_bare_repository(repos_root, repository, home=home)
    adapter = LocalGitAdapter(scratch_home=tmp_path / "git-home")
    config = RepositoryConfig(
        id=repository,
        project_id=project,
        path=bare,
        identity=GitIdentity(name="AgentForge", email="agentforge@noreply.agentforge.local"),
    )
    registry = RepositoryRegistry(repos_root, [config])
    manager = WorktreeManager(
        worktrees_root,
        adapter,
        registry,
        policy=policy,
        clock=clock,  # type: ignore[arg-type]
    )
    return GitSandbox(home, repos_root, worktrees_root, adapter, registry, config, manager)


def fixed_clock(moment: datetime) -> "Clock":
    def clock() -> datetime:
        return moment

    return clock


from collections.abc import Callable  # noqa: E402

Clock = Callable[[], datetime]

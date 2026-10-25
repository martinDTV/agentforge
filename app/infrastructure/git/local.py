"""``GitRepositoryPort`` over the local ``git`` binary.

Every call: argument list built here (never a shell), ``--end-of-options``
before anything a caller could influence, ``--`` before pathspecs, a working
directory the application chose, a timeout, and a sanitized environment:

- an empty ``HOME`` and ``GIT_CONFIG_GLOBAL=/dev/null`` so the operator's
  ``~/.gitconfig`` (credential helpers, signing keys, aliases) never applies;
- ``GIT_CONFIG_NOSYSTEM=1``, no ``SSH_AUTH_SOCK``, ``GIT_SSH_COMMAND=false``,
  ``GIT_ASKPASS=false`` and ``GIT_TERMINAL_PROMPT=0`` so nothing can reach a
  remote or ask for a credential;
- hooks disabled (``core.hooksPath`` points at an empty directory), external
  diff and textconv disabled, signing disabled.

Commits are authored by the repository's configured service identity.
"""

import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from app.git.errors import GitOperationError
from app.git.ports import (
    CommitInfo,
    DiffResult,
    FileChange,
    GitIdentity,
    RepositoryConfig,
    StatusEntry,
    StatusResult,
    WorktreeInfo,
)
from app.git.refs import (
    is_run_branch,
    validate_branch_name,
    validate_pathspec,
    validate_ref,
)
from app.tools.redaction import redact

_SEP = "\x1f"
_LOG_FORMAT = f"%H{_SEP}%h{_SEP}%an{_SEP}%ae{_SEP}%aI{_SEP}%s"
_MAX_STDERR = 500


def _false_binary() -> str:
    for candidate in ("/usr/bin/false", "/bin/false"):
        if os.path.exists(candidate):
            return candidate
    return "false"


class LocalGitAdapter:
    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 30.0,
        scratch_home: Path | None = None,
    ) -> None:
        resolved = binary or shutil.which("git")
        if resolved is None or not os.path.isfile(resolved):
            raise GitOperationError("git_unavailable", "no se encontró el binario git")
        self._git = resolved
        self._timeout = timeout_seconds
        home = scratch_home or Path(tempfile.mkdtemp(prefix="agentforge-git-home-"))
        home.mkdir(parents=True, exist_ok=True)
        self._home = home
        self._hooks = home / "no-hooks"
        self._hooks.mkdir(exist_ok=True)

    # -- plumbing -----------------------------------------------------------------------

    def _env(self, identity: GitIdentity | None = None) -> dict[str, str]:
        false = _false_binary()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self._home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": false,
            "GIT_SSH_COMMAND": false,
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
        if identity is not None:
            env.update(
                GIT_AUTHOR_NAME=identity.name,
                GIT_AUTHOR_EMAIL=identity.email,
                GIT_COMMITTER_NAME=identity.name,
                GIT_COMMITTER_EMAIL=identity.email,
            )
        return env

    def _run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        identity: GitIdentity | None = None,
        ok_codes: Sequence[int] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        command = [
            self._git,
            "-c",
            f"core.hooksPath={self._hooks}",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            "-c",
            "credential.helper=",
            "-c",
            "diff.external=",
            "-c",
            "core.fsmonitor=false",
            *args,
        ]
        try:
            completed = subprocess.run(  # noqa: S603 - argv built here, shell=False
                command,
                cwd=str(cwd),
                env=self._env(identity),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitOperationError(
                "git_timeout", f"git {args[0]} superó {self._timeout:g}s"
            ) from exc
        except OSError as exc:
            raise GitOperationError("git_failed", f"no se pudo ejecutar git: {exc}") from exc
        if completed.returncode not in ok_codes:
            stderr, _ = redact(completed.stderr.strip()[:_MAX_STDERR])
            raise GitOperationError(
                "git_failed", f"git {args[0]} falló ({completed.returncode}): {stderr}"
            )
        return completed

    @staticmethod
    def _parse_commits(stdout: str) -> list[CommitInfo]:
        commits: list[CommitInfo] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            sha, short, name, email, when, subject = line.split(_SEP, 5)
            commits.append(
                CommitInfo(
                    sha=sha,
                    short_sha=short,
                    author_name=name,
                    author_email=email,
                    authored_at=datetime.fromisoformat(when),
                    subject=subject,
                )
            )
        return commits

    # -- read -----------------------------------------------------------------------------

    def head(self, worktree: Path) -> CommitInfo:
        commits = self.log(worktree, max_count=1)
        if not commits:
            raise GitOperationError("no_commits", "el repositorio no tiene commits")
        return commits[0]

    def current_branch(self, worktree: Path) -> str | None:
        completed = self._run(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=worktree, ok_codes=(0, 1)
        )
        return completed.stdout.strip() or None

    def log(
        self,
        worktree: Path,
        *,
        ref: str | None = None,
        max_count: int = 20,
        path: str | None = None,
    ) -> list[CommitInfo]:
        args = ["log", f"--max-count={max(1, min(max_count, 1000))}", f"--format={_LOG_FORMAT}"]
        args += ["--end-of-options", validate_ref(ref) if ref else "HEAD", "--"]
        if path is not None:
            args.append(validate_pathspec(path))
        completed = self._run(args, cwd=worktree, ok_codes=(0, 128))
        if completed.returncode == 128:
            if (
                "does not have any commits" in completed.stderr
                or "bad revision" in completed.stderr
            ):
                if ref is None:
                    return []
                raise GitOperationError("ref_invalid", f"referencia desconocida: {ref}")
            stderr, _ = redact(completed.stderr.strip()[:_MAX_STDERR])
            raise GitOperationError("git_failed", f"git log falló: {stderr}")
        return self._parse_commits(completed.stdout)

    def diff(
        self,
        worktree: Path,
        *,
        base: str | None = None,
        target: str | None = None,
        path: str | None = None,
        max_bytes: int = 64_000,
    ) -> DiffResult:
        revisions = [validate_ref(base) if base else "HEAD"]
        if target is not None:
            revisions.append(validate_ref(target))
        pathspec = ["--", validate_pathspec(path)] if path is not None else ["--"]
        common = ["--no-color", "--no-ext-diff", "--no-textconv", "--end-of-options"]
        stat = self._run(["diff", "--numstat", *common, *revisions, *pathspec], cwd=worktree)
        files: list[FileChange] = []
        for line in stat.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, name = parts
            files.append(
                FileChange(
                    path=name,
                    additions=int(added) if added.isdigit() else None,
                    deletions=int(deleted) if deleted.isdigit() else None,
                )
            )
        patch = self._run(["diff", *common, *revisions, *pathspec], cwd=worktree).stdout
        encoded = patch.encode("utf-8")
        truncated = len(encoded) > max_bytes
        if truncated:
            patch = encoded[:max_bytes].decode("utf-8", errors="ignore")
        patch, _ = redact(patch)
        return DiffResult(
            base=revisions[0],
            target=revisions[1] if len(revisions) > 1 else "worktree",
            files=files,
            patch=patch,
            truncated=truncated,
        )

    def status(self, worktree: Path, *, max_entries: int = 500) -> StatusResult:
        completed = self._run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"],
            cwd=worktree,
        )
        entries: list[StatusEntry] = []
        truncated = False
        for chunk in completed.stdout.split("\x00"):
            if len(chunk) < 4:
                continue
            if len(entries) >= max_entries:
                truncated = True
                break
            entries.append(StatusEntry(path=chunk[3:], state=chunk[:2]))
        head: str | None
        try:
            head = self.head(worktree).sha
        except GitOperationError:
            head = None
        return StatusResult(
            branch=self.current_branch(worktree),
            head=head,
            clean=not entries and not truncated,
            entries=entries,
            truncated=truncated,
        )

    def rev_parse(self, repository: RepositoryConfig, ref: str) -> str:
        completed = self._run(
            [
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{validate_ref(ref)}^{{commit}}",
            ],
            cwd=repository.path,
            ok_codes=(0, 1),
        )
        sha = completed.stdout.strip()
        if completed.returncode != 0 or not sha:
            raise GitOperationError("ref_invalid", f"referencia desconocida: {ref}")
        return sha

    def branch_exists(self, repository: RepositoryConfig, branch: str) -> bool:
        completed = self._run(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{validate_branch_name(branch)}"],
            cwd=repository.path,
            ok_codes=(0, 1),
        )
        return completed.returncode == 0

    # -- worktrees ----------------------------------------------------------------------

    def worktree_add(
        self, repository: RepositoryConfig, path: Path, branch: str, *, start_point: str
    ) -> None:
        self._writable_branch(repository, branch)
        if not path.is_absolute():
            raise GitOperationError("path_outside_root", "la ruta del worktree debe ser absoluta")
        if self.branch_exists(repository, branch):
            args = ["worktree", "add", "--end-of-options", str(path), branch]
        else:
            args = [
                "worktree",
                "add",
                "-b",
                branch,
                "--end-of-options",
                str(path),
                validate_ref(start_point),
            ]
        self._run(args, cwd=repository.path)

    def worktree_list(self, repository: RepositoryConfig) -> list[WorktreeInfo]:
        completed = self._run(["worktree", "list", "--porcelain"], cwd=repository.path)
        worktrees: list[WorktreeInfo] = []
        current: dict[str, object] = {}
        for line in completed.stdout.splitlines() + [""]:
            if not line:
                if current:
                    worktrees.append(WorktreeInfo.model_validate(current))
                    current = {}
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                current["path"] = Path(value)
            elif key == "HEAD":
                current["head"] = value
            elif key == "branch":
                current["branch"] = value.removeprefix("refs/heads/")
            elif key == "bare":
                current["bare"] = True
            elif key == "prunable":
                current["prunable"] = True
        return worktrees

    def worktree_prune(self, repository: RepositoryConfig) -> None:
        self._run(["worktree", "prune"], cwd=repository.path)

    # -- write contracts ------------------------------------------------------------------

    @staticmethod
    def _writable_branch(repository: RepositoryConfig, branch: str) -> str:
        validate_branch_name(branch)
        if repository.is_protected(branch):
            raise GitOperationError("ref_protected", f"la rama '{branch}' está protegida")
        if not is_run_branch(branch):
            raise GitOperationError(
                "not_a_run_branch", f"solo se escribe en ramas run/<run_id>: {branch!r}"
            )
        return branch

    def create_branch(self, repository: RepositoryConfig, name: str, *, start_point: str) -> None:
        self._writable_branch(repository, name)
        self._run(
            ["branch", "--end-of-options", name, validate_ref(start_point)], cwd=repository.path
        )

    def commit(
        self,
        worktree: Path,
        repository: RepositoryConfig,
        message: str,
        *,
        paths: Sequence[str] | None = None,
    ) -> CommitInfo:
        if not message.strip() or len(message) > 2000:
            raise GitOperationError("git_failed", "mensaje de commit vacío o demasiado largo")
        branch = self.current_branch(worktree)
        if branch is None:
            raise GitOperationError("not_a_run_branch", "el worktree no está en una rama")
        self._writable_branch(repository, branch)
        pathspecs = [validate_pathspec(p) for p in paths] if paths else ["."]
        self._run(["add", "--all", "--", *pathspecs], cwd=worktree)
        self._run(
            [
                "commit",
                "--quiet",
                "--no-verify",
                "--no-gpg-sign",
                "-m",
                message,
                "--end-of-options",
            ],
            cwd=worktree,
            identity=repository.identity,
        )
        return self.head(worktree)

    def push(self, repository: RepositoryConfig, branch: str) -> None:
        # Remote writes are not part of this step: no remote is configured and
        # the adapter refuses before touching the binary.
        raise GitOperationError(
            "push_disabled", f"el push de '{branch}' está deshabilitado en esta fase"
        )

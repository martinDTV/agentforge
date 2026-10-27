"""The platform's only way to leave the host through Git (ADR 0011 §1, §5, §7).

``GitPublisher`` turns the run's worktree into exactly one commit on the local
``run/<run_id>`` branch, describes the diff for the pre-publication scan,
checks the base and pushes **only** ``<head_sha>:refs/heads/<prefix><run_id>``
to the mirror's ``origin`` with a credential handed through ``GIT_ASKPASS``.
Every guard is enforced here before Git is invoked: the destination must live
in the reserved prefix and never be a protected ref, the refspec is built from
validated parts (never a deletion, never tags), and ``--force-with-lease``
names the exact remote state expected (absent on the first attempt, our own
``head_sha`` on a retry), so nothing unexpected is ever overwritten.

Agents never reach this module: it is not a tool, the ``GitRepositoryPort``
they use keeps ``push`` disabled and their environment has no credential.
"""

import hashlib
import logging
import re
from collections.abc import Mapping
from pathlib import Path

from app.git import GitOperationError, RepositoryConfig
from app.git.refs import DEFAULT_PROTECTED_REFS, is_protected, validate_branch_name
from app.publishing import ChangedEntry, PublicationError
from app.publishing.plan import validate_branch_prefix

from .local import LocalGitAdapter

logger = logging.getLogger(__name__)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_RAW = re.compile(r"^:(\d{6}) (\d{6}) ([0-9a-f]{40}) ([0-9a-f]{40}) ([ADMT])(?:\d+)?$")
NULL_SHA = "0" * 40
BINARY_PROBE_BYTES = 8000
HTTPS_PROTOCOLS = ("protocol.allow=never", "protocol.https.allow=always")
PUSH_CONFIG = ("push.default=nothing", "push.followTags=false", "advice.pushUpdateRejected=false")


def _sha(value: str, what: str) -> str:
    if not _SHA.fullmatch(value):
        raise PublicationError("head_invalid", f"{what} no es un SHA válido")
    return value


class GitPublisher:
    def __init__(
        self,
        git: LocalGitAdapter,
        *,
        allow_file_remotes: bool = False,
        push_timeout_seconds: float = 120.0,
    ) -> None:
        self._git = git
        self._allow_file = allow_file_remotes
        self._push_timeout = push_timeout_seconds

    # -- local facts ------------------------------------------------------------------------

    def head_of(self, mirror: Path, branch: str) -> str | None:
        heads = self._git.branch_heads(mirror)
        return heads.get(validate_branch_name(branch))

    def is_ancestor(self, mirror: Path, ancestor: str, descendant: str) -> bool:
        completed = self._git._run(
            ["merge-base", "--is-ancestor", _sha(ancestor, "ancestro"), _sha(descendant, "cabeza")],
            cwd=mirror,
            ok_codes=(0, 1),
        )
        return completed.returncode == 0

    def commits_between(self, mirror: Path, base: str, head: str) -> int:
        completed = self._git._run(
            ["rev-list", "--count", f"{_sha(base, 'base')}..{_sha(head, 'cabeza')}"], cwd=mirror
        )
        return int(completed.stdout.strip() or 0)

    def parent_of(self, mirror: Path, sha: str) -> list[str]:
        completed = self._git._run(
            ["rev-list", "--parents", "-n", "1", _sha(sha, "commit")], cwd=mirror
        )
        parts = completed.stdout.split()
        return parts[1:]

    def commit_run_branch(
        self, worktree: Path, repository: RepositoryConfig, *, base_sha: str, message: str
    ) -> str:
        """Exactly one commit on top of ``base_sha`` holding the worktree's changes.

        Idempotent: a worktree already committed (clean, one commit ahead of the
        base, no merges) is returned as is; a dirty worktree is committed with
        the repository's service identity; anything else is ``head_invalid``.
        """
        status = self._git.status(worktree)
        head = self._git.head(worktree).sha
        dirty = bool(status.entries)
        if not dirty:
            if head == base_sha:
                raise PublicationError("head_invalid", "no hay cambios que publicar")
        else:
            if head != base_sha:
                raise PublicationError(
                    "head_invalid", "el worktree tiene commits y cambios sin confirmar a la vez"
                )
            head = self._git.commit(worktree, repository, message).sha
        mirror = repository.path
        parents = self.parent_of(mirror, head)
        if parents != [base_sha] or self.commits_between(mirror, base_sha, head) != 1:
            raise PublicationError(
                "head_invalid", "la cabeza debe ser un único commit cuyo padre es la base"
            )
        return head

    def patch_digest(self, mirror: Path, base: str, head: str) -> str:
        completed = self._git._run(
            [
                "diff-tree",
                "-p",
                "--binary",
                "--full-index",
                "--no-color",
                "--no-ext-diff",
                "--no-renames",
                "-r",
                _sha(base, "base"),
                _sha(head, "cabeza"),
            ],
            cwd=mirror,
        )
        return hashlib.sha256(completed.stdout.encode("utf-8", "surrogateescape")).hexdigest()

    def changed_entries(self, mirror: Path, base: str, head: str) -> list[ChangedEntry]:
        """One ``ChangedEntry`` per path, with the facts the scan needs (fail closed on doubt)."""
        completed = self._git._run(
            [
                "diff-tree",
                "--raw",
                "--no-renames",
                "-r",
                "-z",
                _sha(base, "base"),
                _sha(head, "cabeza"),
            ],
            cwd=mirror,
        )
        fields = completed.stdout.split("\0")
        entries: list[ChangedEntry] = []
        index = 0
        while index < len(fields) and fields[index]:
            match = _RAW.match(fields[index])
            path = fields[index + 1] if index + 1 < len(fields) else ""
            index += 2
            if match is None or not path:
                raise PublicationError("head_invalid", "diff ilegible")
            old_mode, new_mode, old_blob, new_blob, status = match.groups()
            change = {"A": "added", "M": "modified", "D": "deleted", "T": "modified"}[status]
            size: int | None = None
            is_binary: bool | None = None
            digest: str | None = None
            added: tuple[str, ...] = ()
            if change != "deleted" and new_blob != NULL_SHA and new_mode not in ("160000",):
                blob = self._git._run(["cat-file", "blob", new_blob], cwd=mirror)
                raw = blob.stdout.encode("utf-8", "surrogateescape")
                size = len(raw)
                is_binary = b"\0" in raw[:BINARY_PROBE_BYTES]
                digest = hashlib.sha256(raw).hexdigest()
                if not is_binary:
                    added = self._added_lines(mirror, base, head, path)
            entries.append(
                ChangedEntry(
                    path=path,
                    change=change,  # type: ignore[arg-type]
                    old_mode=None if old_mode == "000000" else old_mode,
                    new_mode=None if new_mode == "000000" else new_mode,
                    size=size,
                    is_binary=is_binary,
                    added_lines=added,
                    sha256=digest,
                )
            )
        return entries

    def _added_lines(self, mirror: Path, base: str, head: str, path: str) -> tuple[str, ...]:
        completed = self._git._run(
            [
                "diff-tree",
                "-p",
                "-U0",
                "--no-color",
                "--no-ext-diff",
                "--no-renames",
                "-r",
                base,
                head,
                "--",
                path,
            ],
            cwd=mirror,
        )
        return tuple(
            line[1:]
            for line in completed.stdout.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )

    # -- remote ------------------------------------------------------------------------------

    def remote_branch_head(
        self, mirror: Path, remote_branch: str, *, credential_env: Mapping[str, str] | None
    ) -> str | None:
        """What ``origin`` holds under ``refs/heads/<remote_branch>`` right now, or ``None``."""
        ref = f"refs/heads/{self._branch(remote_branch)}"
        completed = self._git._run(
            ["ls-remote", "--heads", "--", "origin", ref],
            cwd=mirror,
            env_extra=credential_env,
            timeout=self._push_timeout,
            config=self._protocols(),
        )
        for line in completed.stdout.splitlines():
            sha, _, name = line.strip().partition("\t")
            if name == ref and _SHA.fullmatch(sha):
                return sha
        return None

    def push_run_branch(
        self,
        mirror: Path,
        repository: RepositoryConfig,
        *,
        head_sha: str,
        remote_branch: str,
        branch_prefix: str,
        expected_remote_sha: str | None,
        credential_env: Mapping[str, str] | None,
    ) -> None:
        """Push exactly ``head_sha`` to ``refs/heads/<remote_branch>`` on ``origin``.

        Refused before Git runs unless the branch lives in the reserved prefix
        and is not protected. ``expected_remote_sha`` is what the remote must
        hold (``None`` = the branch must not exist); any other state fails the
        lease and nothing is overwritten. A remote branch that already holds
        exactly ``head_sha`` makes the push a no-op (idempotent retry).
        """
        validate_branch_prefix(branch_prefix)
        branch = self._branch(remote_branch)
        if not branch.startswith(branch_prefix) or branch == branch_prefix:
            raise PublicationError(
                "remote_branch_invalid", f"'{branch}' no está bajo el prefijo reservado"
            )
        if is_protected(branch, DEFAULT_PROTECTED_REFS | repository.protected):
            raise PublicationError("remote_branch_invalid", f"'{branch}' es una rama protegida")
        if branch.startswith("run/") or branch in (repository.default_branch, "HEAD"):
            raise PublicationError("remote_branch_invalid", f"'{branch}' no es publicable")
        _sha(head_sha, "cabeza")
        if expected_remote_sha is not None:
            _sha(expected_remote_sha, "estado remoto esperado")
        ref = f"refs/heads/{branch}"
        refspec = f"{head_sha}:{ref}"
        assert not refspec.startswith(":") and "*" not in refspec
        lease = f"--force-with-lease={ref}:{expected_remote_sha or ''}"
        try:
            self._git._run(
                [
                    "push",
                    "--quiet",
                    "--atomic",
                    "--no-verify",
                    "--no-tags",
                    "--no-follow-tags",
                    lease,
                    "--",
                    "origin",
                    refspec,
                ],
                cwd=mirror,
                env_extra=credential_env,
                timeout=self._push_timeout,
                config=(*self._protocols(), *PUSH_CONFIG),
            )
        except GitOperationError as exc:
            if exc.code == "git_timeout":
                raise PublicationError("publication_timeout", exc.detail, retryable=True) from exc
            lowered = exc.detail.lower()
            if "stale info" in lowered or "force-with-lease" in lowered or "rejected" in lowered:
                raise PublicationError(
                    "remote_branch_conflict",
                    "la rama remota no está en el estado esperado; no se sobrescribe",
                ) from exc
            raise PublicationError("publication_failed", exc.detail, retryable=True) from exc
        logger.info("Rama %s publicada en %s (%s)", branch, repository.id, head_sha[:12])

    @staticmethod
    def _branch(name: str) -> str:
        try:
            return validate_branch_name(name)
        except GitOperationError as exc:
            raise PublicationError("remote_branch_invalid", exc.detail) from exc

    def _protocols(self) -> tuple[str, ...]:
        if self._allow_file:
            return (*HTTPS_PROTOCOLS, "protocol.file.allow=always")
        return HTTPS_PROTOCOLS

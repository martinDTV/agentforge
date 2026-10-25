"""Deterministic, bounded archive of a work tree for the sandbox.

Never includes version-control internals, caches, symlinks or files whose
names conventionally hold credentials. The same tree always yields the same
bytes (fixed metadata, sorted entries, gzip without timestamps), so the
digest in the request identifies exactly what ran.
"""

import gzip
import hashlib
import io
import os
import stat
import tarfile
from pathlib import Path

from app.workspaces.sensitive import DENIED_DIRECTORIES, is_sensitive_name

from .errors import SandboxError
from .ports import WorkspaceArchive

SKIPPED_DIRECTORIES: frozenset[str] = DENIED_DIRECTORIES | {
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
}


def pack_worktree(
    root: Path, *, max_bytes: int, max_file_bytes: int = 8 * 1024 * 1024
) -> WorkspaceArchive:
    root = root.resolve()
    buffer = io.BytesIO()
    total = 0
    count = 0
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(
                d for d in dirnames if d not in SKIPPED_DIRECTORIES and not _is_link(dirpath, d)
            )
            for name in sorted(filenames):
                full = Path(dirpath) / name
                # In a linked worktree ``.git`` is a file pointing at the host
                # repository: it never ships either.
                if full.is_symlink() or name in SKIPPED_DIRECTORIES or is_sensitive_name(name):
                    continue
                info = full.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > max_file_bytes:
                    continue
                total += info.st_size
                if total > max_bytes:
                    raise SandboxError(
                        "archive_too_large",
                        f"el worktree supera {max_bytes} bytes; máximo para el sandbox",
                    )
                entry = tarfile.TarInfo(name=full.relative_to(root).as_posix())
                entry.size = info.st_size
                entry.mtime = 0
                entry.uid = entry.gid = 0
                entry.uname = entry.gname = ""
                entry.mode = 0o755 if info.st_mode & stat.S_IXUSR else 0o644
                with full.open("rb") as handle:
                    tar.addfile(entry, handle)
                count += 1
    content = buffer.getvalue()
    return WorkspaceArchive(
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        file_count=count,
        content=content,
    )


def _is_link(dirpath: str, name: str) -> bool:
    return os.path.islink(os.path.join(dirpath, name))

"""Controlled inspection of the workspace archive, without writing anything.

The runner never extracts the untrusted tree on its own disk: it scans the
archive, refuses anything that is not a regular file or a directory with a
relative, normalized name, and then streams the very same bytes into the
container, where a fixed bootstrap extracts them into a size-capped tmpfs
with the stdlib ``data`` filter as a second net. Refused here: absolute
names, ``..``, symlinks, hardlinks, devices, FIFOs, duplicates, oversized
members, too many members and more decompressed bytes than allowed.
"""

import hashlib
import io
import tarfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .errors import RunnerError


@dataclass(frozen=True)
class ExtractionLimits:
    max_files: int
    max_file_bytes: int
    max_total_bytes: int


@dataclass(frozen=True)
class ArchiveManifest:
    files: frozenset[str]
    directories: frozenset[str]
    bytes: int
    executables: frozenset[str] = field(default_factory=frozenset)

    @property
    def count(self) -> int:
        return len(self.files) + len(self.directories)

    def contains(self, path: str) -> bool:
        """A requested program path: a file, a directory, or a directory implied by a file."""
        if path == ".":
            return True
        if path in self.files or path in self.directories:
            return True
        prefix = path.rstrip("/") + "/"
        return any(name.startswith(prefix) for name in self.files)


def verify_archive(
    data: bytes, *, expected_sha256: str, expected_size: int, max_bytes: int
) -> None:
    if len(data) > max_bytes:
        raise RunnerError(
            "archive_too_large", f"archivo de {len(data)} bytes; máximo {max_bytes}", status=413
        )
    if len(data) != expected_size:
        raise RunnerError(
            "archive_mismatch", "el tamaño del archivo no coincide con la solicitud", status=422
        )
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise RunnerError(
            "archive_mismatch", "el digest del archivo no coincide con la solicitud", status=422
        )


def _reject(detail: str) -> RunnerError:
    return RunnerError("archive_rejected", detail, status=422)


def safe_relative(name: str) -> PurePosixPath:
    if not name or "\x00" in name:
        raise _reject("entrada sin nombre o con NUL")
    if name.startswith(("/", "\\")) or "\\" in name:
        raise _reject(f"ruta no admitida en el archivo: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise _reject(f"ruta no admitida en el archivo: {name!r}")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts:
        raise _reject(f"ruta vacía en el archivo: {name!r}")
    if ":" in parts[0] and len(parts[0]) == 2:  # C: on Windows-created archives
        raise _reject(f"ruta no admitida en el archivo: {name!r}")
    return PurePosixPath(*parts)


def scan_archive(data: bytes, limits: ExtractionLimits) -> ArchiveManifest:
    files: set[str] = set()
    directories: set[str] = set()
    executables: set[str] = set()
    seen: set[str] = set()
    total = 0
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise _reject(f"archivo ilegible: {type(exc).__name__}") from exc
    try:
        with tar:
            for member in tar:
                if len(seen) >= limits.max_files:
                    raise _reject(f"más de {limits.max_files} entradas")
                relative = safe_relative(member.name).as_posix()
                if relative in seen:
                    raise _reject(f"entrada duplicada: {member.name!r}")
                seen.add(relative)
                if member.isdir():
                    directories.add(relative)
                    continue
                if not member.isreg():
                    raise _reject(f"tipo de entrada no admitido: {member.name!r}")
                if member.size < 0 or member.size > limits.max_file_bytes:
                    raise _reject(f"archivo demasiado grande: {member.name!r}")
                total += member.size
                if total > limits.max_total_bytes:
                    raise _reject(
                        f"el contenido descomprimido supera {limits.max_total_bytes} bytes"
                    )
                files.add(relative)
                if member.mode & 0o111:
                    executables.add(relative)
                for parent in PurePosixPath(relative).parents:
                    if parent.as_posix() != ".":
                        directories.add(parent.as_posix())
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise _reject(f"archivo ilegible: {type(exc).__name__}") from exc
    if files & directories:
        raise _reject("una entrada es a la vez archivo y directorio")
    return ArchiveManifest(
        files=frozenset(files),
        directories=frozenset(directories),
        bytes=total,
        executables=frozenset(executables),
    )

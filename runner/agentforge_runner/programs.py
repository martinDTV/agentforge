"""The runner's own allowlist: authoritative, independent of the API's.

The API composes argv; the runner re-derives what the argv must look like
for the requested program and refuses anything else: another prefix, an
unknown flag, an option-shaped path, a missing ``--`` separator.
"""

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .errors import RunnerError
from .protocol import ProgramId

if TYPE_CHECKING:
    from .extract import ArchiveManifest

PREFIXES: dict[ProgramId, tuple[str, ...]] = {
    ProgramId.RUFF_CHECK: ("ruff", "check", "--no-cache", "--output-format", "concise"),
    ProgramId.RUFF_FORMAT_CHECK: ("ruff", "format", "--check", "--no-cache"),
    ProgramId.PYTEST: ("python", "-I", "-P", "-m", "pytest", "-q", "-p", "no:cacheprovider"),
}
_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_KEYWORD_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ ()-]{0,99}$")


def validate_argv(program: ProgramId, argv: list[str]) -> list[str]:
    """Return the validated paths or raise ``argv_rejected``."""
    prefix = PREFIXES[program]
    if tuple(argv[: len(prefix)]) != prefix:
        raise RunnerError("argv_rejected", "argv no coincide con el programa permitido", status=422)
    rest = list(argv[len(prefix) :])
    if program is ProgramId.PYTEST:
        if len(rest) >= 2 and rest[0] == "-k":
            keyword = rest[1]
            if not _KEYWORD_RE.fullmatch(keyword) or "--" in keyword:
                raise RunnerError("argv_rejected", "expresión -k no admitida", status=422)
            rest = rest[2:]
        if rest and rest[0] == "-x":
            rest = rest[1:]
    if not rest or rest[0] != "--":
        raise RunnerError("argv_rejected", "falta el separador -- antes de las rutas", status=422)
    paths = rest[1:]
    if not paths:
        raise RunnerError("argv_rejected", "sin rutas", status=422)
    for path in paths:
        if path == "." and program is not ProgramId.PYTEST:
            continue
        if not _PATH_RE.fullmatch(path) or ".." in PurePosixPath(path).parts:
            raise RunnerError("argv_rejected", f"ruta no admitida: {path!r}", status=422)
    return paths


def ensure_paths_in_manifest(paths: list[str], manifest: "ArchiveManifest") -> None:
    for path in paths:
        if not manifest.contains(path):
            raise RunnerError(
                "path_rejected", f"ruta ausente del workspace enviado: {path}", status=422
            )

"""Path confinement for workspace tools.

Every path a model supplies is relative to the run's workspace root and is
resolved with symlinks followed; the result must still live under the root.
Version-control internals and files that conventionally hold credentials
are refused by name, before anything is opened.
"""

from pathlib import Path, PurePosixPath

from app.workspaces.sensitive import (
    DENIED_DIRECTORIES,
    SENSITIVE_NAME_EXCEPTIONS,
    SENSITIVE_NAME_PATTERNS,
    is_sensitive_name,
)

from .errors import ToolExecutionError

__all__ = [
    "DENIED_DIRECTORIES",
    "SENSITIVE_NAME_EXCEPTIONS",
    "SENSITIVE_NAME_PATTERNS",
    "is_sensitive_name",
    "is_under_denied_directory",
    "resolve_inside",
    "validate_relative",
]


def validate_relative(requested: str) -> PurePosixPath:
    """A relative POSIX path with no NUL, no absolute form and no ``..``."""
    if "\x00" in requested:
        raise ToolExecutionError("path_denied", "la ruta contiene un byte nulo")
    rel = PurePosixPath(requested or ".")
    if rel.is_absolute() or requested.startswith(("/", "\\", "~")):
        raise ToolExecutionError("path_outside_workspace", "solo se admiten rutas relativas")
    if any(part == ".." for part in rel.parts):
        raise ToolExecutionError("path_outside_workspace", "la ruta no puede contener '..'")
    return rel


def resolve_inside(root: Path, requested: str) -> Path:
    """Resolve ``requested`` under ``root`` or raise a ``ToolExecutionError``.

    The root is resolved too, so a workspace that itself lives behind a
    symlink (``/tmp`` on macOS) is handled consistently.
    """
    rel = validate_relative(requested)
    try:
        root_resolved = root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ToolExecutionError("workspace_unavailable", "el workspace no existe") from exc
    candidate = root_resolved.joinpath(*rel.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ToolExecutionError("path_not_found", f"no existe: {requested}") from exc
    except (RuntimeError, OSError) as exc:  # symlink loops, permission errors
        raise ToolExecutionError("path_denied", f"ruta no accesible: {requested}") from exc
    if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
        raise ToolExecutionError(
            "path_outside_workspace", f"la ruta sale del workspace: {requested}"
        )
    relative = resolved.relative_to(root_resolved)
    if any(part in DENIED_DIRECTORIES for part in relative.parts):
        raise ToolExecutionError("path_denied", f"ruta reservada: {requested}")
    if relative.parts and is_sensitive_name(resolved.name):
        raise ToolExecutionError("path_denied", f"archivo sensible: {requested}")
    return resolved


def is_under_denied_directory(relative_parts: tuple[str, ...]) -> bool:
    return any(part in DENIED_DIRECTORIES for part in relative_parts)

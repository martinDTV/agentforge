"""Validation of branch names, refs and pathspecs before they reach ``git``.

Every value a model could influence passes through here. The rules are
stricter than ``git check-ref-format``: no leading dash (option injection),
no ``..``, no ``@{``, no ``.lock`` suffix, no control characters. Protected
refs are matched with shell patterns; tags are always protected because no
operation on them exists.
"""

import re
from collections.abc import Iterable
from fnmatch import fnmatchcase
from uuid import UUID

from .errors import GitOperationError

RUN_BRANCH_PREFIX = "run/"
DEFAULT_PROTECTED_REFS: frozenset[str] = frozenset({"main", "master", "release/*", "HEAD"})

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^-]{0,199}$")
_RUN_BRANCH_RE = re.compile(r"^run/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_FORBIDDEN_FRAGMENTS = ("..", "@{", "//", "/.", "\\")


def _common_checks(value: str, *, what: str) -> None:
    if not value or value.startswith("-") or value.endswith(("/", ".", ".lock")):
        raise GitOperationError("ref_invalid", f"{what} inválida: {value!r}")
    if any(fragment in value for fragment in _FORBIDDEN_FRAGMENTS):
        raise GitOperationError("ref_invalid", f"{what} inválida: {value!r}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise GitOperationError("ref_invalid", f"{what} con caracteres de control")


def validate_branch_name(name: str) -> str:
    """A plain branch name (no ``refs/`` prefix, no revision syntax)."""
    _common_checks(name, what="rama")
    if not _BRANCH_RE.fullmatch(name) or name.startswith("refs/"):
        raise GitOperationError("ref_invalid", f"rama inválida: {name!r}")
    return name


def validate_ref(name: str) -> str:
    """A read-only revision: branch, tag, SHA, ``HEAD``, ``HEAD~2``, ``main^``."""
    _common_checks(name, what="referencia")
    if not _REF_RE.fullmatch(name):
        raise GitOperationError("ref_invalid", f"referencia inválida: {name!r}")
    return name


def validate_pathspec(path: str) -> str:
    """A relative path inside the work tree, never an option or a parent escape."""
    if not path or "\x00" in path or path.startswith(("-", "/", "~", ":")):
        raise GitOperationError("pathspec_invalid", f"ruta inválida: {path!r}")
    if any(part == ".." for part in path.split("/")):
        raise GitOperationError("pathspec_invalid", f"ruta inválida: {path!r}")
    return path


def run_branch(run_id: UUID) -> str:
    return f"{RUN_BRANCH_PREFIX}{run_id}"


def is_run_branch(name: str) -> bool:
    return _RUN_BRANCH_RE.fullmatch(name) is not None


def is_protected(name: str, patterns: Iterable[str]) -> bool:
    """Branch names and tags that the gateway must never write to."""
    short = name.removeprefix("refs/heads/")
    if name.startswith(("refs/tags/", "tags/")):
        return True
    return any(fnmatchcase(short, pattern) for pattern in patterns)

"""The programs a sandbox may run, and how their argv is built.

There is no "command" field anywhere: a model picks a program id and typed
options, and the application assembles argv from a fixed prefix plus
validated paths. No shell, no operators, no flags from the model. Every
program counts as untrusted code, pytest included: it imports and executes
whatever the repository contains.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from .errors import SandboxError


class ProgramId(StrEnum):
    RUFF_CHECK = "ruff_check"
    RUFF_FORMAT_CHECK = "ruff_format_check"
    PYTEST = "pytest"


@dataclass(frozen=True, slots=True)
class ProgramSpec:
    id: ProgramId
    description: str
    argv_prefix: tuple[str, ...]
    profile: str = "python-3.13"
    # pytest may only be pointed at the repository's authorized test paths.
    restricted_to_test_paths: bool = False
    accepts_keyword: bool = False
    accepts_fail_fast: bool = False


PROGRAMS: dict[ProgramId, ProgramSpec] = {
    ProgramId.RUFF_CHECK: ProgramSpec(
        id=ProgramId.RUFF_CHECK,
        description="ruff check sobre rutas del worktree",
        argv_prefix=("ruff", "check", "--no-cache", "--output-format", "concise"),
    ),
    ProgramId.RUFF_FORMAT_CHECK: ProgramSpec(
        id=ProgramId.RUFF_FORMAT_CHECK,
        description="ruff format --check sobre rutas del worktree",
        argv_prefix=("ruff", "format", "--check", "--no-cache"),
    ),
    ProgramId.PYTEST: ProgramSpec(
        id=ProgramId.PYTEST,
        description="pytest sobre rutas de pruebas autorizadas (código no confiable)",
        argv_prefix=("python", "-I", "-P", "-m", "pytest", "-q", "-p", "no:cacheprovider"),
        restricted_to_test_paths=True,
        accepts_keyword=True,
        accepts_fail_fast=True,
    ),
}

_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_KEYWORD_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ ()-]{0,99}$")


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    program: ProgramId
    profile: str
    paths: list[str]
    argv: list[str]


def validate_program_path(path: str, *, worktree: Path) -> str:
    """A relative path that exists under the work tree, with no option-like shape."""
    if path == ".":
        return path
    if not _PATH_RE.fullmatch(path) or ".." in PurePosixPath(path).parts:
        raise SandboxError("path_invalid", f"ruta no admitida: {path!r}")
    root = worktree.resolve()
    try:
        resolved = (root / path).resolve(strict=True)
    except FileNotFoundError as exc:
        raise SandboxError("path_not_found", f"no existe en el worktree: {path}") from exc
    except (OSError, RuntimeError) as exc:
        raise SandboxError("path_invalid", f"ruta no accesible: {path}") from exc
    if resolved != root and not resolved.is_relative_to(root):
        raise SandboxError("path_invalid", f"la ruta sale del worktree: {path}")
    return path


def _under_authorized(path: str, roots: Iterable[str]) -> bool:
    parts = PurePosixPath(path).parts
    for root in roots:
        root_parts = PurePosixPath(root).parts
        if parts[: len(root_parts)] == root_parts:
            return True
    return False


def build_plan(
    program: ProgramId,
    paths: Sequence[str],
    *,
    worktree: Path,
    authorized_test_paths: Iterable[str],
    keyword: str | None = None,
    fail_fast: bool = False,
) -> ExecutionPlan:
    spec = PROGRAMS.get(program)
    if spec is None:
        raise SandboxError("program_unknown", f"programa no permitido: {program}")
    authorized = sorted(authorized_test_paths)
    chosen = list(paths) if paths else (authorized if spec.restricted_to_test_paths else ["."])
    if len(chosen) > 50:
        raise SandboxError("path_invalid", "demasiadas rutas")
    validated = [validate_program_path(p, worktree=worktree) for p in chosen]
    if spec.restricted_to_test_paths:
        for path in validated:
            if path == "." or not _under_authorized(path, authorized):
                raise SandboxError(
                    "path_not_authorized",
                    f"{path!r} no está entre las rutas de pruebas autorizadas {authorized}",
                )
    options: list[str] = []
    if keyword is not None:
        if not spec.accepts_keyword:
            raise SandboxError("keyword_invalid", f"{program} no admite una expresión -k")
        if not _KEYWORD_RE.fullmatch(keyword) or "--" in keyword:
            raise SandboxError("keyword_invalid", f"expresión -k no admitida: {keyword!r}")
        options += ["-k", keyword]
    if fail_fast:
        if not spec.accepts_fail_fast:
            raise SandboxError("keyword_invalid", f"{program} no admite fail_fast")
        options.append("-x")
    return ExecutionPlan(
        program=program,
        profile=spec.profile,
        paths=validated,
        argv=[*spec.argv_prefix, *options, "--", *validated],
    )

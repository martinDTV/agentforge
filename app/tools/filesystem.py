"""Read-only workspace tools (Paso 1): list, read and find inside the run's root.

They never write, never execute, never leave ``context.workspace_root``.
Outputs are bounded (entries, bytes) and file contents pass through secret
redaction before they are returned.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.tools import RiskClass

from .errors import ToolExecutionError
from .limits import ToolLimits
from .paths import is_under_denied_directory, resolve_inside, validate_relative
from .ports import ToolContext, ToolSpec
from .redaction import redact

_BINARY_SNIFF_BYTES = 8_000
_MAX_PATH_LENGTH = 1_024


def _root(context: ToolContext) -> Path:
    if context.workspace_root is None:
        raise ToolExecutionError(
            "workspace_unavailable", "esta ejecución no tiene un workspace asignado"
        )
    return context.workspace_root


def _display(root: Path, resolved: Path) -> str:
    relative = resolved.relative_to(root.resolve())
    return "." if not relative.parts else relative.as_posix()


# -- workspace.list_dir ---------------------------------------------------------------


class ListDirInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default=".", max_length=_MAX_PATH_LENGTH)


EntryKind = Literal["file", "dir", "symlink", "other"]


class DirEntry(BaseModel):
    name: str
    kind: EntryKind
    size: int | None = None


class ListDirOutput(BaseModel):
    path: str
    entries: list[DirEntry]
    truncated: bool = False


class ListDirTool:
    spec = ToolSpec(
        name="workspace.list_dir",
        description="Lista las entradas de un directorio del workspace (sin recursión).",
        risk=RiskClass.READ,
        input_model=ListDirInput,
        output_model=ListDirOutput,
    )

    def __init__(self, limits: ToolLimits) -> None:
        self._limits = limits

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        args = ListDirInput.model_validate(arguments, from_attributes=True)
        root = _root(context)
        target = resolve_inside(root, args.path)
        if not target.is_dir():
            raise ToolExecutionError("not_a_directory", f"no es un directorio: {args.path}")
        entries: list[DirEntry] = []
        truncated = False
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            if child.name in {".git", ".hg", ".svn"}:
                continue
            if len(entries) >= self._limits.max_entries:
                truncated = True
                break
            entries.append(_entry(child))
        return ListDirOutput(path=_display(root, target), entries=entries, truncated=truncated)


def _entry(path: Path) -> DirEntry:
    if path.is_symlink():
        return DirEntry(name=path.name, kind="symlink")
    if path.is_dir():
        return DirEntry(name=path.name, kind="dir")
    if path.is_file():
        return DirEntry(name=path.name, kind="file", size=path.stat().st_size)
    return DirEntry(name=path.name, kind="other")


# -- workspace.read_file --------------------------------------------------------------


class ReadFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=_MAX_PATH_LENGTH)
    # A model may ask for less than the platform limit, never for more.
    max_bytes: int | None = Field(default=None, ge=1)


class ReadFileOutput(BaseModel):
    path: str
    content: str
    size: int
    truncated: bool = False
    redactions: int = 0


class ReadFileTool:
    spec = ToolSpec(
        name="workspace.read_file",
        description=(
            "Lee un archivo de texto del workspace, acotado en bytes y con secretos redactados."
        ),
        risk=RiskClass.READ,
        input_model=ReadFileInput,
        output_model=ReadFileOutput,
    )

    def __init__(self, limits: ToolLimits) -> None:
        self._limits = limits

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        args = ReadFileInput.model_validate(arguments, from_attributes=True)
        root = _root(context)
        target = resolve_inside(root, args.path)
        if not target.is_file():
            raise ToolExecutionError("not_a_file", f"no es un archivo regular: {args.path}")
        cap = min(args.max_bytes or self._limits.max_read_bytes, self._limits.max_read_bytes)
        size = target.stat().st_size
        with target.open("rb") as handle:
            raw = handle.read(cap + 1)
        if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
            raise ToolExecutionError("binary_file", f"archivo binario: {args.path}")
        truncated = len(raw) > cap
        text = raw[:cap].decode("utf-8", errors="replace")
        content, redactions = redact(text)
        return ReadFileOutput(
            path=_display(root, target),
            content=content,
            size=size,
            truncated=truncated,
            redactions=redactions,
        )


# -- workspace.find -------------------------------------------------------------------


class FindInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1, max_length=256, description="Glob relativo, p. ej. **/*.py")
    path: str = Field(default=".", max_length=_MAX_PATH_LENGTH)


class FindOutput(BaseModel):
    path: str
    matches: list[str]
    truncated: bool = False


class FindTool:
    spec = ToolSpec(
        name="workspace.find",
        description="Busca rutas por patrón glob dentro del workspace, acotado en resultados.",
        risk=RiskClass.READ,
        input_model=FindInput,
        output_model=FindOutput,
    )

    def __init__(self, limits: ToolLimits) -> None:
        self._limits = limits

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        args = FindInput.model_validate(arguments, from_attributes=True)
        root = _root(context)
        base = resolve_inside(root, args.path)
        if not base.is_dir():
            raise ToolExecutionError("not_a_directory", f"no es un directorio: {args.path}")
        validate_relative(args.pattern)  # no absolute globs, no ".."
        root_resolved = root.resolve()
        matches: list[str] = []
        truncated = False
        scanned = 0
        scan_cap = self._limits.max_entries * 20
        for found in base.glob(args.pattern):
            scanned += 1
            if scanned > scan_cap:
                truncated = True
                break
            try:
                resolved = found.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not resolved.is_relative_to(root_resolved):
                continue  # symlink pointing outside: not reported
            relative = resolved.relative_to(root_resolved)
            if is_under_denied_directory(relative.parts):
                continue
            if len(matches) >= self._limits.max_entries:
                truncated = True
                break
            matches.append(relative.as_posix())
        matches.sort()
        return FindOutput(path=_display(root, base), matches=matches, truncated=truncated)


def workspace_tools(limits: ToolLimits) -> list[ListDirTool | ReadFileTool | FindTool]:
    return [ListDirTool(limits), ReadFileTool(limits), FindTool(limits)]

"""Bounded atomic writes through an audited, managed worktree; no shell or patch parser."""

import hashlib
import os
import stat
import threading
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.tools import RiskClass
from app.security.development import DevelopmentScope
from app.workspaces import WorktreeManager

from .errors import ToolExecutionError
from .managed import managed_worktree
from .paths import DENIED_DIRECTORIES, is_sensitive_name, validate_relative
from .ports import ToolContext, ToolSpec
from .redaction import redact


class WriteFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=32768)
    expected_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")


class WriteFileOutput(BaseModel):
    path: str
    sha256: str
    bytes_written: int


class WriteFileTool:
    spec = ToolSpec(
        name="workspace.write_file",
        description="Reemplaza un archivo autorizado con control de versión por hash.",
        risk=RiskClass.WRITE,
        input_model=WriteFileInput,
        output_model=WriteFileOutput,
    )

    def __init__(self, workspaces: WorktreeManager, scope: DevelopmentScope) -> None:
        self.workspaces, self.scope = workspaces, scope
        self._lock = threading.Lock()

    def run(self, context: ToolContext, arguments: BaseModel) -> WriteFileOutput:
        args = WriteFileInput.model_validate(arguments, from_attributes=True)
        record, repository = managed_worktree(self.workspaces, context)
        if context.invocation is None or not self.scope.permits(context):
            raise ToolExecutionError(
                "write_not_authorized", "se requiere autorización vigente y Gateway"
            )
        relative = validate_relative(args.path)
        if (
            repository.id != self.scope.repository_id
            or args.path not in self.scope.writable_paths
            or relative.as_posix() != args.path
            or len(relative.parts) == 0
            or any(p in DENIED_DIRECTORIES or is_sensitive_name(p) for p in relative.parts)
        ):
            raise ToolExecutionError(
                "path_denied", "archivo fuera de la lista de escritura autorizada"
            )
        raw = args.content.encode("utf-8")
        if len(raw) > 32768 or b"\0" in raw or redact(args.content)[1]:
            raise ToolExecutionError(
                "content_denied", "contenido binario, sensible o demasiado grande"
            )
        with self._lock:
            directory = os.open(record.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            temporary = f".agentforge-write-{uuid.uuid4().hex}"
            try:
                for part in relative.parts[:-1]:
                    next_fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                    os.close(directory)
                    directory = next_fd
                name = relative.name
                existing: bytes | None = None
                try:
                    fd = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                    )
                except FileNotFoundError:
                    pass
                else:
                    with os.fdopen(fd, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_nlink != 1
                            or info.st_size > 32768
                        ):
                            raise ToolExecutionError(
                                "path_denied", "se requiere un archivo regular pequeño sin enlaces"
                            )
                        existing = stream.read(32769)
                actual = hashlib.sha256(existing).hexdigest() if existing is not None else None
                if actual != args.expected_sha256:
                    raise ToolExecutionError("write_conflict", "el archivo cambió desde la lectura")
                fd = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=directory
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if existing is None:
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                    os.unlink(temporary, dir_fd=directory)
                else:
                    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            except OSError as exc:
                raise ToolExecutionError(
                    "path_denied", "no se pudo escribir de forma segura"
                ) from exc
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
                os.close(directory)
        return WriteFileOutput(
            path=args.path, sha256=hashlib.sha256(raw).hexdigest(), bytes_written=len(raw)
        )

"""Scope of a development run: what the Scoper proposes and what a human approves.

Pure contracts (ADR 0009). The proposal is the only thing the model produces;
the orchestrator filters it against the project's policy, a human approves the
filtered result by its digest, and Developer's write authority is built from
that approved list, never from a later model answer.
"""

import hashlib
import json
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

MAX_SCOPE_FILES = 16
MAX_PATH_LENGTH = 1024


def is_canonical_relative_path(path: str) -> bool:
    """``a/b.py`` form only: relative, normalised, no ``..``, no empty or dot parts."""
    if not path or len(path) > MAX_PATH_LENGTH or "\x00" in path or "\\" in path:
        return False
    if path.startswith(("/", "\\", "~", "-")) or path.endswith("/"):
        return False
    pure = PurePosixPath(path)
    parts = pure.parts
    return (
        bool(parts)
        and pure.as_posix() == path
        and all(part not in ("", ".", "..") for part in parts)
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
    )


class ScopeAction(StrEnum):
    EDIT = "edit"  # the file exists and may be replaced
    CREATE = "create"  # the file does not exist yet and may be created


class ScopeFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)
    action: ScopeAction
    reason: str = Field(min_length=1, max_length=500)


class ScopeProposal(BaseModel):
    """What the Scoper returns. Advisory until filtered and approved."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2000)
    files: list[ScopeFile] = Field(min_length=1, max_length=MAX_SCOPE_FILES)
    tests: list[str] = Field(default_factory=list, max_length=8)
    risks: list[str] = Field(default_factory=list, max_length=8)


class RejectedPath(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str = Field(max_length=MAX_PATH_LENGTH)
    reason: str = Field(max_length=200)


class ApprovedScope(BaseModel):
    """The filtered proposal, as shown to the human and bound by its digest."""

    model_config = ConfigDict(frozen=True)

    repository_id: str
    files: list[ScopeFile]
    tests: list[str]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def writable_paths(self) -> frozenset[str]:
        return frozenset(file.path for file in self.files)


def scope_digest(repository_id: str, files: list[ScopeFile], tests: list[str]) -> str:
    """Stable identifier of exactly this scope: repository, files with their
    action, and tests, independent of ordering and of reasons or summaries."""
    canonical = {
        "repository_id": repository_id,
        "files": sorted(
            ({"path": f.path, "action": f.action.value} for f in files), key=lambda e: e["path"]
        ),
        "tests": sorted(tests),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

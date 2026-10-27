"""Pre-publication scan (ADR 0011 §8): what may leave the host, checked twice.

The scan runs when the plan is prepared and again right before ``push``. It
works on facts extracted from Git by the platform (paths, modes, sizes,
binary flags, added lines), never on text the model produced, and it fails
**closed**: an exception, an undetermined binary flag or an empty change is a
blocking finding. Any blocking finding stops the publication.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from app.projects import RepositoryPolicy, check_against_policy

from .plan import Change

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    ("tailscale_key", re.compile(r"\btskey-[A-Za-z0-9\-]{10,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    ),
    ("url_credentials", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@")),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_\-]?key|access[_\-]?token|auth[_\-]?token"
            r"|client[_\-]?secret|private[_\-]?key)\b\s*[:=]\s*[\"'][^\"']{8,}[\"']"
        ),
    ),
)
SYMLINK_MODE = "120000"
SUBMODULE_MODE = "160000"
EXECUTABLE_MODE = "100755"
MAX_FINDINGS = 50


@dataclass(frozen=True)
class ChangedEntry:
    """One path of ``git diff-tree base..head`` as the platform read it."""

    path: str
    change: Change
    old_mode: str | None = None
    new_mode: str | None = None
    size: int | None = None  # bytes after the change; None when deleted
    is_binary: bool | None = None  # None = could not be determined
    added_lines: tuple[str, ...] = ()
    sha256: str | None = None


class ScanFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    path: str = ""
    line: int | None = None
    detail: str = ""
    blocking: bool = True


class ScanReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    findings: list[ScanFinding] = Field(default_factory=list)
    entries: int = 0
    total_bytes: int = 0

    @property
    def blocking(self) -> bool:
        return any(f.blocking for f in self.findings)

    @property
    def codes(self) -> list[str]:
        return sorted({f.code for f in self.findings})

    def digest(self) -> str:
        canonical = json.dumps(
            {
                "entries": self.entries,
                "total_bytes": self.total_bytes,
                "findings": [f.model_dump() for f in self.findings],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


def _failed(code: str, detail: str) -> ScanReport:
    return ScanReport(findings=[ScanFinding(code=code, detail=detail)])


def scan_changes(
    entries: Sequence[ChangedEntry],
    *,
    approved: Mapping[str, str],
    policy: RepositoryPolicy,
) -> ScanReport:
    """``approved`` maps each approved path to its action (``edit`` or ``create``)."""
    try:
        return _scan(entries, approved=approved, policy=policy)
    except Exception as exc:  # fail closed: an unexpected error never publishes
        return _failed("scan_error", f"el escaneo falló: {type(exc).__name__}")


def _scan(
    entries: Sequence[ChangedEntry], *, approved: Mapping[str, str], policy: RepositoryPolicy
) -> ScanReport:
    findings: list[ScanFinding] = []

    def add(code: str, path: str = "", detail: str = "", line: int | None = None) -> None:
        if len(findings) < MAX_FINDINGS:
            findings.append(ScanFinding(code=code, path=path, detail=detail, line=line))

    if not entries:
        add("empty_change", detail="no hay cambios entre la base y la cabeza")
        return ScanReport(findings=findings)
    paths = [e.path for e in entries]
    if len(set(paths)) != len(paths):
        add("duplicate_path", detail="el diff repite rutas")
    for rejected in check_against_policy(paths, policy):
        add(f"path_{rejected.reason}", rejected.path)
    total = 0
    for entry in entries:
        action = approved.get(entry.path)
        if action is None:
            add("path_not_in_scope", entry.path)
        elif entry.change == "deleted":
            add("deletion_not_allowed", entry.path)
        elif action == "create" and entry.change != "added":
            add("action_mismatch", entry.path, "aprobado como creación, el archivo ya existía")
        elif action == "edit" and entry.change != "modified":
            add("action_mismatch", entry.path, "aprobado como edición, el archivo no existía")
        if entry.new_mode == SYMLINK_MODE or entry.old_mode == SYMLINK_MODE:
            add("symlink", entry.path)
        if entry.new_mode == SUBMODULE_MODE or entry.old_mode == SUBMODULE_MODE:
            add("submodule", entry.path)
        if entry.new_mode == EXECUTABLE_MODE and entry.old_mode != EXECUTABLE_MODE:
            add("executable_bit_added", entry.path)
        if entry.change != "deleted":
            if entry.is_binary is None:
                add("binary_undetermined", entry.path)
            elif entry.is_binary:
                add("binary_file", entry.path)
            if entry.size is None:
                add("size_undetermined", entry.path)
            else:
                total += entry.size
                if entry.size > policy.max_file_bytes:
                    add("file_too_large", entry.path, f"{entry.size} > {policy.max_file_bytes}")
            if entry.sha256 is None:
                add("hash_undetermined", entry.path)
        for number, line in enumerate(entry.added_lines, start=1):
            if line.endswith("\r"):
                add("crlf_introduced", entry.path, line=number)
                break
        for number, line in enumerate(entry.added_lines, start=1):
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    add("secret_detected", entry.path, name, line=number)
                    break
    if total > policy.max_change_bytes:
        add("change_too_large", detail=f"{total} > {policy.max_change_bytes}")
    return ScanReport(findings=findings, entries=len(entries), total_bytes=total)

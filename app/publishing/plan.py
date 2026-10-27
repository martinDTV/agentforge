"""The publication plan and its canonical digest (ADR 0011 §3)."""

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.git.refs import validate_branch_name

PUBLISHER_ACTOR = "agentforge:publisher"
SHA_PATTERN = r"^[0-9a-f]{40}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,48}/$")
RESERVED_PREFIXES: frozenset[str] = frozenset(
    {"run/", "release/", "main/", "master/", "refs/", "heads/", "tags/", "hotfix/"}
)

Change = Literal["added", "modified", "deleted"]


def validate_branch_prefix(prefix: str) -> str:
    """A reserved, lower-case, slash-terminated namespace that is never a protected one."""
    if not _PREFIX_RE.fullmatch(prefix) or prefix in RESERVED_PREFIXES:
        raise ValueError(f"prefijo de rama no admitido: {prefix!r}")
    return prefix


def remote_branch_for(prefix: str, run_id: UUID) -> str:
    """``<prefix><run_id>``: derived, unique per run, never taken from a request."""
    return validate_branch_name(f"{validate_branch_prefix(prefix)}{run_id}")


class PublicationFile(BaseModel):
    """One path of the diff between ``base_sha`` and ``head_sha``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    change: Change
    sha256: str | None = Field(
        default=None, pattern=DIGEST_PATTERN
    )  # content after; None if deleted
    size: int = Field(default=0, ge=0)
    mode: str = Field(default="100644", pattern=r"^[0-7]{6}$")


class PublicationPlan(BaseModel):
    """Everything a ``publish`` decision authorises, and nothing else.

    Built by the platform from the mirror; shown to the human; recomputed and
    compared right before ``push``. ``prepared_at`` and ``summary`` are
    informative; every other field is part of the digest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    repository_id: str
    project_id: str
    provider: Literal["github"] = "github"
    owner: str
    name: str
    github_repository_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    base_branch: str
    base_sha: str = Field(pattern=SHA_PATTERN)
    # Head of the base branch at GitHub when the plan was prepared: must equal
    # ``base_sha`` (strict policy) and must still equal it when publishing.
    base_branch_head: str = Field(pattern=SHA_PATTERN)
    remote_branch: str
    head_sha: str = Field(pattern=SHA_PATTERN)
    patch_digest: str = Field(pattern=DIGEST_PATTERN)
    files: list[PublicationFile] = Field(min_length=1, max_length=64)
    scope_digest: str = Field(pattern=DIGEST_PATTERN)
    scan_digest: str = Field(pattern=DIGEST_PATTERN)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=4000)
    prepared_at: datetime

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def digest(self) -> str:
        return publish_digest(self)

    def binding_fields(self) -> dict[str, Any]:
        """The canonical subset the digest covers (sorted keys, no timestamps)."""
        return {
            "run_id": str(self.run_id),
            "repository_id": self.repository_id,
            "project_id": self.project_id,
            "provider": self.provider,
            "owner": self.owner.lower(),
            "name": self.name.lower(),
            "github_repository_id": self.github_repository_id,
            "installation_id": self.installation_id,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "base_branch_head": self.base_branch_head,
            "remote_branch": self.remote_branch,
            "head_sha": self.head_sha,
            "patch_digest": self.patch_digest,
            "files": [
                {"path": f.path, "change": f.change, "sha256": f.sha256, "mode": f.mode}
                for f in sorted(self.files, key=lambda f: f.path)
            ],
            "scope_digest": self.scope_digest,
            "scan_digest": self.scan_digest,
            "title": self.title,
        }


def publish_digest(plan: PublicationPlan) -> str:
    canonical = json.dumps(
        plan.binding_fields(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

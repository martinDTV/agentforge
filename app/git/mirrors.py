"""Local mirrors of remote repositories: records, outcomes and errors (ADR 0010).

A mirror is a bare repository under ``GIT_REPOSITORIES_ROOT/<project>/<repo>.git``
with a marker next to it that pins the remote identity it was created for. The
service that refreshes it lives in ``app/infrastructure/git/mirror.py``.
"""

from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.exceptions import DomainError

MIRROR_MARKER_SUFFIX = ".mirror.json"
MIRROR_LOCK_SUFFIX = ".mirror.lock"
PLATFORM_ACTOR = "agentforge:platform"


class MirrorError(DomainError):
    """A mirror operation was refused or failed. ``code`` is stable.

    Codes: ``mirror_not_remote``, ``mirror_unconfigured``, ``mirror_missing``,
    ``mirror_exists``, ``mirror_identity_mismatch``, ``mirror_remote_mismatch``,
    ``mirror_base_missing``, ``mirror_locked``, ``mirror_sync_failed``,
    ``mirror_base_mismatch`` plus every GitHub error code propagated as is.
    """

    sync: "MirrorSync | None" = None  # audit row describing the failure, when known

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class MirrorRecord(BaseModel):
    """What the marker pins: identity, remote URL and the last known base."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_id: str
    project_id: str
    provider: str
    owner: str
    name: str
    github_repository_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    default_branch: str
    remote_url: str
    path: Path
    base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    created_at: datetime
    last_synced_at: datetime | None = None
    last_identity_check_at: datetime | None = None


SyncResult = Literal["ok", "failed"]


class MirrorSync(BaseModel):
    """Outcome of one synchronisation, persisted for audit (never a token)."""

    model_config = ConfigDict(frozen=True)

    repository_id: str
    project_id: str
    operation: Literal["create", "refresh", "check"]
    result: SyncResult
    code: str | None = None
    base_sha: str | None = None
    previous_sha: str | None = None
    changed: bool = False
    github_repository_id: int | None = None
    installation_id: int | None = None
    token_expires_at: datetime | None = None
    started_at: datetime
    finished_at: datetime
    actor_subject: str = PLATFORM_ACTOR

    @property
    def duration_ms(self) -> int:
        return max(0, int((self.finished_at - self.started_at).total_seconds() * 1000))


class MirrorSyncRepository(Protocol):
    async def record(self, sync: MirrorSync) -> int: ...

    async def list_for_repository(self, repository_id: str, *, limit: int = 50) -> list[MirrorSync]:
        """Newest first."""
        ...

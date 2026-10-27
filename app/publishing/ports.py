"""Audit of publication steps: one row per transition, never a credential."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .plan import PUBLISHER_ACTOR
from .state import PublicationState


class PublicationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    repository_id: str
    project_id: str
    state: PublicationState
    code: str | None = None
    retryable: bool = False
    remote_branch: str | None = None
    base_branch: str | None = None
    base_sha: str | None = None
    base_branch_head: str | None = None
    head_sha: str | None = None
    patch_digest: str | None = None
    publish_digest: str | None = None
    pr_number: int | None = Field(default=None, ge=1)
    pr_url: str | None = None
    approved_by: str | None = None
    actor_subject: str = PUBLISHER_ACTOR
    attempt: int = Field(default=0, ge=0)
    token_expires_at: datetime | None = None
    recorded_at: datetime


class PublicationAuditRepository(Protocol):
    async def record(self, record: PublicationRecord) -> int: ...

    async def list_for_run(self, run_id: UUID) -> list[PublicationRecord]:
        """Oldest first."""
        ...

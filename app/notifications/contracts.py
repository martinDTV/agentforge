"""Contracts of the notification subsystem: kinds, messages, outbox and sender port."""

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class NotificationKind(StrEnum):
    RUN_CREATED = "run_created"
    ROUTE_SELECTED = "route_selected"
    ROUTE_ESCALATED = "route_escalated"
    PLANNING_COMPLETED = "planning_completed"
    PLAN_PENDING = "plan_pending"
    SCOPE_PENDING = "scope_pending"
    DEVELOPER_STARTED = "developer_started"
    DEVELOPER_COMPLETED = "developer_completed"
    QA_STARTED = "qa_started"
    QA_CHANGES_REQUESTED = "qa_changes_requested"
    QA_APPROVED = "qa_approved"
    PUBLISH_PENDING = "publish_pending"
    PULL_REQUEST_OPENED = "pull_request_opened"
    RUN_COMPLETED = "run_completed"
    RUN_PUBLISHED = "run_published"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RUN_EXPIRED = "run_expired"
    # Goals (Fase 8, ADR 0012): what the Supervisor tells the human about an objective.
    GOAL_CREATED = "goal_created"
    GOAL_PLAN_REQUESTED = "goal_plan_requested"
    GOAL_TASK_READY = "goal_task_ready"
    GOAL_TASK_STARTED = "goal_task_started"
    GOAL_TASK_PUBLISHED = "goal_task_published"
    GOAL_DEPENDENCY_MERGED = "goal_dependency_merged"
    GOAL_NEEDS_HUMAN = "goal_needs_human"
    GOAL_COMPLETED = "goal_completed"
    GOAL_PARTIAL = "goal_partial"
    GOAL_FAILED = "goal_failed"
    GOAL_CANCELLED = "goal_cancelled"
    GOAL_EXPIRED = "goal_expired"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"  # retry scheduled (next_attempt_at)
    DEAD = "dead"  # attempts exhausted or a non-retryable error


class EmailMessage(BaseModel):
    """What the sender delivers. Never carries credentials."""

    model_config = ConfigDict(frozen=True)

    to: list[str] = Field(min_length=1)
    subject: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1)
    html: str | None = None
    # Deterministic per outbox item: a retry after an uncertain SMTP answer sends
    # the same Message-ID, so a duplicate is recognisable by the mail client.
    message_id: str = Field(min_length=3)
    references: list[str] = Field(default_factory=list)  # threads one run together


class SendOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    code: str | None = None  # smtp_auth_failed, smtp_unreachable, smtp_rejected, ...
    detail: str | None = None  # never the credential, never the message body
    retryable: bool = False
    # The server may have accepted the message although the answer was lost.
    uncertain: bool = False


class EmailSender(Protocol):
    def send(self, message: EmailMessage) -> SendOutcome: ...


class OutboxItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    # Exactly one of the two is set: a run's notification or a goal's (migration 0010).
    run_id: UUID | None
    event_id: int | None
    kind: NotificationKind
    goal_id: UUID | None = None
    recipient: str
    subject: str
    text_body: str
    html_body: str | None
    dedup_key: str
    message_id: str
    status: OutboxStatus
    attempts: int = 0
    next_attempt_at: datetime
    last_error: str | None = None
    created_at: datetime
    sent_at: datetime | None = None
    lease_until: datetime | None = None


class NotificationOutbox(Protocol):
    """Persistent outbox. Implementations: PostgreSQL and in-memory."""

    async def claim(self, *, now: datetime, limit: int, lease_seconds: float) -> list[OutboxItem]:
        """Items due for delivery (pending/failed whose ``next_attempt_at`` passed,
        or ``sending`` whose lease expired), moved to ``sending`` atomically."""
        ...

    async def mark_sent(self, item_id: int, *, now: datetime) -> bool: ...

    async def mark_failed(
        self,
        item_id: int,
        *,
        now: datetime,
        error: str,
        next_attempt_at: datetime | None,
        dead: bool,
    ) -> bool: ...

    async def list_items(
        self, *, statuses: Sequence[OutboxStatus] | None = None, limit: int = 100
    ) -> list[OutboxItem]: ...

    async def requeue(self, item_id: int, *, now: datetime) -> bool:
        """Operator action: put a dead/failed item back to ``pending`` now."""
        ...

    async def counts(self) -> dict[str, int]: ...

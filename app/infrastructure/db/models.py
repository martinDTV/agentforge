"""SQLAlchemy 2.0 models for the business side of a run.

These tables are a *projection* of what the LangGraph checkpoint knows plus
what the graph cannot know: who approved, when, the audit trail and the
execution lease. They never replace the checkpoint as the source of truth
for resuming a graph.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.contracts.lifecycle import RunEventKind, RunLifecycleStatus, WorkflowKind
from app.contracts.tools import InvocationStatus

from .base import Base


def _values(enum_cls: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_cls]


class RunRecord(Base):
    __tablename__ = "runs"

    # Also the LangGraph thread_id: one run == one thread, by construction.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    # Ownership and isolation (ADR 0005). Rows older than authentication carry
    # the sentinels "legacy:unowned" / "legacy" set by migration 0003.
    owner_subject: Mapped[str] = mapped_column(String(255))
    project_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    task: Mapped[str] = mapped_column(Text)
    status: Mapped[RunLifecycleStatus] = mapped_column(
        Enum(
            RunLifecycleStatus,
            name="run_status",
            native_enum=False,
            length=32,
            create_constraint=True,
            validate_strings=True,
            values_callable=_values,
        )
    )

    # Which graph drives the run (ADR 0008). Rows created before 0005 are planning runs.
    workflow_kind: Mapped[WorkflowKind] = mapped_column(
        Enum(
            WorkflowKind,
            name="run_workflow_kind",
            native_enum=False,
            length=32,
            create_constraint=True,
            validate_strings=True,
            values_callable=_values,
        ),
        server_default=text("'planning'"),
    )

    # Projection of RunState.
    revision: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    llm_calls: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    architect_approved: Mapped[bool | None] = mapped_column(Boolean)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    architecture: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    last_checkpoint_id: Mapped[str | None] = mapped_column(String(64))

    # Execution lease: who is driving the graph and until when (heartbeat-renewed).
    # ``lease_generation`` is a monotonic fencing token: every acquisition
    # increments it, and every write by a lease holder is conditioned on it.
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_generation: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Idempotent POST /runs: one run per (project, principal, key). A key
        # reused by another principal or project is simply a different run.
        Index(
            "ix_runs_idempotency_scope",
            "project_id",
            "owner_subject",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_runs_project_id_created_at", "project_id", "created_at"),
        Index("ix_runs_owner_subject_created_at", "owner_subject", "created_at"),
        Index("ix_runs_status_created_at", "status", "created_at"),
        # Sweeper: expired leases for in-flight statuses.
        Index("ix_runs_status_lease_expires_at", "status", "lease_expires_at"),
        CheckConstraint("length(owner_subject) > 0", name="owner_subject_not_blank"),
        CheckConstraint("length(project_id) > 0", name="project_id_not_blank"),
        CheckConstraint("revision >= 0", name="revision_non_negative"),
        CheckConstraint("llm_calls >= 0", name="llm_calls_non_negative"),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        CheckConstraint("lease_generation >= 0", name="lease_generation_non_negative"),
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)", name="lease_fields_paired"
        ),
        CheckConstraint(
            "(error_code IS NULL) = (error_message IS NULL)", name="error_fields_paired"
        ),
    )


class RunApprovalRecord(Base):
    """The human decision, durable before it is applied to the graph.

    ``run_id`` is unique: the INSERT is the atomic claim that makes a second
    concurrent approval fail at the database, whatever the application does.
    ``applied_at`` stays NULL until ``Command(resume=...)`` has been confirmed,
    which is what lets a sweeper re-apply decisions lost to a crash.
    """

    __tablename__ = "run_approvals"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), unique=True
    )
    approved: Mapped[bool] = mapped_column(Boolean)
    # The principal who decided (human or service). NULL only for approvals
    # recorded before authentication existed.
    decided_by: Mapped[str | None] = mapped_column(String(255))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEventRecord(Base):
    """Append-only audit trail.

    ``(run_id, kind, ordinal)`` is the natural key: re-writing the same event
    after a retry is a no-op instead of a duplicate. ``ordinal`` is the plan
    revision for per-revision events and 0 otherwise. Payloads hold ids,
    codes and counters, never full plans (those live in ``runs``).
    """

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id", ondelete="CASCADE"))
    kind: Mapped[RunEventKind] = mapped_column(
        Enum(
            RunEventKind,
            name="run_event_kind",
            native_enum=False,
            length=48,
            create_constraint=False,
            validate_strings=True,
            values_callable=_values,
        )
    )
    ordinal: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    checkpoint_id: Mapped[str | None] = mapped_column(String(64))
    # Who caused the event: a principal's subject for human actions (creation,
    # approval), NULL for agent/node and system (recovery) events.
    actor_subject: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("run_id", "kind", "ordinal", name="uq_run_events_run_id_kind_ordinal"),
        Index("ix_run_events_run_id_id", "run_id", "id"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
    )


class ToolInvocationRecord(Base):
    """Audit of every tool call made on behalf of a run (Fase 5, ADR 0006).

    One row per (run, logical step, attempt), claimed *before* the tool runs
    and closed after. ``step`` is chosen by the orchestration and is stable
    across restarts; ``input_digest`` lets a retry prove it repeats the same
    call. A ``running`` row whose attempt is over means the process died:
    the next attempt marks it ``uncertain`` and refuses to repeat steps with
    effects. Digests, codes and counters only: arguments, file contents and
    outputs are never stored.
    """

    __tablename__ = "tool_invocations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id", ondelete="CASCADE"))
    step: Mapped[str] = mapped_column(String(128))
    attempt: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(128))
    tool_version: Mapped[str] = mapped_column(String(32), server_default=text("''"))
    risk: Mapped[str | None] = mapped_column(String(16))  # NULL: name matched no tool
    agent: Mapped[str] = mapped_column(String(32))
    actor_subject: Mapped[str] = mapped_column(String(255))
    project_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[InvocationStatus] = mapped_column(
        Enum(
            InvocationStatus,
            name="tool_invocation_status",
            native_enum=False,
            length=24,
            create_constraint=True,
            validate_strings=True,
            values_callable=_values,
        )
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    input_digest: Mapped[str] = mapped_column(String(64))
    output_digest: Mapped[str | None] = mapped_column(String(64))
    output_bytes: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "run_id", "step", "attempt", name="uq_tool_invocations_run_id_step_attempt"
        ),
        Index("ix_tool_invocations_project_id_started_at", "project_id", "started_at"),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        CheckConstraint("length(step) > 0", name="step_not_blank"),
        CheckConstraint(
            "(finished_at IS NULL) = (status IN ('pending', 'running'))",
            name="finished_when_closed",
        ),
    )

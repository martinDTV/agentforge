"""SQLAlchemy 2.0 models for the business side of a run.

These tables are a *projection* of what the LangGraph checkpoint knows plus
what the graph cannot know: who approved, when, the audit trail and the
execution lease. They never replace the checkpoint as the source of truth
for resuming a graph.
"""

import uuid
from datetime import datetime
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

from app.contracts.lifecycle import RunEventKind, RunLifecycleStatus

from .base import Base


def _values(enum_cls: type[RunLifecycleStatus] | type[RunEventKind]) -> list[str]:
    return [member.value for member in enum_cls]


class RunRecord(Base):
    __tablename__ = "runs"

    # Also the LangGraph thread_id: one run == one thread, by construction.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
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
        # Idempotent POST /runs: at most one run per client-supplied key.
        Index(
            "ix_runs_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_runs_status_created_at", "status", "created_at"),
        # Sweeper: expired leases for in-flight statuses.
        Index("ix_runs_status_lease_expires_at", "status", "lease_expires_at"),
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("run_id", "kind", "ordinal", name="uq_run_events_run_id_kind_ordinal"),
        Index("ix_run_events_run_id_id", "run_id", "id"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
    )

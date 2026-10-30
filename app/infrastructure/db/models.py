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
    Float,
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

from app.contracts.goals import ACTIVE_TASK_STATUSES, GoalStatus, GoalTaskStatus
from app.contracts.lifecycle import (
    DecisionStage,
    RunEventKind,
    RunLifecycleStatus,
    WorkflowKind,
)
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
    # Fase 7 (ADR 0009): which human decision the run waits for, until when,
    # and what was published (Paso 3). NULL for planning runs and for runs
    # created before migration 0006.
    pending_stage: Mapped[str | None] = mapped_column(String(16))
    approval_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publication: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Smart Router: the route the run follows and the mode that was requested.
    route: Mapped[str | None] = mapped_column(String(8))
    route_mode: Mapped[str | None] = mapped_column(String(8))

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


class RunDecisionRecord(Base):
    """One human decision per stage of a run (ADR 0009): ``plan`` (copied from
    ``run_approvals`` and double-written while that table exists), ``scope`` and
    ``publish``. ``(run_id, stage)`` is unique: the INSERT is the atomic claim.
    ``scope_digest`` binds a ``scope`` decision to the exact proposal shown."""

    __tablename__ = "run_decisions"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id", ondelete="CASCADE"))
    stage: Mapped[DecisionStage] = mapped_column(
        Enum(
            DecisionStage,
            name="run_decision_stage",
            native_enum=False,
            length=16,
            create_constraint=True,
            validate_strings=True,
            values_callable=_values,
        )
    )
    approved: Mapped[bool] = mapped_column(Boolean)
    decided_by: Mapped[str | None] = mapped_column(String(255))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope_digest: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("run_id", "stage", name="uq_run_decisions_run_id_stage"),
        Index("ix_run_decisions_run_id", "run_id"),
    )


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


class MirrorSyncRecord(Base):
    """One synchronisation of a local mirror with its GitHub repository (ADR 0010).

    Append-only audit written by the platform (``agentforge:platform``) before a
    development run and by the operator script. Holds identities, commit SHAs
    and codes; never a token, a URL with credentials or repository content.
    """

    __tablename__ = "mirror_syncs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(16))
    result: Mapped[str] = mapped_column(String(8))
    code: Mapped[str | None] = mapped_column(String(64))
    base_sha: Mapped[str | None] = mapped_column(String(40))
    previous_sha: Mapped[str | None] = mapped_column(String(40))
    changed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    github_repository_id: Mapped[int | None] = mapped_column(BigInteger)
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor_subject: Mapped[str] = mapped_column(
        String(255), server_default=text("'agentforge:platform'")
    )

    __table_args__ = (
        CheckConstraint(
            "operation IN ('create', 'refresh', 'check')", name="ck_mirror_syncs_operation"
        ),
        CheckConstraint("result IN ('ok', 'failed')", name="ck_mirror_syncs_result"),
        Index("ix_mirror_syncs_repository_id_started_at", "repository_id", "started_at"),
    )


class PublicationAuditRecord(Base):
    """One row per publication step of a run (ADR 0011): prepared, approved,
    pushed, opened, published, failed... with the identities, commits and pull
    request involved. Written by the platform (``agentforge:publisher``); never
    holds a token, a URL with credentials or the pull request body."""

    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id", ondelete="CASCADE"))
    repository_id: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32))
    code: Mapped[str | None] = mapped_column(String(64))
    retryable: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    remote_branch: Mapped[str | None] = mapped_column(String(200))
    base_branch: Mapped[str | None] = mapped_column(String(200))
    base_sha: Mapped[str | None] = mapped_column(String(40))
    base_branch_head: Mapped[str | None] = mapped_column(String(40))
    head_sha: Mapped[str | None] = mapped_column(String(40))
    patch_digest: Mapped[str | None] = mapped_column(String(64))
    publish_digest: Mapped[str | None] = mapped_column(String(64))
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(String(512))
    approved_by: Mapped[str | None] = mapped_column(String(255))
    actor_subject: Mapped[str] = mapped_column(
        String(255), server_default=text("'agentforge:publisher'")
    )
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('unavailable', 'prepared', 'approved', 'pushed', 'opened', 'published', "
            "'rejected', 'failed', 'cancelled_after_push')",
            name="ck_publications_state",
        ),
        Index("ix_publications_run_id_id", "run_id", "id"),
    )


class NotificationOutboxRecord(Base):
    """Outgoing notification, written in the same transaction as the event that
    caused it. Holds rendered text only: no credentials, no repository content."""

    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # One of the two is set (migration 0010): a run's notification or a goal's (ADR 0012).
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE")
    )
    goal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("goals.id", ondelete="CASCADE")
    )
    event_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(48))
    recipient: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(255))
    text_body: Mapped[str] = mapped_column(Text)
    html_body: Mapped[str | None] = mapped_column(Text)
    dedup_key: Mapped[str] = mapped_column(String(512))
    message_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_notification_outbox_dedup_key"),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'dead')",
            name="ck_notification_outbox_status",
        ),
        Index("ix_notification_outbox_status_next_attempt_at", "status", "next_attempt_at"),
        Index("ix_notification_outbox_run_id", "run_id"),
        Index("ix_notification_outbox_goal_id", "goal_id"),
        CheckConstraint(
            "run_id IS NOT NULL OR goal_id IS NOT NULL", name="ck_notification_outbox_subject"
        ),
    )


class RouteDecisionRecord(Base):
    """Smart Router decision for a run: route, reason, policy version, confidence
    and classification budget. One row per decision (the initial one and any
    escalation)."""

    __tablename__ = "route_decisions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id", ondelete="CASCADE"))
    route: Mapped[str] = mapped_column(String(8))
    mode: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(String(255))
    policy_version: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float | None] = mapped_column()
    classifier_used: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    mandatory_rules: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    escalated_from: Mapped[str | None] = mapped_column(String(8))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("route IN ('fast', 'full')", name="ck_route_decisions_route"),
        CheckConstraint("mode IN ('auto', 'fast', 'full')", name="ck_route_decisions_mode"),
        Index("ix_route_decisions_run_id_id", "run_id", "id"),
    )


def _in(values: list[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class GoalRecord(Base):
    """An operator objective coordinated by the deterministic Supervisor (ADR 0012).

    Projection of nothing: the goal has no LangGraph thread. Its children are
    ordinary ``runs`` rows owned by the goal's creator; ``goal_tasks`` links them.
    ``lease_owner``/``lease_until`` serialise reconciliation across processes.
    """

    __tablename__ = "goals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    repository_id: Mapped[str] = mapped_column(String(64))
    owner_subject: Mapped[str] = mapped_column(String(255))
    objective: Mapped[str] = mapped_column(Text)
    decomposition: Mapped[str] = mapped_column(String(16))
    dependency_policy: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB)
    route: Mapped[str | None] = mapped_column(String(8))
    route_decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Reserved *before* the planning run exists (idempotent creation): no foreign key.
    plan_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    plan_creating_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Aggregate budget: reservations charged before a child is created, and the
    # actual agent calls read back from the children's rows.
    tasks_created: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    reserved_model_calls: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    reserved_usd: Mapped[float] = mapped_column(Float, server_default=text("0"))
    llm_calls_used: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    planning_charged: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    blocked_reasons: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index(
            "ix_goals_idempotency_scope",
            "project_id",
            "owner_subject",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_goals_project_id_created_at", "project_id", "created_at"),
        Index("ix_goals_owner_subject_created_at", "owner_subject", "created_at"),
        Index("ix_goals_status_next_reconcile_at", "status", "next_reconcile_at"),
        CheckConstraint(
            f"status IN ({_in([s.value for s in GoalStatus])})", name="ck_goals_status"
        ),
        CheckConstraint(
            "decomposition IN ('auto', 'single', 'plan')", name="ck_goals_decomposition"
        ),
        CheckConstraint("tasks_created >= 0", name="goal_tasks_created_non_negative"),
        CheckConstraint("reserved_model_calls >= 0", name="goal_reserved_model_calls_non_negative"),
        CheckConstraint("reserved_usd >= 0", name="goal_reserved_usd_non_negative"),
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_until IS NULL)", name="goal_lease_fields_paired"
        ),
        CheckConstraint(
            "(error_code IS NULL) = (error_message IS NULL)", name="goal_error_fields_paired"
        ),
    )


class GoalTaskRecord(Base):
    """One task of a goal and the child run that carries it out (ADR 0012 §2.1).

    ``run_id`` is reserved *before* ``RunService.create`` so a crash in between
    is recoverable without a duplicate. The partial unique index on
    ``(repository_id, fingerprint)`` makes two identical active tasks impossible.
    """

    __tablename__ = "goal_tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("goals.id", ondelete="CASCADE"))
    task_key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    task_text: Mapped[str] = mapped_column(Text)
    dependencies: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    ordinal: Mapped[int] = mapped_column(Integer)
    repository_id: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    # Reserved before ``RunService.create`` runs (ADR 0012 §2.3): a row may name a run that
    # does not exist yet, so there is deliberately no foreign key to ``runs``.
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    base_sha: Mapped[str | None] = mapped_column(String(40))
    head_sha: Mapped[str | None] = mapped_column(String(40))
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(String(512))
    merge_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    scope_digest: Mapped[str | None] = mapped_column(String(64))
    scope_paths: Mapped[list[str] | None] = mapped_column(JSONB)
    creating_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    waiting_stage: Mapped[str | None] = mapped_column(String(16))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    llm_calls: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("goal_id", "task_key", name="uq_goal_tasks_goal_id_task_key"),
        Index(
            "ix_goal_tasks_active_fingerprint",
            "repository_id",
            "fingerprint",
            unique=True,
            postgresql_where=text(
                f"status IN ({_in(sorted(s.value for s in ACTIVE_TASK_STATUSES))})"
            ),
        ),
        Index("ix_goal_tasks_goal_id_ordinal", "goal_id", "ordinal"),
        Index("ix_goal_tasks_run_id", "run_id"),
        CheckConstraint(
            f"status IN ({_in([s.value for s in GoalTaskStatus])})", name="ck_goal_tasks_status"
        ),
        CheckConstraint("ordinal >= 0", name="goal_task_ordinal_non_negative"),
        CheckConstraint("attempt >= 0", name="goal_task_attempt_non_negative"),
    )


class GoalEventRecord(Base):
    """Append-only audit of a goal; ``(goal_id, kind, ordinal)`` is the natural key."""

    __tablename__ = "goal_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("goals.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(48))
    ordinal: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    actor_subject: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("goal_id", "kind", "ordinal", name="uq_goal_events_goal_id_kind_ordinal"),
        Index("ix_goal_events_goal_id_id", "goal_id", "id"),
        CheckConstraint("ordinal >= 0", name="goal_event_ordinal_non_negative"),
    )

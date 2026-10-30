"""Goals, goal tasks and goal events (Fase 8, Paso 1; ADR 0012).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-30

Additive: ``goals`` (an operator objective coordinated by the deterministic
Supervisor), ``goal_tasks`` (its tasks and the child run reserved for each; run ids
are reserved before the run exists, so they carry no foreign key to ``runs``),
``goal_events`` (append-only audit) and, on ``notification_outbox``, a nullable
``goal_id`` next to a now-nullable ``run_id`` with a check that one of them is
set. No existing row changes. The downgrade drops the three tables and the
column; it refuses while any outbox row belongs to a goal only, because 0009
cannot represent it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GOAL_STATUSES = [
    "planning",
    "active",
    "blocked",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "expired",
]
TASK_STATUSES = [
    "pending",
    "ready",
    "creating",
    "running",
    "waiting_human",
    "published",
    "merged",
    "failed",
    "skipped",
    "duplicate",
]
ACTIVE_TASK_STATUSES = ["creating", "pending", "ready", "running", "waiting_human"]


def _in(values: list[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "goals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("repository_id", sa.String(length=64), nullable=False),
        sa.Column("owner_subject", sa.String(length=255), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("decomposition", sa.String(length=16), nullable=False),
        sa.Column("dependency_policy", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("route", sa.String(length=8), nullable=True),
        sa.Column("route_decision", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("plan_run_id", sa.Uuid(), nullable=True),
        sa.Column("plan_creating_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tasks_created", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "reserved_model_calls", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("reserved_usd", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("llm_calls_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "planning_charged", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "blocked_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(f"status IN ({_in(GOAL_STATUSES)})", name=op.f("ck_goals_status")),
        sa.CheckConstraint(
            "decomposition IN ('auto', 'single', 'plan')", name=op.f("ck_goals_decomposition")
        ),
        sa.CheckConstraint(
            "tasks_created >= 0", name=op.f("ck_goals_goal_tasks_created_non_negative")
        ),
        sa.CheckConstraint(
            "reserved_model_calls >= 0",
            name=op.f("ck_goals_goal_reserved_model_calls_non_negative"),
        ),
        sa.CheckConstraint(
            "reserved_usd >= 0", name=op.f("ck_goals_goal_reserved_usd_non_negative")
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL) = (lease_until IS NULL)",
            name=op.f("ck_goals_goal_lease_fields_paired"),
        ),
        sa.CheckConstraint(
            "(error_code IS NULL) = (error_message IS NULL)",
            name=op.f("ck_goals_goal_error_fields_paired"),
        ),
    )
    op.create_index(
        "ix_goals_idempotency_scope",
        "goals",
        ["project_id", "owner_subject", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index("ix_goals_project_id_created_at", "goals", ["project_id", "created_at"])
    op.create_index("ix_goals_owner_subject_created_at", "goals", ["owner_subject", "created_at"])
    op.create_index("ix_goals_status_next_reconcile_at", "goals", ["status", "next_reconcile_at"])

    op.create_table(
        "goal_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("goal_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("task_text", sa.Text(), nullable=False),
        sa.Column(
            "dependencies",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("repository_id", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("base_sha", sa.String(length=40), nullable=True),
        sa.Column("head_sha", sa.String(length=40), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(length=512), nullable=True),
        sa.Column("merge_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("scope_digest", sa.String(length=64), nullable=True),
        sa.Column("scope_paths", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("creating_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waiting_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waiting_stage", sa.String(length=16), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("llm_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], name=op.f("fk_goal_tasks_goal_id_goals"), ondelete="CASCADE"
        ),
        sa.UniqueConstraint("goal_id", "task_key", name="uq_goal_tasks_goal_id_task_key"),
        sa.CheckConstraint(f"status IN ({_in(TASK_STATUSES)})", name=op.f("ck_goal_tasks_status")),
        sa.CheckConstraint(
            "ordinal >= 0", name=op.f("ck_goal_tasks_goal_task_ordinal_non_negative")
        ),
        sa.CheckConstraint(
            "attempt >= 0", name=op.f("ck_goal_tasks_goal_task_attempt_non_negative")
        ),
    )
    op.create_index(
        "ix_goal_tasks_active_fingerprint",
        "goal_tasks",
        ["repository_id", "fingerprint"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({_in(ACTIVE_TASK_STATUSES)})"),
    )
    op.create_index("ix_goal_tasks_goal_id_ordinal", "goal_tasks", ["goal_id", "ordinal"])
    op.create_index("ix_goal_tasks_run_id", "goal_tasks", ["run_id"])

    op.create_table(
        "goal_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("goal_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("ordinal", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("actor_subject", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], name=op.f("fk_goal_events_goal_id_goals"), ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "goal_id", "kind", "ordinal", name="uq_goal_events_goal_id_kind_ordinal"
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name=op.f("ck_goal_events_goal_event_ordinal_non_negative")
        ),
    )
    op.create_index("ix_goal_events_goal_id_id", "goal_events", ["goal_id", "id"])

    # Outbox: a notification now belongs to a run or to a goal.
    op.alter_column("notification_outbox", "run_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("notification_outbox", sa.Column("goal_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_notification_outbox_goal_id_goals"),
        "notification_outbox",
        "goals",
        ["goal_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_notification_outbox_goal_id", "notification_outbox", ["goal_id"])
    op.create_check_constraint(
        op.f("ck_notification_outbox_subject"),
        "notification_outbox",
        "run_id IS NOT NULL OR goal_id IS NOT NULL",
    )


def downgrade() -> None:
    orphans = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM notification_outbox WHERE run_id IS NULL"))
        .scalar()
    )
    if orphans:
        raise RuntimeError(
            f"downgrade rechazado: {orphans} notificaciones pertenecen solo a un objetivo y no "
            "tienen representación en 0009; elimínalas explícitamente antes de bajar"
        )
    op.drop_constraint(op.f("ck_notification_outbox_subject"), "notification_outbox", type_="check")
    op.drop_index("ix_notification_outbox_goal_id", table_name="notification_outbox")
    op.drop_constraint(
        op.f("fk_notification_outbox_goal_id_goals"), "notification_outbox", type_="foreignkey"
    )
    op.drop_column("notification_outbox", "goal_id")
    op.alter_column("notification_outbox", "run_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_index("ix_goal_events_goal_id_id", table_name="goal_events")
    op.drop_table("goal_events")
    op.drop_index("ix_goal_tasks_run_id", table_name="goal_tasks")
    op.drop_index("ix_goal_tasks_goal_id_ordinal", table_name="goal_tasks")
    op.drop_index("ix_goal_tasks_active_fingerprint", table_name="goal_tasks")
    op.drop_table("goal_tasks")
    op.drop_index("ix_goals_status_next_reconcile_at", table_name="goals")
    op.drop_index("ix_goals_owner_subject_created_at", table_name="goals")
    op.drop_index("ix_goals_project_id_created_at", table_name="goals")
    op.drop_index("ix_goals_idempotency_scope", table_name="goals")
    op.drop_table("goals")

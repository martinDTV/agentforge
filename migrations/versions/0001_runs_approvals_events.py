"""Business tables for runs: projection, durable approvals and audit trail.

Revision ID: 0001
Revises:
Create Date: 2026-10-24

The LangGraph checkpoint tables are deliberately absent: ``AsyncPostgresSaver``
creates and versions them through its own ``setup()`` (ADR 0003). Both this
migration and that setup run as deploy steps, never at application startup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_STATUSES = (
    "running",
    "waiting_approval",
    "resume_pending",
    "interrupted",
    "approved",
    "rejected",
    "failed",
)
RUN_EVENT_KINDS = (
    "run_created",
    "planner_completed",
    "architect_reviewed",
    "revision_requested",
    "waiting_approval",
    "approval_recorded",
    "approval_applied",
    "run_approved",
    "run_rejected",
    "run_failed",
    "run_interrupted",
    "run_resumed",
)


def upgrade() -> None:
    op.create_table(
        "runs",
        # Also the LangGraph thread_id.
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                *RUN_STATUSES,
                name="run_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        # Projection of the graph state.
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("llm_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("architect_approved", sa.Boolean(), nullable=True),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("architecture", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("last_checkpoint_id", sa.String(length=64), nullable=True),
        # Execution lease (heartbeat-renewed) and attempt counter.
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "(error_code IS NULL) = (error_message IS NULL)",
            name=op.f("ck_runs_error_fields_paired"),
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
            name=op.f("ck_runs_lease_fields_paired"),
        ),
        sa.CheckConstraint("attempt >= 0", name=op.f("ck_runs_attempt_non_negative")),
        sa.CheckConstraint("llm_calls >= 0", name=op.f("ck_runs_llm_calls_non_negative")),
        sa.CheckConstraint("revision >= 0", name=op.f("ck_runs_revision_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    # Idempotent POST /runs: at most one run per client-supplied key.
    op.create_index(
        "ix_runs_idempotency_key",
        "runs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index("ix_runs_status_created_at", "runs", ["status", "created_at"], unique=False)
    # Sweeper: expired leases for in-flight statuses.
    op.create_index(
        "ix_runs_status_lease_expires_at", "runs", ["status", "lease_expires_at"], unique=False
    )

    op.create_table(
        "run_approvals",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # NULL until Command(resume=...) is confirmed; sweepers re-apply NULL ones.
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_approvals_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_approvals")),
        # One decision per run, enforced by the database whatever the app does.
        sa.UniqueConstraint("run_id", name=op.f("uq_run_approvals_run_id")),
    )

    op.create_table(
        "run_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(*RUN_EVENT_KINDS, name="run_event_kind", native_enum=False, length=48),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_run_events_ordinal_non_negative")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_events_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_events")),
        # Natural key: re-writing an event after a retry is a no-op, not a duplicate.
        sa.UniqueConstraint("run_id", "kind", "ordinal", name="uq_run_events_run_id_kind_ordinal"),
    )
    op.create_index("ix_run_events_run_id_id", "run_events", ["run_id", "id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_run_events_run_id_id", table_name="run_events")
    op.drop_table("run_events")
    op.drop_table("run_approvals")
    op.drop_index("ix_runs_status_lease_expires_at", table_name="runs")
    op.drop_index("ix_runs_status_created_at", table_name="runs")
    op.drop_index(
        "ix_runs_idempotency_key",
        table_name="runs",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.drop_table("runs")

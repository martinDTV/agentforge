"""Audit of tool invocations (Fase 5, ADR 0006).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24

One row per tool call made on behalf of a run, claimed before the tool runs
and closed afterwards. ``(run_id, step, attempt)`` is the natural key: the
logical step is chosen by the orchestration and survives restarts, so a
re-driven attempt finds what earlier attempts did (``input_digest`` proves it
is the same call) and a step left ``running`` by a dead process is marked
``uncertain`` instead of silently repeated. Rows hold digests, codes and
counters only; arguments, file contents and outputs are never stored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("pending", "running", "ok", "failed", "denied", "invalid_input", "uncertain")


def upgrade() -> None:
    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("step", sa.String(length=128), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column(
            "tool_version", sa.String(length=32), server_default=sa.text("''"), nullable=False
        ),
        # NULL when the requested name matched no registered tool.
        sa.Column("risk", sa.String(length=16), nullable=True),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("actor_subject", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                *STATUSES,
                name="tool_invocation_status",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("output_digest", sa.String(length=64), nullable=True),
        sa.Column("output_bytes", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # Only an open row (pending/running) may lack a finish time; refusals
        # are inserted already closed.
        sa.CheckConstraint(
            "(finished_at IS NULL) = (status IN ('pending', 'running'))",
            name=op.f("ck_tool_invocations_finished_when_closed"),
        ),
        sa.CheckConstraint("attempt >= 0", name=op.f("ck_tool_invocations_attempt_non_negative")),
        sa.CheckConstraint("length(step) > 0", name=op.f("ck_tool_invocations_step_not_blank")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_tool_invocations_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_invocations")),
        sa.UniqueConstraint(
            "run_id", "step", "attempt", name="uq_tool_invocations_run_id_step_attempt"
        ),
    )
    op.create_index(
        "ix_tool_invocations_project_id_started_at",
        "tool_invocations",
        ["project_id", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tool_invocations_project_id_started_at", table_name="tool_invocations")
    op.drop_table("tool_invocations")

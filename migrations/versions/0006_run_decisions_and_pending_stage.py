"""Human decisions per stage, pending stage metadata and the ``cancelled`` and
``expired`` statuses (Fase 7, ADR 0009).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26

Additive. ``run_decisions`` records one decision per ``(run_id, stage)``;
every row of ``run_approvals`` is copied as stage ``plan`` and that table is
kept (and still written) for compatibility. ``runs`` gains ``pending_stage``,
``approval_deadline`` and ``publication`` (all NULL for existing rows) and two
terminal statuses. The downgrade drops the new table and columns and restores
the previous status constraint; it is refused while any run is ``cancelled``
or ``expired`` because 0005 has no faithful status for them. ``scope`` and
``publish`` decisions are lost on downgrade (they have no table in 0005);
``plan`` decisions survive in ``run_approvals``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = (
    "running",
    "waiting_approval",
    "resume_pending",
    "interrupted",
    "approved",
    "rejected",
    "completed",
    "failed",
)
NEW_STATUSES = (*OLD_STATUSES[:-1], "cancelled", "expired", "failed")
STAGES = ("plan", "scope", "publish")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def upgrade() -> None:
    op.create_table(
        "run_decisions",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage",
            sa.Enum(*STAGES, name="run_decision_stage", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope_digest", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_decisions_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_decisions")),
        sa.UniqueConstraint("run_id", "stage", name="uq_run_decisions_run_id_stage"),
    )
    op.create_check_constraint(
        op.f("ck_run_decisions_run_decision_stage"), "run_decisions", _in("stage", STAGES)
    )
    op.create_index("ix_run_decisions_run_id", "run_decisions", ["run_id"], unique=False)
    op.execute(
        "INSERT INTO run_decisions (run_id, stage, approved, decided_by, decided_at, applied_at) "
        "SELECT run_id, 'plan', approved, decided_by, decided_at, applied_at FROM run_approvals"
    )
    op.add_column("runs", sa.Column("pending_stage", sa.String(length=16), nullable=True))
    op.add_column("runs", sa.Column("approval_deadline", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "runs",
        sa.Column("publication", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.drop_constraint(op.f("ck_runs_run_status"), "runs", type_="check")
    op.create_check_constraint(op.f("ck_runs_run_status"), "runs", _in("status", NEW_STATUSES))


def downgrade() -> None:
    blocked = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM runs WHERE status IN ('cancelled', 'expired')"))
        .scalar()
    )
    if blocked:
        raise RuntimeError(
            f"downgrade rechazado: {blocked} ejecuciones en estado 'cancelled' o 'expired' no "
            "tienen equivalente en 0005; resuélvelas (o elimínalas explícitamente) antes de bajar"
        )
    op.drop_constraint(op.f("ck_runs_run_status"), "runs", type_="check")
    op.create_check_constraint(op.f("ck_runs_run_status"), "runs", _in("status", OLD_STATUSES))
    op.drop_column("runs", "publication")
    op.drop_column("runs", "approval_deadline")
    op.drop_column("runs", "pending_stage")
    op.drop_index("ix_run_decisions_run_id", table_name="run_decisions")
    op.drop_table("run_decisions")

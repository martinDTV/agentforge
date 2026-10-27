"""Publication audit and the ``published`` status (Fase 7, Paso 3; ADR 0011).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-27

Additive. ``runs.status`` accepts the terminal value ``published`` (a human
approved publication and the draft pull request exists). ``publications``
holds one row per publication step of a run (plan prepared, approved, branch
pushed, pull request opened, published, failed...) with identities, commits
and the pull request number; never credentials. The downgrade drops the table
and restores the previous status constraint; it is refused while any run is
``published`` because 0007 has no faithful status for it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
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
    "cancelled",
    "expired",
    "failed",
)
NEW_STATUSES = (*OLD_STATUSES[:-1], "published", "failed")
STATES = (
    "unavailable",
    "prepared",
    "approved",
    "pushed",
    "opened",
    "published",
    "rejected",
    "failed",
    "cancelled_after_push",
)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_runs_run_status"), "runs", type_="check")
    op.create_check_constraint(op.f("ck_runs_run_status"), "runs", _in("status", NEW_STATUSES))
    op.create_table(
        "publications",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=True),
        sa.Column("retryable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("remote_branch", sa.String(length=200), nullable=True),
        sa.Column("base_branch", sa.String(length=200), nullable=True),
        sa.Column("base_sha", sa.String(length=40), nullable=True),
        sa.Column("base_branch_head", sa.String(length=40), nullable=True),
        sa.Column("head_sha", sa.String(length=40), nullable=True),
        sa.Column("patch_digest", sa.String(length=64), nullable=True),
        sa.Column("publish_digest", sa.String(length=64), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(length=512), nullable=True),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column(
            "actor_subject",
            sa.String(length=255),
            server_default=sa.text("'agentforge:publisher'"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_publications_run_id_runs"), ondelete="CASCADE"
        ),
        sa.CheckConstraint(_in("state", STATES), name="ck_publications_state"),
    )
    op.create_index("ix_publications_run_id_id", "publications", ["run_id", "id"])


def downgrade() -> None:
    blocked = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM runs WHERE status = 'published'"))
        .scalar()
    )
    if blocked:
        raise RuntimeError(
            f"downgrade rechazado: {blocked} ejecuciones en estado 'published' no tienen "
            "equivalente en 0007; resuélvelas (o elimínalas explícitamente) antes de bajar"
        )
    op.drop_index("ix_publications_run_id_id", table_name="publications")
    op.drop_table("publications")
    op.drop_constraint(op.f("ck_runs_run_status"), "runs", type_="check")
    op.create_check_constraint(op.f("ck_runs_run_status"), "runs", _in("status", OLD_STATUSES))

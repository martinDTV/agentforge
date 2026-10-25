"""Ownership, project isolation and actors (Fase 4, ADR 0005).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24

Runs gain an owner (principal subject) and a project; approvals record who
decided; events record who acted. Rows that pre-date authentication are NOT
handed to any new user: they are backfilled with the sentinels
``legacy:unowned`` / ``legacy``, a project no token can claim, so only a
principal holding ``projects:all`` can reach them. The idempotency key becomes
unique per (project, owner, key) instead of globally.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_OWNER = "legacy:unowned"
LEGACY_PROJECT = "legacy"


def upgrade() -> None:
    # Expand with a server default so existing rows get the sentinels, then
    # drop the default: new rows must state owner and project explicitly.
    op.add_column(
        "runs",
        sa.Column(
            "owner_subject", sa.String(length=255), server_default=LEGACY_OWNER, nullable=False
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "project_id", sa.String(length=64), server_default=LEGACY_PROJECT, nullable=False
        ),
    )
    op.alter_column("runs", "owner_subject", server_default=None)
    op.alter_column("runs", "project_id", server_default=None)
    op.create_check_constraint(
        op.f("ck_runs_owner_subject_not_blank"), "runs", "length(owner_subject) > 0"
    )
    op.create_check_constraint(
        op.f("ck_runs_project_id_not_blank"), "runs", "length(project_id) > 0"
    )

    op.drop_index(
        "ix_runs_idempotency_key",
        table_name="runs",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_runs_idempotency_scope",
        "runs",
        ["project_id", "owner_subject", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index("ix_runs_project_id_created_at", "runs", ["project_id", "created_at"])
    op.create_index("ix_runs_owner_subject_created_at", "runs", ["owner_subject", "created_at"])

    op.add_column("run_approvals", sa.Column("decided_by", sa.String(length=255), nullable=True))
    op.add_column("run_events", sa.Column("actor_subject", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("run_events", "actor_subject")
    op.drop_column("run_approvals", "decided_by")
    op.drop_index("ix_runs_owner_subject_created_at", table_name="runs")
    op.drop_index("ix_runs_project_id_created_at", table_name="runs")
    op.drop_index(
        "ix_runs_idempotency_scope",
        table_name="runs",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    # Back to a global key: duplicates across owners would now collide, so
    # keep only the oldest row per key before recreating the unique index.
    op.execute(
        """
        UPDATE runs SET idempotency_key = NULL
        WHERE idempotency_key IS NOT NULL AND id NOT IN (
            SELECT DISTINCT ON (idempotency_key) id FROM runs
            WHERE idempotency_key IS NOT NULL ORDER BY idempotency_key, created_at
        )
        """
    )
    op.create_index(
        "ix_runs_idempotency_key",
        "runs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.drop_constraint(op.f("ck_runs_project_id_not_blank"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_owner_subject_not_blank"), "runs", type_="check")
    op.drop_column("runs", "project_id")
    op.drop_column("runs", "owner_subject")

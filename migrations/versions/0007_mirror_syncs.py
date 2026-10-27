"""Audit of local mirror synchronisations (Fase 7, Paso 2; ADR 0010).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-26

Additive: one new table, ``mirror_syncs``, with one row per create/refresh/
check of a local mirror against its GitHub repository (result, codes, base
commits, identities, never credentials). The downgrade drops the table; the
history it holds is lost, nothing else is affected.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mirror_syncs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("repository_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("result", sa.String(length=8), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=True),
        sa.Column("base_sha", sa.String(length=40), nullable=True),
        sa.Column("previous_sha", sa.String(length=40), nullable=True),
        sa.Column("changed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("github_repository_id", sa.BigInteger(), nullable=True),
        sa.Column("installation_id", sa.BigInteger(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "actor_subject",
            sa.String(length=255),
            server_default=sa.text("'agentforge:platform'"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "operation IN ('create', 'refresh', 'check')", name="ck_mirror_syncs_operation"
        ),
        sa.CheckConstraint("result IN ('ok', 'failed')", name="ck_mirror_syncs_result"),
    )
    op.create_index(
        "ix_mirror_syncs_repository_id_started_at",
        "mirror_syncs",
        ["repository_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mirror_syncs_repository_id_started_at", table_name="mirror_syncs")
    op.drop_table("mirror_syncs")

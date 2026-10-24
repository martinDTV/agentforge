"""Fencing token for execution leases.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24

``runs.lease_generation`` increases on every lease acquisition. Writes made by
a lease holder are conditioned on ``(lease_owner, lease_generation)``, so a
previous owner that wakes up late can never overwrite a newer state.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("lease_generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_runs_lease_generation_non_negative"), "runs", "lease_generation >= 0"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_runs_lease_generation_non_negative"), "runs", type_="check")
    op.drop_column("runs", "lease_generation")

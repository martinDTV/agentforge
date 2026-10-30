"""Optional formal reason on human decisions.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-30

Additive: ``run_decisions.reason`` (nullable, 500 characters), the motive a human
may attach when approving or rejecting a stage. Notifications show it as the
recorded reason of the decision instead of guessing one. The downgrade drops the
column; the reasons are lost, nothing else changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run_decisions", sa.Column("reason", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("run_decisions", "reason")

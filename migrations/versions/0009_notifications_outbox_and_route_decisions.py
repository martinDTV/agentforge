"""Notification outbox and Smart Router decisions.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-27

Additive: ``notification_outbox`` (rendered notifications waiting for or
already delivered by the worker; never credentials), ``route_decisions`` (route
chosen per run with reason, policy version, confidence and classification
budget) and two nullable columns on ``runs`` (``route``, ``route_mode``). The
downgrade drops them; the notification history and route audit are lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("route", sa.String(length=8), nullable=True))
    op.add_column("runs", sa.Column("route_mode", sa.String(length=8), nullable=True))
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=True),
        sa.Column("dedup_key", sa.String(length=512), nullable=False),
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_notification_outbox_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("dedup_key", name="uq_notification_outbox_dedup_key"),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'dead')",
            name="ck_notification_outbox_status",
        ),
    )
    op.create_index(
        "ix_notification_outbox_status_next_attempt_at",
        "notification_outbox",
        ["status", "next_attempt_at"],
    )
    op.create_index("ix_notification_outbox_run_id", "notification_outbox", ["run_id"])
    op.create_table(
        "route_decisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("route", sa.String(length=8), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("classifier_used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "mandatory_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "usage",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("escalated_from", sa.String(length=8), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_route_decisions_run_id_runs"), ondelete="CASCADE"
        ),
        sa.CheckConstraint("route IN ('fast', 'full')", name="ck_route_decisions_route"),
        sa.CheckConstraint("mode IN ('auto', 'fast', 'full')", name="ck_route_decisions_mode"),
    )
    op.create_index("ix_route_decisions_run_id_id", "route_decisions", ["run_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_route_decisions_run_id_id", table_name="route_decisions")
    op.drop_table("route_decisions")
    op.drop_index("ix_notification_outbox_run_id", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_status_next_attempt_at", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.drop_column("runs", "route_mode")
    op.drop_column("runs", "route")

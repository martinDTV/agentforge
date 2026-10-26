"""Workflow kind per run and the ``completed`` terminal status (Fase 6, ADR 0008).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26

``runs.workflow_kind`` records which graph drives a run (``planning`` for every
row that exists before this revision, ``development`` for the Developer/QA
flow) so recovery decides from the row instead of guessing from a key.
``completed`` is the terminal status of a workflow that needs no human
decision; ``approved``/``rejected`` keep meaning a human verdict.

Additive and backwards compatible: existing rows get ``planning`` by default
and no existing status changes meaning. The downgrade is refused while any
row is ``completed`` (there is no faithful older status for it), so it never
rewrites data silently.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = (
    "running",
    "waiting_approval",
    "resume_pending",
    "interrupted",
    "approved",
    "rejected",
    "failed",
)
NEW_STATUSES = (*OLD_STATUSES[:-1], "completed", "failed")
WORKFLOW_KINDS = ("planning", "development")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "workflow_kind",
            sa.Enum(
                *WORKFLOW_KINDS,
                name="run_workflow_kind",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default=sa.text("'planning'"),
            nullable=False,
        ),
    )
    # ``add_column`` never emits the Enum's CHECK: create it explicitly.
    op.create_check_constraint(
        op.f("ck_runs_run_workflow_kind"), "runs", _in("workflow_kind", WORKFLOW_KINDS)
    )
    op.drop_constraint(op.f("ck_runs_run_status"), "runs", type_="check")
    op.create_check_constraint(op.f("ck_runs_run_status"), "runs", _in("status", NEW_STATUSES))


def downgrade() -> None:
    completed = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM runs WHERE status = 'completed'"))
        .scalar()
    )
    if completed:
        raise RuntimeError(
            f"downgrade rechazado: {completed} ejecuciones en estado 'completed' no tienen "
            "equivalente en 0004; resuélvelas (o elimínalas explícitamente) antes de bajar"
        )
    op.drop_constraint(op.f("ck_runs_run_status"), "runs", type_="check")
    op.create_check_constraint(op.f("ck_runs_run_status"), "runs", _in("status", OLD_STATUSES))
    op.drop_constraint(op.f("ck_runs_run_workflow_kind"), "runs", type_="check")
    op.drop_column("runs", "workflow_kind")

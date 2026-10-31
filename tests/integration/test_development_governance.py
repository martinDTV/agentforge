"""Fase 7, Paso 1 on PostgreSQL: migration 0006, stage decisions with their race,
cancellation, expiry, and a governed development run through the API."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.contracts.lifecycle import DecisionStage, RunEventKind, RunLifecycleStatus, WorkflowKind
from app.infrastructure.db import (
    Database,
    SqlAlchemyRunRepository,
)
from tests.integration.conftest import downgrade, migrate

pytestmark = pytest.mark.anyio
S = RunLifecycleStatus
PROJECT = "sandbox-gov"
REPO = "sandbox-gov"
RUNS = "/api/v1/runs"


def test_0006_adds_decisions_and_statuses_and_refuses_a_lossy_downgrade(empty_database_url: str):
    migrate(empty_database_url, "0005")
    engine = create_engine(empty_database_url)
    try:
        run_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO runs (id, task, status, owner_subject, project_id) "
                    "VALUES (:id, 't', 'approved', 'alice', 'alpha')"
                ),
                {"id": run_id},
            )
            conn.execute(
                text(
                    "INSERT INTO run_approvals (run_id, approved, decided_by, applied_at) "
                    "VALUES (:id, true, 'bob', now())"
                ),
                {"id": run_id},
            )
        migrate(empty_database_url, "0006")
        inspector = inspect(engine)
        assert "run_decisions" in inspector.get_table_names()
        columns = {c["name"] for c in inspector.get_columns("runs")}
        assert {"pending_stage", "approval_deadline", "publication"} <= columns
        with engine.begin() as conn:
            copied = conn.execute(
                text(
                    "SELECT stage, approved, decided_by, applied_at IS NOT NULL FROM run_decisions"
                )
            ).all()
            assert copied == [("plan", True, "bob", True)]  # history preserved as stage plan
            conn.execute(
                text(
                    "INSERT INTO runs (id, task, status, owner_subject, project_id, workflow_kind) "
                    "VALUES (gen_random_uuid(), 't', 'cancelled', 'alice', 'sandbox-x', "
                    "'development')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO runs (id, task, status, owner_subject, project_id, workflow_kind) "
                    "VALUES (gen_random_uuid(), 't', 'expired', 'alice', 'sandbox-x', "
                    "'development')"
                )
            )
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO run_decisions (run_id, stage, approved) "
                    "VALUES (:id, 'plan', false)"
                ),
                {"id": run_id},
            )  # one decision per (run, stage)
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO run_decisions (run_id, stage, approved) "
                    "VALUES (:id, 'other', true)"
                ),
                {"id": run_id},
            )
        with pytest.raises(RuntimeError, match="2 ejecuciones"):
            downgrade(empty_database_url, "0005")  # cancelled and expired both block it
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM runs WHERE status = 'cancelled'"))
        with pytest.raises(RuntimeError, match="1 ejecuciones"):
            downgrade(empty_database_url, "0005")  # expired alone still blocks it
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM runs WHERE status = 'expired'"))
        downgrade(empty_database_url, "0005")
        inspector = inspect(engine)
        assert "run_decisions" not in inspector.get_table_names()
        assert "pending_stage" not in {c["name"] for c in inspector.get_columns("runs")}
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM run_approvals")) == 1  # plan survives
        migrate(empty_database_url, "0006")
    finally:
        engine.dispose()


async def test_stage_decisions_are_claimed_once_cancelled_and_expired_in_sql(
    checkpoint_database_url: str,
):
    database = Database(checkpoint_database_url)
    repository = SqlAlchemyRunRepository(database.session_factory)
    try:
        run_id = uuid.uuid4()
        await repository.create_run(
            run_id,
            "task",
            owner="p",
            ttl_seconds=60,
            owner_subject="olivia",
            project_id=PROJECT,
            workflow_kind=WorkflowKind.DEVELOPMENT,
        )
        # Put the row in waiting_approval with a free lease, as the projection would.
        async with database.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE runs SET status='waiting_approval', pending_stage='scope', "
                    "approval_deadline = now() + interval '1 hour', lease_owner=NULL, "
                    "lease_expires_at=NULL WHERE id=:id"
                ),
                {"id": run_id},
            )
        digest = "a" * 64
        claims = await asyncio.gather(
            *[
                repository.claim_decision(
                    run_id,
                    DecisionStage.SCOPE,
                    approved,
                    owner=f"api-{i}",
                    ttl_seconds=30,
                    decided_by=f"human-{i}",
                    scope_digest=digest,
                )
                for i, approved in enumerate((True, False, True))
            ]
        )
        assert sum(1 for c in claims if c is not None and c.claimed) == 1
        winner = next(c for c in claims if c is not None and c.claimed)
        assert winner.existing is not None and winner.existing.stage is DecisionStage.SCOPE
        assert winner.existing.scope_digest == digest and winner.existing.applied_at is None
        pending = await repository.get_pending_decision(run_id)
        assert pending is not None and pending.stage is DecisionStage.SCOPE
        assert await repository.mark_decision_applied(run_id, DecisionStage.SCOPE)
        assert not await repository.mark_decision_applied(run_id, DecisionStage.SCOPE)
        assert await repository.get_pending_decision(run_id) is None
        assert [d.stage for d in await repository.list_decisions(run_id)] == [DecisionStage.SCOPE]
        # A resume_pending row with a live lease cannot be cancelled; a waiting one can.
        assert await repository.cancel_run(run_id, actor="olivia") is None
        other = uuid.uuid4()
        await repository.create_run(
            other,
            "task",
            owner="p",
            ttl_seconds=60,
            owner_subject="olivia",
            project_id=PROJECT,
            workflow_kind=WorkflowKind.DEVELOPMENT,
        )
        async with database.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE runs SET status='waiting_approval', pending_stage='scope', "
                    "approval_deadline = now() - interval '1 minute', lease_owner=NULL, "
                    "lease_expires_at=NULL WHERE id=:id"
                ),
                {"id": other},
            )
        expired = await repository.expire_waiting_runs(now=datetime.now(UTC), limit=10)
        assert [r.id for r in expired] == [other] and expired[0].status is S.EXPIRED
        assert expired[0].pending_stage is None and expired[0].approval_deadline is None
        kinds = [e.kind for e in await repository.list_events(other, after_id=None, limit=20)]
        assert RunEventKind.RUN_EXPIRED in kinds
        assert await repository.cancel_run(other, actor="olivia") is None  # terminal already
        third = uuid.uuid4()
        await repository.create_run(
            third,
            "task",
            owner="p",
            ttl_seconds=60,
            owner_subject="olivia",
            project_id=PROJECT,
            workflow_kind=WorkflowKind.DEVELOPMENT,
        )
        async with database.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE runs SET status='waiting_approval', pending_stage='scope', "
                    "lease_owner=NULL, lease_expires_at=NULL WHERE id=:id"
                ),
                {"id": third},
            )
        cancelled = await repository.cancel_run(third, actor="olivia")
        assert cancelled is not None and cancelled.status is S.CANCELLED
        events = await repository.list_events(third, after_id=None, limit=20)
        assert (
            next(e for e in events if e.kind is RunEventKind.RUN_CANCELLED).actor_subject
            == "olivia"
        )
    finally:
        await database.dispose()


async def test_two_decisions_on_one_run_keep_their_own_event_ordinals(
    checkpoint_database_url: str,
):
    """Regression (first real publication, 2026-10-27): ``run_events`` is unique on
    ``(run_id, kind, ordinal)``; recording the ``publish`` decision after the ``scope``
    one used to raise ``UniqueViolation`` on ``approval_recorded``/``approval_applied``
    and rolled the approval back. Each stage now owns an ordinal."""
    from app.contracts.lifecycle import RunEventKind
    from app.orchestration.ports import RunEvent

    database = Database(checkpoint_database_url)
    repository = SqlAlchemyRunRepository(database.session_factory)
    try:
        run_id = uuid.uuid4()
        await repository.create_run(
            run_id,
            "task",
            owner="p",
            ttl_seconds=60,
            owner_subject="agentforge-operator",
            project_id=PROJECT,
            workflow_kind=WorkflowKind.DEVELOPMENT,
        )

        async def wait_for(stage: str) -> None:
            async with database.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE runs SET status='waiting_approval', pending_stage=:stage, "
                        "approval_deadline = now() + interval '1 hour', lease_owner=NULL, "
                        "lease_expires_at=NULL WHERE id=:id"
                    ),
                    {"id": run_id, "stage": stage},
                )

        for stage, digest in ((DecisionStage.SCOPE, "a" * 64), (DecisionStage.PUBLISH, "b" * 64)):
            await wait_for(stage.value)
            claim = await repository.claim_decision(
                run_id,
                stage,
                True,
                owner="api",
                ttl_seconds=30,
                decided_by="martin",
                scope_digest=digest,
            )
            assert claim is not None and claim.claimed, stage
            assert await repository.mark_decision_applied(run_id, stage)
            assert (
                await repository.append_events(
                    run_id,
                    [
                        RunEvent(
                            kind=RunEventKind.APPROVAL_APPLIED,
                            ordinal=stage.event_ordinal,
                            payload={"approved": True, "stage": stage.value},
                        )
                    ],
                )
                == 1
            )
        assert [d.stage for d in await repository.list_decisions(run_id)] == [
            DecisionStage.SCOPE,
            DecisionStage.PUBLISH,
        ]
        events = await repository.list_events(run_id, after_id=None, limit=100)
        recorded = [(e.kind, e.ordinal, e.payload.get("stage")) for e in events]
        assert (RunEventKind.APPROVAL_RECORDED, 1, "scope") in recorded
        assert (RunEventKind.APPROVAL_RECORDED, 2, "publish") in recorded
        assert (RunEventKind.APPROVAL_APPLIED, 1, "scope") in recorded
        assert (RunEventKind.APPROVAL_APPLIED, 2, "publish") in recorded
    finally:
        await database.dispose()


async def test_expiry_keeps_the_stage_in_the_row_the_event_and_closes_the_window_for_good(
    checkpoint_database_url: str,
):
    """H2: the sweep writes ``expired:<stage>`` and the event names the stage before the
    row forgets it; an expired decision can never be claimed afterwards."""
    database = Database(checkpoint_database_url)
    repository = SqlAlchemyRunRepository(database.session_factory)
    try:
        for stage in ("scope", "publish"):
            run_id = uuid.uuid4()
            await repository.create_run(
                run_id,
                "task",
                owner="p",
                ttl_seconds=60,
                owner_subject="olivia",
                project_id=PROJECT,
                workflow_kind=WorkflowKind.DEVELOPMENT,
            )
            async with database.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE runs SET status='waiting_approval', pending_stage=:stage, "
                        "approval_deadline = now() - interval '1 minute', lease_owner=NULL, "
                        "lease_expires_at=NULL WHERE id=:id"
                    ),
                    {"id": run_id, "stage": stage},
                )
            expired = await repository.expire_waiting_runs(now=datetime.now(UTC), limit=10)
            row = next(r for r in expired if r.id == run_id)
            assert row.status is S.EXPIRED and row.pending_stage is None
            assert row.error is not None and row.error.code == f"expired:{stage}"
            assert "caducó" in row.error.message
            events = await repository.list_events(run_id, after_id=None, limit=20)
            event = next(e for e in events if e.kind is RunEventKind.RUN_EXPIRED)
            assert event.payload["stage"] == stage and event.payload["code"] == f"expired:{stage}"
            assert event.payload["deadline"]
            # Closed for good: nothing pending, no second expiry, no late claim.
            assert await repository.get_pending_decision(run_id) is None
            again = await repository.expire_waiting_runs(now=datetime.now(UTC), limit=10)
            assert all(r.id != run_id for r in again)
            late = await repository.claim_decision(
                run_id,
                DecisionStage(stage),
                True,
                owner="api-late",
                ttl_seconds=30,
                decided_by="human-late",
                scope_digest="a" * 64,
            )
            assert late is None or not late.claimed
    finally:
        await database.dispose()

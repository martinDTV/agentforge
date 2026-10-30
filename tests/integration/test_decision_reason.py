"""Migration 0011 and the optional formal reason of a human decision, persisted with the
decision row and carried by the ``approval_recorded`` event that notifications read."""

import uuid

import pytest
from sqlalchemy import create_engine, inspect

from app.contracts.lifecycle import DecisionStage, RunEventKind, RunLifecycleStatus, WorkflowKind
from app.infrastructure.db import Database, SqlAlchemyRunRepository
from app.infrastructure.db.schema import expected_head_revision
from app.orchestration.ports import RunProjection
from tests.integration.conftest import downgrade, migrate

pytestmark = pytest.mark.anyio


def test_0011_adds_the_nullable_reason_column_and_downgrades_cleanly(empty_database_url: str):
    assert expected_head_revision() == "0011"
    migrate(empty_database_url, "0010")
    engine = create_engine(empty_database_url)
    try:
        assert "reason" not in {c["name"] for c in inspect(engine).get_columns("run_decisions")}
        migrate(empty_database_url, "0011")
        columns = {c["name"]: c for c in inspect(engine).get_columns("run_decisions")}
        assert columns["reason"]["nullable"] is True
        downgrade(empty_database_url, "0010")
        assert "reason" not in {c["name"] for c in inspect(engine).get_columns("run_decisions")}
        migrate(empty_database_url, "head")
    finally:
        engine.dispose()


async def test_a_decision_reason_is_persisted_and_audited_but_never_required(
    fresh_migrated_database_url: str,
):
    database = Database(fresh_migrated_database_url)
    try:
        runs = SqlAlchemyRunRepository(database.session_factory)
        run_id = uuid.uuid4()
        created = await runs.create_run(
            run_id,
            "Implementa greet",
            owner="p",
            ttl_seconds=60,
            owner_subject="olivia",
            project_id="sandbox",
            workflow_kind=WorkflowKind.DEVELOPMENT,
        )
        assert created.lease is not None
        await runs.project(
            run_id,
            RunProjection(
                status=RunLifecycleStatus.WAITING_APPROVAL, pending_stage=DecisionStage.SCOPE
            ),
            lease=created.lease,
            release=True,
        )
        claim = await runs.claim_decision(
            run_id,
            DecisionStage.SCOPE,
            False,
            owner="p",
            ttl_seconds=60,
            decided_by="omar",
            scope_digest="a" * 64,
            reason="Los archivos no cubren las pruebas de aceptación",
        )
        assert claim is not None and claim.claimed and claim.existing is not None
        assert claim.existing.reason == "Los archivos no cubren las pruebas de aceptación"
        decisions = await runs.list_decisions(run_id)
        assert decisions[0].reason == "Los archivos no cubren las pruebas de aceptación"
        events = await runs.list_events(run_id)
        recorded = next(e for e in events if e.kind is RunEventKind.APPROVAL_RECORDED)
        assert recorded.payload["reason"] == "Los archivos no cubren las pruebas de aceptación"
        assert recorded.payload["rejected_by"] == "omar"
        # Without a reason nothing is written: older clients keep working unchanged.
        other = uuid.uuid4()
        second = await runs.create_run(
            other,
            "Implementa greet 2",
            owner="p",
            ttl_seconds=60,
            owner_subject="olivia",
            project_id="sandbox",
            workflow_kind=WorkflowKind.PLANNING,
        )
        assert second.lease is not None
        await runs.project(
            other,
            RunProjection(status=RunLifecycleStatus.WAITING_APPROVAL),
            lease=second.lease,
            release=True,
        )
        plain = await runs.claim_decision(
            other, DecisionStage.PLAN, True, owner="p", ttl_seconds=60, decided_by="omar"
        )
        assert plain is not None and plain.existing is not None and plain.existing.reason is None
        event = next(
            e for e in await runs.list_events(other) if e.kind is RunEventKind.APPROVAL_RECORDED
        )
        assert "reason" not in event.payload
    finally:
        await database.dispose()

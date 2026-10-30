"""Fase 7, Paso 3 on PostgreSQL: migration 0008, the publication audit table and
the compare-and-set on ``runs.publication``."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.contracts.lifecycle import WorkflowKind
from app.infrastructure.db import (
    Database,
    SqlAlchemyPublicationRepository,
    SqlAlchemyRunRepository,
)
from app.infrastructure.db.schema import expected_head_revision
from app.publishing import PublicationRecord, PublicationState
from tests.integration.conftest import downgrade, migrate

pytestmark = pytest.mark.anyio
T0 = datetime(2026, 10, 27, tzinfo=UTC)


def test_0008_adds_published_and_the_publications_table_and_guards_the_downgrade(
    empty_database_url: str,
):
    assert expected_head_revision() == "0010"
    migrate(empty_database_url, "0007")
    engine = create_engine(empty_database_url)
    try:
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO runs (id, task, status, owner_subject, project_id, workflow_kind) "
                    "VALUES (gen_random_uuid(), 't', 'published', 'alice', 'sandbox-x', "
                    "'development')"
                )
            )
        migrate(empty_database_url, "0008")
        inspector = inspect(engine)
        assert "publications" in inspector.get_table_names()
        run_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO runs (id, task, status, owner_subject, project_id, workflow_kind) "
                    "VALUES (:id, 't', 'published', 'alice', 'sandbox-x', 'development')"
                ),
                {"id": run_id},
            )
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO publications (run_id, repository_id, project_id, state) "
                    "VALUES (:id, 'r', 'p', 'merged')"
                ),
                {"id": run_id},
            )
        with pytest.raises(Exception, match="published"):
            downgrade(empty_database_url, "0007")  # refused while a published run exists
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM runs WHERE id = :id"), {"id": run_id})
        downgrade(empty_database_url, "0007")
        assert "publications" not in inspect(engine).get_table_names()
        migrate(empty_database_url, "head")
    finally:
        engine.dispose()


async def test_publication_audit_rows_and_the_state_compare_and_set(migrated_database_url: str):
    database = Database(migrated_database_url)
    try:
        runs = SqlAlchemyRunRepository(database.session_factory)
        audit = SqlAlchemyPublicationRepository(database.session_factory)
        run_id = uuid.uuid4()
        await runs.create_run(
            run_id,
            "t",
            owner="p",
            ttl_seconds=60,
            owner_subject="olivia",
            project_id="sandbox-github",
            workflow_kind=WorkflowKind.DEVELOPMENT,
        )
        prepared = {"state": "prepared", "publish_digest": "d" * 64}
        assert await runs.set_publication(run_id, prepared, expect_absent=True) is not None
        assert await runs.set_publication(run_id, prepared, expect_absent=True) is None
        approved = {**prepared, "state": "approved", "approved_by": "omar"}
        assert await runs.set_publication(run_id, approved, expected_state="pushed") is None
        row = await runs.set_publication(run_id, approved, expected_state="prepared")
        assert row is not None and row.publication == approved
        pushed = {**approved, "state": "pushed", "head_sha": "b" * 40}
        winners = [
            await runs.set_publication(run_id, pushed, expected_state="approved"),
            await runs.set_publication(run_id, pushed, expected_state="approved"),
        ]
        assert sum(w is not None for w in winners) == 1
        for state in (
            PublicationState.PREPARED,
            PublicationState.APPROVED,
            PublicationState.PUSHED,
        ):
            await audit.record(
                PublicationRecord(
                    run_id=run_id,
                    repository_id="sandbox-github",
                    project_id="sandbox-github",
                    state=state,
                    remote_branch=f"agentforge/{run_id}",
                    head_sha="b" * 40,
                    approved_by="omar" if state is not PublicationState.PREPARED else None,
                    recorded_at=T0,
                )
            )
        rows = await audit.list_for_run(run_id)
        assert [r.state for r in rows] == [
            PublicationState.PREPARED,
            PublicationState.APPROVED,
            PublicationState.PUSHED,
        ]
        assert rows[-1].actor_subject == "agentforge:publisher" and rows[-1].approved_by == "omar"
    finally:
        await database.dispose()

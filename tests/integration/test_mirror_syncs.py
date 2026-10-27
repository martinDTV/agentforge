"""Fase 7, Paso 2 on PostgreSQL: migration 0007 and the mirror audit repository."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.git.mirrors import MirrorSync
from app.infrastructure.db import Database, SqlAlchemyMirrorSyncRepository
from app.infrastructure.db.schema import expected_head_revision
from tests.integration.conftest import downgrade, migrate

pytestmark = pytest.mark.anyio
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def test_0007_adds_the_mirror_audit_table_and_downgrades_cleanly(empty_database_url: str):
    assert expected_head_revision() == "0009"
    migrate(empty_database_url, "0006")
    engine = create_engine(empty_database_url)
    try:
        assert "mirror_syncs" not in inspect(engine).get_table_names()
        migrate(empty_database_url, "0007")
        inspector = inspect(engine)
        assert "mirror_syncs" in inspector.get_table_names()
        columns = {c["name"] for c in inspector.get_columns("mirror_syncs")}
        assert {
            "repository_id",
            "operation",
            "result",
            "code",
            "base_sha",
            "previous_sha",
        } <= columns
        assert "token" not in " ".join(sorted(columns)).replace("token_expires_at", "")
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO mirror_syncs (repository_id, project_id, operation, result, "
                    "started_at, finished_at) VALUES ('r', 'p', 'push', 'ok', now(), now())"
                )
            )
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO mirror_syncs (repository_id, project_id, operation, result, "
                    "started_at, finished_at) VALUES ('r', 'p', 'refresh', 'maybe', now(), now())"
                )
            )
        downgrade(empty_database_url, "0006")
        assert "mirror_syncs" not in inspect(engine).get_table_names()
        migrate(empty_database_url, "head")
    finally:
        engine.dispose()


async def test_mirror_syncs_are_recorded_and_listed_newest_first(migrated_database_url: str):
    database = Database(migrated_database_url)
    try:
        repo = SqlAlchemyMirrorSyncRepository(database.session_factory)
        first = MirrorSync(
            repository_id="sandbox-demo",
            project_id="sandbox",
            operation="create",
            result="ok",
            base_sha="a" * 40,
            changed=True,
            github_repository_id=987654321,
            installation_id=55,
            token_expires_at=T0 + timedelta(hours=1),
            started_at=T0,
            finished_at=T0 + timedelta(seconds=3),
        )
        second = first.model_copy(
            update={
                "operation": "refresh",
                "result": "failed",
                "code": "repository_substituted",
                "base_sha": None,
                "changed": False,
                "started_at": T0 + timedelta(minutes=5),
                "finished_at": T0 + timedelta(minutes=5, seconds=1),
            }
        )
        assert await repo.record(first) < await repo.record(second)
        rows = await repo.list_for_repository("sandbox-demo")
        assert [(r.operation, r.result, r.code) for r in rows] == [
            ("refresh", "failed", "repository_substituted"),
            ("create", "ok", None),
        ]
        assert rows[1].base_sha == "a" * 40 and rows[1].actor_subject == "agentforge:platform"
        assert rows[0].duration_ms == 1000 and rows[1].token_expires_at == first.token_expires_at
        assert await repo.list_for_repository("other") == []
    finally:
        await database.dispose()

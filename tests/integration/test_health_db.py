"""Readiness against a real database: healthy, outage, wrong schema, unreachable."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.infrastructure.db import Database, SchemaMismatchError
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.main import create_app
from app.orchestration.limits import RunLimits
from tests.integration.conftest import TemporaryDatabases

READY = "/health/ready"
RUNS = "/api/v1/runs"


def test_ready_is_ok_against_a_migrated_database(
    fake_llm: FakeLLMAdapter, migrated_database_url: str
):
    app = create_app(fake_llm, RunLimits(), database=Database(migrated_database_url))
    with TestClient(app) as client:
        response = client.get(READY)
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "database": "ok",
            "schema": "ok",
            "checkpoints": "memory",
        }
        # Fase 2 behaviour is intact with a database attached.
        created = client.post(RUNS, json={"task": "Crear módulo de pacientes con pruebas"})
        assert created.status_code == 201
        assert created.json()["status"] == "waiting_approval"


def test_startup_fails_fast_when_the_schema_is_not_at_head(
    fake_llm: FakeLLMAdapter, empty_database_url: str
):
    app = create_app(fake_llm, RunLimits(), database=Database(empty_database_url))
    with pytest.raises(SchemaMismatchError, match="alembic upgrade head"):
        with TestClient(app):
            pass


def test_startup_fails_fast_when_the_schema_is_stale(
    fake_llm: FakeLLMAdapter, stale_schema_database_url: str
):
    app = create_app(fake_llm, RunLimits(), database=Database(stale_schema_database_url))
    with pytest.raises(SchemaMismatchError, match="9999"):
        with TestClient(app):
            pass


def test_app_starts_degraded_when_the_database_is_unreachable(fake_llm: FakeLLMAdapter):
    unreachable = Database("postgresql+psycopg://127.0.0.1:1/agentforge", connect_timeout_seconds=1)
    app = create_app(fake_llm, RunLimits(), database=unreachable)
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get(READY)
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable", "database": "unreachable"}


def test_ready_flips_to_503_during_an_outage_and_recovers(
    fake_llm: FakeLLMAdapter,
    temporary_databases: TemporaryDatabases,
    fresh_migrated_database_url: str,
):
    url = fresh_migrated_database_url
    app = create_app(fake_llm, RunLimits(), database=Database(url, connect_timeout_seconds=2))
    with TestClient(app) as client:
        assert client.get(READY).status_code == 200
        temporary_databases.set_allow_connections(url, False)
        try:
            outage = client.get(READY)
            assert outage.status_code == 503
            assert outage.json()["database"] == "unreachable"
        finally:
            temporary_databases.set_allow_connections(url, True)
        recovered = client.get(READY)
        assert recovered.status_code == 200
        assert recovered.json()["schema"] == "ok"


def test_ready_reports_schema_mismatch_detected_after_startup(
    fake_llm: FakeLLMAdapter, fresh_migrated_database_url: str
):
    url = fresh_migrated_database_url
    app = create_app(fake_llm, RunLimits(), database=Database(url))
    with TestClient(app) as client:
        assert client.get(READY).status_code == 200
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '9999'"))
        engine.dispose()
        app.state.schema_verified = False  # as after a reconnect to a restored replica
        response = client.get(READY)
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable", "database": "ok", "schema": "mismatch"}

"""The unit suite runs with an in-memory checkpointer and cannot reach PostgreSQL."""

import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from psycopg_pool import PoolTimeout

from app.infrastructure.checkpoints import PostgresCheckpointer
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.main import create_app
from app.orchestration.limits import RunLimits


@pytest.mark.anyio
async def test_async_postgres_connections_are_blocked():
    with pytest.raises(RuntimeError, match="bloqueado en la suite unitaria"):
        await psycopg.AsyncConnection.connect("postgresql://localhost:5432/agentforge")


def test_sync_postgres_connections_are_blocked():
    with pytest.raises(RuntimeError, match="bloqueado en la suite unitaria"):
        psycopg.Connection.connect("postgresql://localhost:5432/agentforge")


@pytest.mark.anyio
async def test_checkpointer_pool_cannot_open_in_the_unit_suite():
    checkpointer = PostgresCheckpointer(
        "postgresql://localhost:5432/agentforge", connect_timeout_seconds=1
    )
    with pytest.raises(PoolTimeout):
        await checkpointer.open(wait=True)
    await checkpointer.close()


def test_unit_app_checkpoints_in_memory_and_keeps_fase_2_behaviour(fake_llm: FakeLLMAdapter):
    app = create_app(fake_llm, RunLimits())
    with TestClient(app) as client:
        assert app.state.checkpoints is None
        assert app.state.database is None
        assert isinstance(app.state.run_service.graph.checkpointer, InMemorySaver)
        created = client.post(
            "/api/v1/runs", json={"task": "Crear módulo de pacientes con pruebas"}
        )
        assert created.status_code == 201
        assert created.json()["status"] == "waiting_approval"

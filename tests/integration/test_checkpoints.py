"""LangGraph checkpoints in PostgreSQL: restart, resume, thread_id, strict rows, recovery."""

import asyncio
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.contracts.planning import ArchitectureReview, DevelopmentPlan
from app.infrastructure.checkpoints import (
    CheckpointSchemaError,
    PostgresCheckpointer,
    expected_migration_version,
)
from app.infrastructure.db import Database, SqlAlchemyRunRepository
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.orchestration.graph import build_graph, build_state_graph
from app.orchestration.runs import RunService
from app.security.tokens import DisabledAuthentication
from tests.integration.conftest import (
    CHECKPOINTS_APP_NAME as APP_NAME,
)
from tests.integration.conftest import (
    libpq_dsn,
    make_app,
)

RUNS = "/api/v1/runs"
TASK = {"task": "Crear módulo de pacientes con pruebas"}
KEY = b"0123456789abcdef0123456789abcdef"
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(fake_llm: FakeLLMAdapter, checkpoint_database_url: str) -> Iterator[TestClient]:
    with TestClient(make_app(fake_llm, checkpoint_database_url)) as client:
        yield client


def query(url: str, sql: str, *params: object) -> list[tuple[object, ...]]:
    with psycopg.connect(libpq_dsn(url)) as conn:
        return conn.execute(sql, params).fetchall()  # type: ignore[arg-type,return-value]


def calls_by_schema(fake_llm: FakeLLMAdapter) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, _, schema in fake_llm.calls:
        counts[schema.__name__] = counts.get(schema.__name__, 0) + 1
    return counts


# 1 + 2 + 3 -------------------------------------------------------------------


def test_run_survives_restart_and_pending_approval_resumes_on_a_second_instance(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    with TestClient(make_app(fake_llm, url)) as first:
        created = first.post(RUNS, json=TASK).json()
    run_id = created["run_id"]
    assert created["status"] == "waiting_approval"

    with TestClient(make_app(fake_llm, url)) as second:
        assert second.get(f"{RUNS}/{run_id}").json() == created
        approved = second.post(f"{RUNS}/{run_id}/approve", json={"approved": True})
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

    with TestClient(make_app(fake_llm, url)) as third:
        assert third.get(f"{RUNS}/{run_id}").json()["status"] == "approved"
        again = third.post(f"{RUNS}/{run_id}/approve", json={"approved": False})
        assert again.status_code == 409 and again.json()["code"] == "run_already_decided"

    assert calls_by_schema(fake_llm) == {"DevelopmentPlan": 1, "ArchitectureReview": 1}


def test_thread_id_is_exactly_the_run_id(client: TestClient, checkpoint_database_url: str):
    run_id = client.post(RUNS, json=TASK).json()["run_id"]
    uuid.UUID(run_id)  # well-formed

    rows = query(
        checkpoint_database_url,
        "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id = %s",
        run_id,
    )
    assert rows == [(run_id,)]

    async def inspect() -> tuple[str, int, bool]:
        checkpointer = PostgresCheckpointer(libpq_dsn(checkpoint_database_url))
        saver = await checkpointer.open(wait=True)
        try:
            config = {"configurable": {"thread_id": run_id}}
            found = await saver.aget_tuple(config)
            assert found is not None
            history = [c async for c in saver.alist(config)]
            missing = await saver.aget_tuple({"configurable": {"thread_id": str(uuid.uuid4())}})
            return found.config["configurable"]["thread_id"], len(history), missing is None
        finally:
            await checkpointer.close()

    thread_id, checkpoints, unknown_is_none = asyncio.run(inspect())
    assert thread_id == run_id
    assert checkpoints >= 3  # input, planner, architect, (interrupt)
    assert unknown_is_none


# 4 ---------------------------------------------------------------------------


def test_checkpoint_rows_never_contain_pickle(client: TestClient, checkpoint_database_url: str):
    run_id = client.post(RUNS, json=TASK).json()["run_id"]
    client.post(f"{RUNS}/{run_id}/approve", json={"approved": True})
    blob_types = {
        row[0]
        for row in query(
            checkpoint_database_url,
            "SELECT DISTINCT type FROM checkpoint_blobs WHERE thread_id = %s",
            run_id,
        )
    }
    write_types = {
        row[0]
        for row in query(
            checkpoint_database_url,
            "SELECT DISTINCT type FROM checkpoint_writes WHERE thread_id = %s",
            run_id,
        )
    }
    assert blob_types and write_types
    assert "pickle" not in blob_types | write_types
    assert (blob_types | write_types) <= {"msgpack", "empty", "json", "null"}


def test_encrypted_checkpoints_hide_content_and_stay_readable_with_the_key(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str, sample_plan: DevelopmentPlan
):
    url = checkpoint_database_url
    with TestClient(make_app(fake_llm, url, key=KEY)) as encrypted:
        run_id = encrypted.post(RUNS, json=TASK).json()["run_id"]
    blobs = query(url, "SELECT blob FROM checkpoint_blobs WHERE thread_id = %s", run_id)
    writes = query(url, "SELECT blob FROM checkpoint_writes WHERE thread_id = %s", run_id)
    needle = sample_plan.summary.encode()
    assert blobs and all(needle not in bytes(row[0]) for row in blobs + writes)
    with TestClient(make_app(fake_llm, url, key=KEY)) as reader:
        payload = reader.get(f"{RUNS}/{run_id}").json()
    assert payload["status"] == "waiting_approval"
    assert payload["plan"]["summary"] == sample_plan.summary


# 5 + 9 -----------------------------------------------------------------------


def halt_after_planner(url: str, llm: FakeLLMAdapter, run_id: str) -> None:
    """Simulate a process that died right after the planner checkpoint.

    The row exists (the service creates it before the graph) but was never
    projected past ``running``: exactly what a crash leaves behind.
    """

    async def _run() -> None:
        database = Database(url)
        checkpointer = PostgresCheckpointer(libpq_dsn(url))
        saver = await checkpointer.open(wait=True)
        try:
            repository = SqlAlchemyRunRepository(database.session_factory)
            await repository.create_run(
                uuid.UUID(run_id),
                TASK["task"],
                owner="dead-process",
                ttl_seconds=0.001,
                owner_subject="local-dev",
                project_id="default",
            )
            halted = build_state_graph(llm).compile(checkpointer=saver, interrupt_after=["planner"])
            await halted.ainvoke(TASK, {"configurable": {"thread_id": run_id}})
        finally:
            await checkpointer.close()
            await database.dispose()

    asyncio.run(_run())
    time.sleep(0.05)  # let the 1 ms lease of the dead process expire


def test_incomplete_checkpoint_reads_as_interrupted_not_waiting_approval(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    run_id = str(uuid.uuid4())
    halt_after_planner(url, fake_llm, run_id)

    with TestClient(make_app(fake_llm, url)) as client:
        payload = client.get(f"{RUNS}/{run_id}").json()
        assert payload["status"] == "interrupted"
        assert payload["plan"] is not None and payload["architecture"] is None
        assert payload["error"] is None
        denied = client.post(f"{RUNS}/{run_id}/approve", json={"approved": True})
        assert denied.status_code == 409 and "interrupted" in denied.json()["detail"]
        # A real interrupt is still reported as such.
        live = client.post(RUNS, json=TASK).json()
        assert live["status"] == "waiting_approval"


def test_recovery_continues_from_the_last_checkpoint_without_repeating_the_planner(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    run_id = str(uuid.uuid4())
    halt_after_planner(url, fake_llm, run_id)
    assert calls_by_schema(fake_llm) == {"DevelopmentPlan": 1}

    async def recover() -> str:
        database = Database(url)
        checkpointer = PostgresCheckpointer(libpq_dsn(url))
        saver = await checkpointer.open(wait=True)
        try:
            repository = SqlAlchemyRunRepository(database.session_factory)
            service = RunService(build_graph(fake_llm, checkpointer=saver), repository)
            principal = DisabledAuthentication().principal()
            assert (await service.get(run_id, principal)).status == "interrupted"
            await service.graph.ainvoke(None, {"configurable": {"thread_id": run_id}})
            return str((await service.get(run_id, principal)).status)
        finally:
            await checkpointer.close()
            await database.dispose()

    assert asyncio.run(recover()) == "waiting_approval"
    assert calls_by_schema(fake_llm) == {"DevelopmentPlan": 1, "ArchitectureReview": 1}

    with TestClient(make_app(fake_llm, url)) as client:
        approved = client.post(f"{RUNS}/{run_id}/approve", json={"approved": True})
        assert approved.json()["status"] == "approved"
    assert ArchitectureReview in {schema for _, _, schema in fake_llm.calls}


# 6 ---------------------------------------------------------------------------


def connections(url: str, application_name: str) -> int:
    rows = query(
        url,
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND application_name = %s",
        application_name,
    )
    return int(rows[0][0])  # type: ignore[arg-type]


def test_shutdown_waits_and_closes_every_connection(
    fake_llm: FakeLLMAdapter, fresh_checkpoint_database_url: str
):
    url = fresh_checkpoint_database_url
    app = make_app(fake_llm, url)
    with TestClient(app) as client:
        client.post(RUNS, json=TASK)
        assert connections(url, APP_NAME) >= 1
        assert connections(url, "agentforge") >= 1
        checkpoints = app.state.checkpoints
        assert checkpoints.closed is False
        assert app.state.run_service.active_run_ids == frozenset()
    assert checkpoints.closed is True
    assert connections(url, APP_NAME) == 0
    assert connections(url, "agentforge") == 0


def test_startup_aborts_when_checkpoint_tables_are_missing(
    fake_llm: FakeLLMAdapter, fresh_migrated_database_url: str
):
    app = make_app(fake_llm, fresh_migrated_database_url)
    with pytest.raises(CheckpointSchemaError, match="setup_checkpoints"):
        with TestClient(app):
            pass


def test_ready_reports_persistent_checkpoints(client: TestClient):
    assert client.get("/health/ready").json() == {
        "status": "ok",
        "database": "ok",
        "schema": "ok",
        "checkpoints": "ok",
        "notifications": "disabled",
    }


def test_setup_script_creates_tables_idempotently_and_check_mode_reports_state(
    fresh_migrated_database_url: str,
):
    env = {
        **os.environ,
        "DATABASE_URL": fresh_migrated_database_url,
        "ANTHROPIC_VERTEX_PROJECT_ID": "test-project",
        "USE_FAKE_MODEL": "true",
    }
    script = [sys.executable, "-m", "scripts.setup_checkpoints"]

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*script, *args], env=env, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
        )

    missing = run("--check")
    assert missing.returncode == 1 and "DESACTUALIZADO" in missing.stdout
    created = run()
    assert created.returncode == 0, created.stderr
    assert f"v{expected_migration_version()}" in created.stdout
    again = run()
    assert again.returncode == 0 and "Nada que hacer" in again.stdout
    check = run("--check")
    assert check.returncode == 0 and "OK" in check.stdout
    assert fresh_migrated_database_url.split("@")[-1] not in created.stdout

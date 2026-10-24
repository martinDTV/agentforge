"""Business persistence and audit against PostgreSQL (repository + checkpointer)."""

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.contracts.lifecycle import RunEventKind
from app.contracts.planning import ArchitectureReview, DevelopmentPlan
from app.infrastructure.checkpoints import PostgresCheckpointer
from app.infrastructure.db import Database, SqlAlchemyRunRepository
from app.infrastructure.llm.errors import LLMTimeoutError
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.orchestration.graph import build_graph
from app.orchestration.ports import RunEvent
from app.orchestration.runs import RunService
from tests.integration.conftest import TemporaryDatabases, libpq_dsn, make_app
from tests.integration.test_checkpoints import query

RUNS = "/api/v1/runs"
TASK = {"task": "Crear módulo de pacientes con pruebas"}


def run_row(url: str, run_id: str) -> dict[str, Any]:
    rows = query(
        url,
        "SELECT status, revision, llm_calls, architect_approved, plan, architecture, "
        "error_code, error_message, last_checkpoint_id, attempt FROM runs WHERE id = %s",
        run_id,
    )
    assert len(rows) == 1
    keys = (
        "status",
        "revision",
        "llm_calls",
        "architect_approved",
        "plan",
        "architecture",
        "error_code",
        "error_message",
        "last_checkpoint_id",
        "attempt",
    )
    return dict(zip(keys, rows[0], strict=True))


def events(url: str, run_id: str) -> list[tuple[str, int, dict[str, Any]]]:
    return [
        (str(kind), int(ordinal), dict(payload))  # type: ignore[arg-type]
        for kind, ordinal, payload in query(
            url,
            "SELECT kind, ordinal, payload FROM run_events WHERE run_id = %s ORDER BY id",
            run_id,
        )
    ]


def approval_row(url: str, run_id: str) -> tuple[bool, bool] | None:
    rows = query(
        url, "SELECT approved, applied_at IS NOT NULL FROM run_approvals WHERE run_id = %s", run_id
    )
    return (bool(rows[0][0]), bool(rows[0][1])) if rows else None


def latest_checkpoint_id(url: str, run_id: str) -> str | None:
    async def _read() -> str | None:
        checkpointer = PostgresCheckpointer(libpq_dsn(url))
        saver = await checkpointer.open(wait=True)
        try:
            found = await saver.aget_tuple({"configurable": {"thread_id": run_id}})
            return None if found is None else str(found.config["configurable"]["checkpoint_id"])
        finally:
            await checkpointer.close()

    return asyncio.run(_read())


# 1 ---------------------------------------------------------------------------


def test_run_is_persisted_in_runs_and_readable_from_another_instance(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    with TestClient(make_app(fake_llm, url)) as first:
        created = first.post(RUNS, json=TASK).json()
    run_id = created["run_id"]
    row = run_row(url, run_id)
    assert row["status"] == "waiting_approval"
    assert (row["revision"], row["llm_calls"], row["architect_approved"], row["attempt"]) == (
        0,
        2,
        True,
        1,
    )
    assert row["plan"]["summary"] == created["plan"]["summary"]
    assert row["architecture"]["approved"] is True
    assert row["last_checkpoint_id"] == latest_checkpoint_id(url, run_id) is not None
    with TestClient(make_app(fake_llm, url)) as second:
        assert second.get(f"{RUNS}/{run_id}").json() == created


# 2 ---------------------------------------------------------------------------


def test_errors_and_final_results_are_persisted(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    with TestClient(make_app(fake_llm, url)) as client:
        ok = client.post(RUNS, json=TASK).json()
        client.post(f"{RUNS}/{ok['run_id']}/approve", json={"approved": True})
        fake_llm.register_sequence(DevelopmentPlan, [LLMTimeoutError("sin respuesta")])
        failed = client.post(RUNS, json=TASK).json()

    approved = run_row(url, ok["run_id"])
    assert approved["status"] == "approved" and approved["error_code"] is None
    assert approval_row(url, ok["run_id"]) == (True, True)

    broken = run_row(url, failed["run_id"])
    assert broken["status"] == "failed"
    assert (broken["error_code"], broken["error_message"]) == ("llm_timeout", "sin respuesta")
    assert broken["plan"] is None and broken["llm_calls"] == 1
    assert [e[:2] for e in events(url, failed["run_id"])] == [("run_created", 0), ("run_failed", 0)]
    assert events(url, failed["run_id"])[-1][2] == {"node": "planner", "code": "llm_timeout"}


# 3 ---------------------------------------------------------------------------


def test_audit_trail_of_the_planner_architect_cycle(
    fake_llm: FakeLLMAdapter,
    checkpoint_database_url: str,
    sample_plan: DevelopmentPlan,
    revised_plan: DevelopmentPlan,
    sample_review: ArchitectureReview,
    rejecting_review: ArchitectureReview,
):
    url = checkpoint_database_url
    fake_llm.register_sequence(DevelopmentPlan, [sample_plan, revised_plan])
    fake_llm.register_sequence(ArchitectureReview, [rejecting_review, sample_review])
    with TestClient(make_app(fake_llm, url)) as client:
        run_id = client.post(RUNS, json=TASK).json()["run_id"]
        client.post(f"{RUNS}/{run_id}/approve", json={"approved": True})

    trail = events(url, run_id)
    assert [e[:2] for e in trail] == [
        ("run_created", 0),
        ("planner_completed", 0),
        ("architect_reviewed", 0),
        ("revision_requested", 1),
        ("planner_completed", 1),
        ("architect_reviewed", 1),
        ("waiting_approval", 1),
        ("approval_recorded", 0),
        ("run_approved", 0),
        ("approval_applied", 0),
    ]
    by_kind = {(k, o): p for k, o, p in trail}
    assert by_kind[("architect_reviewed", 0)] == {
        "revision": 0,
        "approved": False,
        "issues": 1,
        "decisions": 0,
    }
    assert by_kind[("planner_completed", 1)] == {"revision": 1, "tasks": 2, "risks": 0}
    assert by_kind[("approval_recorded", 0)] == {"approved": True}
    flat = str([p for _, _, p in trail])
    for sensitive in (TASK["task"], sample_plan.summary, rejecting_review.issues[0]):
        assert sensitive not in flat


# 4 ---------------------------------------------------------------------------


def test_retrying_event_writes_does_not_duplicate_rows(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    with TestClient(make_app(fake_llm, url)) as client:
        run_id = client.post(RUNS, json=TASK).json()["run_id"]
        for _ in range(3):
            client.get(f"{RUNS}/{run_id}")
    before = events(url, run_id)

    async def retry() -> int:
        database = Database(url)
        try:
            repository = SqlAlchemyRunRepository(database.session_factory)
            return await repository.append_events(
                uuid.UUID(run_id),
                [
                    RunEvent(kind=RunEventKind.PLANNER_COMPLETED, ordinal=0),
                    RunEvent(kind=RunEventKind.WAITING_APPROVAL, ordinal=0),
                    RunEvent(kind=RunEventKind.RUN_CREATED),
                ],
            )
        finally:
            await database.dispose()

    assert asyncio.run(retry()) == 0
    assert events(url, run_id) == before


# 5 ---------------------------------------------------------------------------


def test_reconciliation_after_a_crash_between_checkpoint_and_projection(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    run_id = str(uuid.uuid4())

    async def crash_after_checkpoint() -> None:
        database = Database(url)
        checkpointer = PostgresCheckpointer(libpq_dsn(url))
        saver = await checkpointer.open(wait=True)
        try:
            repository = SqlAlchemyRunRepository(database.session_factory)
            await repository.create_run(
                uuid.UUID(run_id), TASK["task"], owner="dead-process", ttl_seconds=0.001
            )
            # Graph completes and checkpoints; the projection never happens.
            await build_graph(fake_llm, checkpointer=saver).ainvoke(
                TASK, {"configurable": {"thread_id": run_id}}
            )
        finally:
            await checkpointer.close()
            await database.dispose()

    asyncio.run(crash_after_checkpoint())
    time.sleep(0.05)  # let the 1 ms lease of the dead process expire
    stale = run_row(url, run_id)
    assert stale["status"] == "running" and stale["last_checkpoint_id"] is None

    with TestClient(make_app(fake_llm, url)) as client:
        payload = client.get(f"{RUNS}/{run_id}").json()
    assert payload["status"] == "waiting_approval" and payload["plan"] is not None
    repaired = run_row(url, run_id)
    assert repaired["status"] == "waiting_approval"
    assert repaired["last_checkpoint_id"] == latest_checkpoint_id(url, run_id)
    assert repaired["llm_calls"] == 2


# 6 + 7 ------------------------------------------------------------------------


def test_decision_is_durable_before_resume_and_survives_a_crash(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    app = make_app(fake_llm, url)
    with TestClient(app, raise_server_exceptions=False) as client:
        run_id = client.post(RUNS, json=TASK).json()["run_id"]
        calls_before = len(fake_llm.calls)
        service = app.state.run_service

        async def crash(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("process died before Command(resume=...)")

        original = service._apply_approval
        service._apply_approval = crash  # type: ignore[method-assign]
        assert client.post(f"{RUNS}/{run_id}/approve", json={"approved": True}).status_code == 500
        service._apply_approval = original  # type: ignore[method-assign]

        assert approval_row(url, run_id) == (True, False)
        assert run_row(url, run_id)["status"] == "resume_pending"
        assert client.get(f"{RUNS}/{run_id}").json()["status"] == "resume_pending"
        # Repeating the same decision is accepted idempotently; the opposite is refused.
        assert client.post(f"{RUNS}/{run_id}/approve", json={"approved": True}).status_code == 200
        assert client.post(f"{RUNS}/{run_id}/approve", json={"approved": False}).status_code == 409

    async def recover_from_a_new_process() -> str:
        database = Database(url)
        checkpointer = PostgresCheckpointer(libpq_dsn(url))
        saver = await checkpointer.open(wait=True)
        try:
            repository = SqlAlchemyRunRepository(database.session_factory)
            service = RunService(build_graph(fake_llm, checkpointer=saver), repository)
            return str((await service.apply_pending_approval(run_id)).status)
        finally:
            await checkpointer.close()
            await database.dispose()

    assert asyncio.run(recover_from_a_new_process()) == "approved"
    assert approval_row(url, run_id) == (True, True)
    assert run_row(url, run_id)["status"] == "approved"
    assert [e[:2] for e in events(url, run_id)][-3:] == [
        ("approval_recorded", 0),
        ("run_approved", 0),
        ("approval_applied", 0),
    ]
    assert len(fake_llm.calls) == calls_before


# 8 ---------------------------------------------------------------------------


def test_concurrent_duplicate_and_contradictory_approvals(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    with TestClient(make_app(fake_llm, url)) as client:
        run_id = client.post(RUNS, json=TASK).json()["run_id"]
        calls_before = len(fake_llm.calls)
        decisions = [True, False, True, False, True, False]

        def approve(decision: bool) -> tuple[int, bool, str]:
            response = client.post(f"{RUNS}/{run_id}/approve", json={"approved": decision})
            body = response.json()
            outcome = body["status"] if response.status_code == 200 else body["code"]
            return response.status_code, decision, str(outcome)

        with ThreadPoolExecutor(max_workers=len(decisions)) as pool:
            results = list(pool.map(approve, decisions))

    recorded = approval_row(url, run_id)
    assert recorded is not None and recorded[1] is True
    winner = recorded[0]
    final = "approved" if winner else "rejected"
    for status_code, decision, outcome in results:
        if decision == winner:
            assert (status_code, outcome) == (200, final)
        else:
            assert (status_code, outcome) == (409, "run_already_decided")
    assert run_row(url, run_id)["status"] == final
    assert len(fake_llm.calls) == calls_before
    assert sum(1 for k, _, _ in events(url, run_id) if k == "approval_recorded") == 1


# 9 ---------------------------------------------------------------------------


def test_postgres_outage_becomes_503_problem_details(
    fake_llm: FakeLLMAdapter,
    temporary_databases: TemporaryDatabases,
    fresh_checkpoint_database_url: str,
):
    url = fresh_checkpoint_database_url
    with TestClient(make_app(fake_llm, url), raise_server_exceptions=False) as client:
        run_id = client.post(RUNS, json=TASK).json()["run_id"]
        temporary_databases.set_allow_connections(url, False)
        try:
            for response in (
                client.post(RUNS, json=TASK),
                client.get(f"{RUNS}/{run_id}"),
                client.post(f"{RUNS}/{run_id}/approve", json={"approved": True}),
            ):
                assert response.status_code == 503, response.text
                assert response.headers["content-type"] == "application/problem+json"
                assert response.headers["Retry-After"] == "5"
                body = response.json()
                assert body["code"] == "database_unavailable"
                assert "postgresql" not in response.text.lower()
                assert "5432" not in response.text
        finally:
            temporary_databases.set_allow_connections(url, True)
        # Recovery is not instantaneous: the checkpointer pool reconnects with
        # backoff and readiness keeps saying 503 until then. Poll like a client would.
        deadline = time.monotonic() + 30
        while (status := client.get(f"{RUNS}/{run_id}").status_code) != 200:
            assert status == 503 and time.monotonic() < deadline, status
            time.sleep(0.5)
        assert client.post(f"{RUNS}/{run_id}/approve", json={"approved": True}).json()[
            "status"
        ] == ("approved")


# 10 --------------------------------------------------------------------------


def test_response_contract_is_a_superset_of_fase_2(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    with TestClient(make_app(fake_llm, checkpoint_database_url)) as client:
        payload = client.post(RUNS, json=TASK).json()
        unknown = client.get(f"{RUNS}/does-not-exist")
    assert set(payload) >= {
        "run_id",
        "status",
        "revision",
        "llm_calls",
        "architect_approved",
        "plan",
        "architecture",
        "error",
    }
    assert unknown.status_code == 404 and unknown.json()["code"] == "run_not_found"


@pytest.mark.parametrize("decision", [True, False])
def test_human_decision_is_reflected_in_row_approval_and_events(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str, decision: bool
):
    url = checkpoint_database_url
    with TestClient(make_app(fake_llm, url)) as client:
        run_id = client.post(RUNS, json=TASK).json()["run_id"]
        final = client.post(f"{RUNS}/{run_id}/approve", json={"approved": decision}).json()
    expected = "approved" if decision else "rejected"
    assert final["status"] == expected
    assert run_row(url, run_id)["status"] == expected
    assert approval_row(url, run_id) == (decision, True)
    assert ("run_" + expected, 0) in [e[:2] for e in events(url, run_id)]

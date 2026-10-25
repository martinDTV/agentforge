"""Leases, recovery, idempotency and shutdown against real PostgreSQL."""

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.contracts.lifecycle import RunLifecycleStatus
from app.contracts.planning import DevelopmentPlan
from app.infrastructure.checkpoints import PostgresCheckpointer
from app.infrastructure.db import Database, SqlAlchemyRunRepository
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.orchestration.graph import build_graph, build_state_graph
from app.orchestration.policy import RecoveryPolicy
from app.orchestration.ports import LeaseToken, RunProjection
from app.orchestration.recovery import RecoveryService
from app.orchestration.runs import RunService
from app.security import AuthorizationPolicy
from tests.integration.conftest import libpq_dsn, make_app
from tests.integration.test_business_persistence import approval_row, events, run_row
from tests.integration.test_checkpoints import query

T = TypeVar("T", bound=BaseModel)
S = RunLifecycleStatus
RUNS = "/api/v1/runs"
TASK = {"task": "Crear módulo de pacientes con pruebas"}
REPO_ROOT = Path(__file__).resolve().parents[2]
FAST = RecoveryPolicy(lease_ttl_seconds=1.5, heartbeat_seconds=0.4)


def cfg(run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": run_id}}


class GatedLLM:
    def __init__(self, inner: FakeLLMAdapter) -> None:
        self.inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate_structured(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        if schema is DevelopmentPlan:
            self.entered.set()
            assert self.release.wait(30), "la prueba no liberó al planner"
        return self.inner.generate_structured(system_prompt, user_prompt, schema)


@asynccontextmanager
async def components(
    url: str, llm: Any, policy: RecoveryPolicy = FAST, owner: str | None = None
) -> AsyncIterator[tuple[RunService, SqlAlchemyRunRepository]]:
    database = Database(url)
    checkpointer = PostgresCheckpointer(libpq_dsn(url))
    saver = await checkpointer.open(wait=True)
    try:
        repository = SqlAlchemyRunRepository(database.session_factory)
        service = RunService(
            build_graph(llm, checkpointer=saver),
            repository,
            policy,
            owner=owner,
            authorization=AuthorizationPolicy(separation_of_duties=False),
        )
        yield service, repository
    finally:
        await checkpointer.close()
        await database.dispose()


def expire_lease(url: str, run_id: str) -> None:
    query(
        url,
        "UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = %s RETURNING id",
        run_id,
    )


def lease_of(url: str, run_id: str) -> tuple[str | None, int, bool]:
    rows = query(
        url,
        "SELECT lease_owner, lease_generation, lease_expires_at > now() FROM runs WHERE id = %s",
        run_id,
    )
    owner, generation, active = rows[0]
    return (str(owner) if owner is not None else None, int(generation), bool(active))  # type: ignore[arg-type]


def abandoned_after_planner(url: str, llm: Any, *, attempt: int = 1) -> str:
    """Row + checkpoint as a process that died right after the planner leaves them."""
    run_id = str(uuid.uuid4())

    async def _run() -> None:
        async with components(url, llm, owner="dead-process") as (service, repository):
            await repository.create_run(
                uuid.UUID(run_id),
                TASK["task"],
                owner="dead-process",
                ttl_seconds=60,
                owner_subject="local-dev",
                project_id="default",
            )
            halted = build_state_graph(llm).compile(
                checkpointer=service.graph.checkpointer, interrupt_after=["planner"]
            )
            await halted.ainvoke(TASK, cfg(run_id))

    asyncio.run(_run())
    if attempt != 1:
        query(url, "UPDATE runs SET attempt = %s WHERE id = %s RETURNING id", attempt, run_id)
    expire_lease(url, run_id)
    return run_id


def sweep(url: str, llm: Any, policy: RecoveryPolicy = FAST, owner: str | None = None) -> dict:
    async def _sweep() -> dict[str, Any]:
        async with components(url, llm, policy, owner) as (service, _):
            report = await RecoveryService(service).sweep()
            return {"claimed": report.claimed, **report.outcomes}

    return asyncio.run(_sweep())


# 1 ---------------------------------------------------------------------------


def test_concurrent_lease_claims_in_postgres_have_exactly_one_winner(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    with TestClient(make_app(fake_llm, url)) as client:
        run_id = client.post(RUNS, json=TASK).json()["run_id"]  # lease released at the end
    assert lease_of(url, run_id) == (None, 1, False)

    def claim(index: int) -> LeaseToken | None:
        async def _claim() -> LeaseToken | None:
            database = Database(url)
            try:
                result = await SqlAlchemyRunRepository(database.session_factory).acquire_lease(
                    uuid.UUID(run_id),
                    owner=f"claimer-{index}",
                    ttl_seconds=60,
                    from_statuses=frozenset({S.WAITING_APPROVAL}),
                )
                return None if result is None else result[1]
            finally:
                await database.dispose()

        return asyncio.run(_claim())

    with ThreadPoolExecutor(max_workers=6) as pool:
        tokens = [t for t in pool.map(claim, range(6)) if t is not None]
    assert len(tokens) == 1
    assert lease_of(url, run_id) == (tokens[0].owner, 2, True)


# 2 + 3 -----------------------------------------------------------------------


def test_heartbeat_renews_in_postgres_and_a_lost_lease_cancels_the_drive(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    gated = GatedLLM(fake_llm)
    run_id = str(uuid.uuid4())
    app = make_app(gated, url, policy=FAST)
    with TestClient(app, raise_server_exceptions=False) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(
                lambda: client.post(RUNS, json=TASK, headers={"Idempotency-Key": run_id})
            )
            assert gated.entered.wait(15)
            owner_row = query(url, "SELECT id FROM runs WHERE idempotency_key = %s", run_id)
            rid = str(owner_row[0][0])
            owner, generation, active = lease_of(url, rid)
            assert active and generation == 1 and owner == app.state.run_service.owner
            first = query(url, "SELECT lease_expires_at FROM runs WHERE id = %s", rid)[0][0]
            time.sleep(1.0)  # > 2 heartbeats, < TTL
            second = query(url, "SELECT lease_expires_at FROM runs WHERE id = %s", rid)[0][0]
            assert second > first  # type: ignore[operator]
            assert client.get(f"{RUNS}/{rid}").json()["status"] == "running"

            # Takeover, as recovery would do after expiry: the driver must stop.
            expire_lease(url, rid)
            query(
                url,
                "UPDATE runs SET lease_owner = 'intruder', lease_expires_at = now() + interval "
                "'1 minute', lease_generation = lease_generation + 1 WHERE id = %s RETURNING id",
                rid,
            )
            response = request.result(timeout=15)
        gated.release.set()
    assert response.status_code == 409
    assert response.json()["code"] == "run_lease_lost"
    assert lease_of(url, rid) == ("intruder", 2, True)
    row = run_row(url, rid)
    # The loser never projected its (gated) planner result; the reader GET above
    # may have recorded the input checkpoint id, which is a legitimate read-side write.
    assert row["status"] == "running" and row["plan"] is None and row["llm_calls"] == 0


# 4 ---------------------------------------------------------------------------


def test_stale_owner_writes_are_rejected_by_postgres(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    run_id = str(uuid.uuid4())

    async def _scenario() -> None:
        database = Database(url)
        try:
            repository = SqlAlchemyRunRepository(database.session_factory)
            outcome = await repository.create_run(
                uuid.UUID(run_id),
                TASK["task"],
                owner="proc-a",
                ttl_seconds=60,
                owner_subject="local-dev",
                project_id="default",
            )
            old = outcome.lease
            assert old is not None
            expire_lease(url, run_id)
            taken = await repository.acquire_lease(
                uuid.UUID(run_id),
                owner="proc-b",
                ttl_seconds=60,
                from_statuses=frozenset({S.RUNNING}),
            )
            assert taken is not None
            new = taken[1]
            projection = RunProjection(status=S.WAITING_APPROVAL, checkpoint_id="1f00-cp-2")
            assert await repository.project(uuid.UUID(run_id), projection, lease=old) is None
            assert await repository.renew_lease(uuid.UUID(run_id), old, 60) is False
            assert await repository.release_lease(uuid.UUID(run_id), old) is False
            assert await repository.project(uuid.UUID(run_id), projection, lease=new) is not None
            older = RunProjection(status=S.WAITING_APPROVAL, checkpoint_id="1f00-cp-1")
            assert await repository.project(uuid.UUID(run_id), older) is None
            assert await repository.project(uuid.UUID(run_id), older, lease=new) is None
        finally:
            await database.dispose()

    asyncio.run(_scenario())
    assert lease_of(url, run_id) == ("proc-b", 2, True)


# 5 ---------------------------------------------------------------------------


def test_recovery_reprojects_abandoned_runs_and_backfills_audit(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    run_id = str(uuid.uuid4())

    async def _crash_after_interrupt() -> None:
        async with components(url, fake_llm, owner="dead-process") as (service, repository):
            await repository.create_run(
                uuid.UUID(run_id),
                TASK["task"],
                owner="dead-process",
                ttl_seconds=60,
                owner_subject="local-dev",
                project_id="default",
            )
            await service.graph.ainvoke(TASK, cfg(run_id))

    asyncio.run(_crash_after_interrupt())
    expire_lease(url, run_id)
    assert [e[:2] for e in events(url, run_id)] == [("run_created", 0)]

    assert sweep(url, fake_llm) == {"claimed": 1, "reprojected": 1}

    row = run_row(url, run_id)
    assert row["status"] == "waiting_approval" and row["llm_calls"] == 2
    assert lease_of(url, run_id) == (None, 2, False)
    assert [e[:2] for e in events(url, run_id)] == [
        ("run_created", 0),
        ("planner_completed", 0),
        ("architect_reviewed", 0),
        ("waiting_approval", 0),
    ]
    assert sweep(url, fake_llm) == {"claimed": 0}


# 6 ---------------------------------------------------------------------------


def test_recovery_applies_a_pending_approval_recorded_by_a_dead_process(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    with TestClient(make_app(fake_llm, url)) as client:
        run_id = client.post(RUNS, json=TASK).json()["run_id"]
    calls_before = len(fake_llm.calls)

    async def _claim_and_die() -> None:
        database = Database(url)
        try:
            claim = await SqlAlchemyRunRepository(database.session_factory).claim_approval(
                uuid.UUID(run_id),
                True,
                owner="dead-process",
                ttl_seconds=60,
                decided_by="approver-1",
            )
            assert claim is not None and claim.claimed
        finally:
            await database.dispose()

    asyncio.run(_claim_and_die())
    assert run_row(url, run_id)["status"] == "resume_pending"
    assert approval_row(url, run_id) == (True, False)
    # While the dead process' lease is still live nobody touches it.
    assert sweep(url, fake_llm) == {"claimed": 0}
    expire_lease(url, run_id)

    assert sweep(url, fake_llm) == {"claimed": 1, "approval_applied": 1}

    assert run_row(url, run_id)["status"] == "approved"
    assert approval_row(url, run_id) == (True, True)
    assert lease_of(url, run_id)[0] is None
    assert len(fake_llm.calls) == calls_before
    with TestClient(make_app(fake_llm, url)) as client:
        assert client.post(f"{RUNS}/{run_id}/approve", json={"approved": True}).status_code == 200
        assert client.post(f"{RUNS}/{run_id}/approve", json={"approved": False}).status_code == 409


# 7 ---------------------------------------------------------------------------


def test_two_concurrent_sweepers_split_abandoned_runs_with_skip_locked(
    fake_llm: FakeLLMAdapter, fresh_checkpoint_database_url: str
):
    url = fresh_checkpoint_database_url
    run_ids = [abandoned_after_planner(url, fake_llm) for _ in range(6)]
    policy = FAST.model_copy(update={"batch_size": 3})

    async def _race() -> list[dict[str, Any]]:
        async with (
            components(url, fake_llm, policy, owner="sweeper-a") as (a, _),
            components(url, fake_llm, policy, owner="sweeper-b") as (b, _),
        ):
            reports = await asyncio.gather(RecoveryService(a).sweep(), RecoveryService(b).sweep())
            return [{"claimed": r.claimed, **r.outcomes} for r in reports]

    reports = asyncio.run(_race())
    assert sum(r["claimed"] for r in reports) == 6
    assert sorted(r["claimed"] for r in reports) == [3, 3]
    for run_id in run_ids:
        assert run_row(url, run_id)["status"] == "interrupted"
        assert lease_of(url, run_id) == (None, 2, False)  # claimed exactly once
        assert [e[:2] for e in events(url, run_id)].count(("run_interrupted", 1)) == 1


# 8 ---------------------------------------------------------------------------


def test_idempotency_key_under_concurrency_creates_one_run(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    key = f"order-{uuid.uuid4().hex[:8]}"
    with TestClient(make_app(fake_llm, url)) as client:
        calls_before = len(fake_llm.calls)

        def create(_: int) -> tuple[int, str, str | None]:
            response = client.post(RUNS, json=TASK, headers={"Idempotency-Key": key})
            return (
                response.status_code,
                response.json()["run_id"],
                response.headers.get("Idempotency-Replayed"),
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(create, range(6)))
        statuses = sorted(code for code, _, _ in results)
        assert statuses == [200, 200, 200, 200, 200, 201]
        assert len({run_id for _, run_id, _ in results}) == 1
        assert all(replayed == "true" for code, _, replayed in results if code == 200)
        assert len(fake_llm.calls) - calls_before == 2
        run_id = results[0][1]
        assert client.get(f"{RUNS}/{run_id}").json()["status"] == "waiting_approval"

        conflict = client.post(
            RUNS,
            json={"task": "Una tarea distinta con la misma clave"},
            headers={"Idempotency-Key": key},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "idempotency_key_reused"
        too_long = client.post(RUNS, json=TASK, headers={"Idempotency-Key": "k" * 129})
        assert too_long.status_code == 422
    assert query(url, "SELECT count(*) FROM runs WHERE idempotency_key = %s", key)[0][0] == 1


# 9 + 12 ----------------------------------------------------------------------


def test_attempts_exhausted_and_auto_resume_without_repeating_nodes(
    fake_llm: FakeLLMAdapter, fresh_checkpoint_database_url: str
):
    # Private database: with auto_resume on, any interrupted run left by another
    # test in the shared database would be a legitimate candidate too.
    url = fresh_checkpoint_database_url
    auto = FAST.model_copy(update={"auto_resume": True, "max_attempts": 2})

    exhausted = abandoned_after_planner(url, fake_llm, attempt=2)
    assert sweep(url, fake_llm, auto) == {"claimed": 1, "abandoned": 1}
    row = run_row(url, exhausted)
    assert (row["status"], row["error_code"]) == ("failed", "abandoned")
    assert row["plan"] is not None and lease_of(url, exhausted)[0] is None

    recoverable = abandoned_after_planner(url, fake_llm)
    planner_calls = sum(1 for c in fake_llm.calls if c[2] is DevelopmentPlan)
    assert sweep(url, fake_llm, auto) == {"claimed": 1, "resumed": 1}
    row = run_row(url, recoverable)
    assert (row["status"], row["attempt"]) == ("waiting_approval", 2)
    assert sum(1 for c in fake_llm.calls if c[2] is DevelopmentPlan) == planner_calls
    assert ("run_resumed", 2) in [e[:2] for e in events(url, recoverable)]
    assert lease_of(url, recoverable)[0] is None
    with TestClient(make_app(fake_llm, url)) as client:
        assert (
            client.post(f"{RUNS}/{recoverable}/approve", json={"approved": True}).json()["status"]
            == "approved"
        )


# 10 --------------------------------------------------------------------------


def test_graceful_shutdown_of_the_app_cancels_and_releases_only_its_leases(
    fake_llm: FakeLLMAdapter, fresh_checkpoint_database_url: str
):
    url = fresh_checkpoint_database_url
    foreign = abandoned_after_planner(url, fake_llm)
    query(
        url,
        "UPDATE runs SET lease_owner = 'another-instance', lease_expires_at = now() + interval "
        "'1 minute' WHERE id = %s RETURNING id",
        foreign,
    )
    gated = GatedLLM(fake_llm)
    app = make_app(gated, url, policy=FAST, shutdown_grace_seconds=0.5)
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(lambda: client.post(RUNS, json=TASK))
        assert gated.entered.wait(15)
        rid = str(
            query(url, "SELECT id FROM runs WHERE lease_owner = %s", app.state.run_service.owner)[
                0
            ][0]
        )
        started = time.monotonic()
        # The LLM "returns" 2 s into the shutdown: after the 0.5 s grace and the
        # cancellation, while the event loop teardown is joining the worker thread.
        threading.Timer(2.0, gated.release.set).start()
        client.__exit__(None, None, None)  # drain 0.5 s, cancel, release leases, close pools
        exited_after = time.monotonic() - started
        response = request.result(timeout=15)
    assert 2.0 <= exited_after < 10, exited_after
    assert response.status_code == 503 and response.json()["code"] == "shutting_down"
    assert run_row(url, rid)["status"] == "running"  # never marked terminal
    assert lease_of(url, rid)[0] is None
    assert lease_of(url, foreign)[0] == "another-instance"  # not ours, untouched
    assert app.state.checkpoints.closed is True
    assert sweep(url, fake_llm) == {"claimed": 1, "interrupted": 1}
    assert run_row(url, rid)["status"] == "interrupted"


# 11 --------------------------------------------------------------------------


CHILD = textwrap.dedent(
    """
    import asyncio, os, sys, time, uuid
    from app.contracts.planning import DevelopmentPlan
    from app.infrastructure.checkpoints import PostgresCheckpointer
    from app.infrastructure.db import Database, SqlAlchemyRunRepository
    from app.infrastructure.llm.fake import FakeLLMAdapter
    from app.orchestration.graph import build_graph
    from app.orchestration.policy import RecoveryPolicy
    from app.orchestration.runs import RunService
    from app.security import AuthorizationPolicy
    from app.security.tokens import DisabledAuthentication
    from app.settings import LIBPQ_SCHEME, SQLALCHEMY_SCHEME

    url = os.environ["TEST_URL"]
    dsn = LIBPQ_SCHEME + url[len(SQLALCHEMY_SCHEME):]

    class Stuck:
        def generate_structured(self, system_prompt, user_prompt, schema):
            time.sleep(600)  # the process will be SIGKILLed long before this returns

    async def main():
        database = Database(url)
        checkpointer = PostgresCheckpointer(dsn)
        saver = await checkpointer.open(wait=True)
        service = RunService(
            build_graph(Stuck(), checkpointer=saver),
            SqlAlchemyRunRepository(database.session_factory),
            RecoveryPolicy(lease_ttl_seconds=1.5, heartbeat_seconds=0.4),
            owner="child-process",
            authorization=AuthorizationPolicy(separation_of_duties=False),
        )
        principal = DisabledAuthentication().principal()
        await service.create(os.environ["TEST_TASK"], principal, run_id=os.environ["TEST_RUN_ID"])

    asyncio.run(main())
    """
)


def test_recovery_after_sigkill_of_the_driving_process(
    fake_llm: FakeLLMAdapter, fresh_checkpoint_database_url: str
):
    # Private database: the sweeps below must see this run and nothing else.
    url = fresh_checkpoint_database_url
    run_id = str(uuid.uuid4())
    env = {
        **os.environ,
        "TEST_URL": url,
        "TEST_RUN_ID": run_id,
        "TEST_TASK": TASK["task"],
        "ANTHROPIC_VERTEX_PROJECT_ID": "test-project",
    }
    child = subprocess.Popen([sys.executable, "-c", CHILD], cwd=REPO_ROOT, env=env)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rows = query(url, "SELECT lease_owner FROM runs WHERE id = %s", run_id)
            if rows and rows[0][0] == "child-process":
                break
            time.sleep(0.1)
        else:
            pytest.fail("el proceso hijo no llegó a registrar su ejecución")
        time.sleep(1.0)  # let it heartbeat at least once while "thinking"
        assert lease_of(url, run_id)[2] is True
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()

    # The lease is still live: readers say running and recovery keeps its hands off.
    with TestClient(make_app(fake_llm, url)) as client:
        assert client.get(f"{RUNS}/{run_id}").json()["status"] == "running"
    assert sweep(url, fake_llm) == {"claimed": 0}

    # Once the dead process' lease expires, recovery classifies it from the checkpoint.
    deadline = time.monotonic() + 10
    while lease_of(url, run_id)[2] and time.monotonic() < deadline:
        time.sleep(0.2)
    report = sweep(url, fake_llm)
    assert report == {"claimed": 1, "interrupted": 1}
    row = run_row(url, run_id)
    assert row["status"] == "interrupted" and row["plan"] is None and row["attempt"] == 1
    assert lease_of(url, run_id)[0] is None
    assert ("run_interrupted", 1) in [e[:2] for e in events(url, run_id)]

    # Auto-resume re-drives it from the only checkpoint (the input) with the real fake.
    auto = FAST.model_copy(update={"auto_resume": True, "max_attempts": 3})
    assert sweep(url, fake_llm, auto) == {"claimed": 1, "resumed": 1}
    row = run_row(url, run_id)
    assert (row["status"], row["attempt"]) == ("waiting_approval", 2)
    with TestClient(make_app(fake_llm, url)) as client:
        assert client.get(f"{RUNS}/{run_id}").json()["status"] == "waiting_approval"

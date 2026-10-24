"""Leases, heartbeat, fencing, recovery, idempotency and shutdown with in-memory fakes."""

import asyncio
import threading
import uuid
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel

from app.contracts.lifecycle import RunLifecycleStatus
from app.contracts.planning import DevelopmentPlan
from app.infrastructure.db import InMemoryRunRepository
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.orchestration.exceptions import (
    IdempotencyKeyReusedError,
    LeaseLostError,
    ShuttingDownError,
)
from app.orchestration.graph import build_graph, build_state_graph
from app.orchestration.policy import RecoveryPolicy
from app.orchestration.ports import RunProjection
from app.orchestration.recovery import RecoveryService
from app.orchestration.runs import RunService

T = TypeVar("T", bound=BaseModel)
S = RunLifecycleStatus
TASK = "Diseña un módulo de pacientes con pruebas"
FAST = RecoveryPolicy(lease_ttl_seconds=0.9, heartbeat_seconds=0.15)
pytestmark = pytest.mark.anyio


def cfg(run_id: uuid.UUID) -> dict[str, Any]:
    return {"configurable": {"thread_id": str(run_id)}}


def kinds(events: list[Any]) -> list[tuple[str, int]]:
    return [(e.kind.value, e.ordinal) for e in events]


class GatedLLM:
    def __init__(self, inner: FakeLLMAdapter) -> None:
        self.inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate_structured(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        if schema is DevelopmentPlan:
            self.entered.set()
            assert self.release.wait(15), "la prueba no liberó al planner"
        return self.inner.generate_structured(system_prompt, user_prompt, schema)


def make(
    llm: Any,
    policy: RecoveryPolicy | None = None,
    repository: InMemoryRunRepository | None = None,
    owner: str = "proc-a",
) -> tuple[RunService, InMemoryRunRepository]:
    repo = repository or InMemoryRunRepository()
    return RunService(build_graph(llm), repo, policy or FAST, owner=owner), repo


async def abandoned_after_planner(
    service: RunService, repo: InMemoryRunRepository, llm: Any, *, attempt: int = 1
) -> uuid.UUID:
    """A run whose owner died right after the planner checkpoint, lease expired."""
    rid = uuid.uuid4()
    await repo.create_run(rid, TASK, owner="dead", ttl_seconds=60)
    halted = build_state_graph(llm).compile(
        checkpointer=service.graph.checkpointer, interrupt_after=["planner"]
    )
    await halted.ainvoke({"task": TASK}, cfg(rid))
    if attempt != 1:
        repo.runs[rid] = repo.runs[rid].model_copy(update={"attempt": attempt})
    repo.expire_lease(rid)
    return rid


# 1 ---------------------------------------------------------------------------


async def test_concurrent_claims_of_the_same_run_yield_one_owner(fake_llm: FakeLLMAdapter):
    _, repo = make(fake_llm)
    rid = uuid.uuid4()
    await repo.create_run(rid, TASK, owner="proc-a", ttl_seconds=60)
    live = await repo.acquire_lease(
        rid, owner="proc-b", ttl_seconds=60, from_statuses=frozenset({S.RUNNING})
    )
    assert live is None  # the lease is still active
    repo.expire_lease(rid)
    results = await asyncio.gather(
        *(
            repo.acquire_lease(
                rid, owner=f"proc-{i}", ttl_seconds=60, from_statuses=frozenset({S.RUNNING})
            )
            for i in range(5)
        )
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    _, token = winners[0]
    assert token.generation == 2 and repo.runs[rid].lease_owner == token.owner


# 2 ---------------------------------------------------------------------------


async def test_heartbeat_renews_the_lease_during_a_long_llm_call(fake_llm: FakeLLMAdapter):
    gated = GatedLLM(fake_llm)
    service, repo = make(gated)
    rid = uuid.uuid4()
    pending = asyncio.create_task(service.create(TASK, run_id=str(rid)))
    await asyncio.to_thread(gated.entered.wait, 10)
    first_expiry = repo.runs[rid].lease_expires_at
    ticks = 0
    for _ in range(6):  # ~0.6 s: several heartbeats while the LLM "thinks"
        await asyncio.sleep(0.1)
        ticks += 1
    assert ticks == 6  # the loop kept running
    assert len(repo.renewals) >= 2
    assert repo.runs[rid].lease_expires_at is not None and first_expiry is not None
    assert repo.runs[rid].lease_expires_at > first_expiry
    assert (await service.get(str(rid))).status == "running"
    gated.release.set()
    assert (await pending).response.status == "waiting_approval"
    assert repo.runs[rid].lease_owner is None  # released with the final projection


# 3 ---------------------------------------------------------------------------


async def test_losing_the_lease_cancels_the_drive_and_leaves_the_row_to_the_new_owner(
    fake_llm: FakeLLMAdapter,
):
    gated = GatedLLM(fake_llm)
    service, repo = make(gated)
    rid = uuid.uuid4()
    pending = asyncio.create_task(service.create(TASK, run_id=str(rid)))
    await asyncio.to_thread(gated.entered.wait, 10)
    # Another process took over (as recovery would after the lease expired).
    repo.expire_lease(rid)
    taken = await repo.acquire_lease(
        rid, owner="proc-b", ttl_seconds=60, from_statuses=frozenset({S.RUNNING})
    )
    assert taken is not None
    with pytest.raises(LeaseLostError):
        await pending
    gated.release.set()
    row = repo.runs[rid]
    assert row.lease_owner == "proc-b" and row.lease_generation == 2
    assert row.status is S.RUNNING and row.last_checkpoint_id is None  # loser wrote nothing
    assert service.active_run_ids == frozenset()


# 4 ---------------------------------------------------------------------------


async def test_previous_owner_cannot_write_after_a_takeover(fake_llm: FakeLLMAdapter):
    _, repo = make(fake_llm)
    rid = uuid.uuid4()
    outcome = await repo.create_run(rid, TASK, owner="proc-a", ttl_seconds=60)
    old = outcome.lease
    assert old is not None
    repo.expire_lease(rid)
    taken = await repo.acquire_lease(
        rid, owner="proc-b", ttl_seconds=60, from_statuses=frozenset({S.RUNNING})
    )
    assert taken is not None
    _, new = taken
    projection = RunProjection(status=S.WAITING_APPROVAL, checkpoint_id="cp-2")
    assert await repo.project(rid, projection, lease=old) is None
    assert await repo.renew_lease(rid, old, 60) is False
    assert await repo.release_lease(rid, old) is False
    assert await repo.project(rid, projection, lease=new) is not None
    # Monotonic checkpoint guard applies to everyone, lease or not.
    older = RunProjection(status=S.WAITING_APPROVAL, checkpoint_id="cp-1")
    assert await repo.project(rid, older) is None
    assert await repo.project(rid, older, lease=new) is None


# 5 ---------------------------------------------------------------------------


async def test_recovery_reprojects_an_abandoned_run_and_backfills_its_audit(
    fake_llm: FakeLLMAdapter,
):
    service, repo = make(fake_llm)
    rid = uuid.uuid4()
    await repo.create_run(rid, TASK, owner="dead", ttl_seconds=60)
    # The dead owner checkpointed up to the approval interrupt but wrote neither
    # the projection nor any audit event.
    await service.graph.ainvoke({"task": TASK}, cfg(rid))
    repo.expire_lease(rid)

    report = await RecoveryService(service).sweep()

    assert report.claimed == 1 and report.outcomes == {"reprojected": 1}
    row = repo.runs[rid]
    assert row.status is S.WAITING_APPROVAL and row.lease_owner is None
    assert row.last_checkpoint_id is not None and row.llm_calls == 2
    assert kinds(await repo.list_events(rid)) == [
        ("run_created", 0),
        ("planner_completed", 0),
        ("architect_reviewed", 0),
        ("waiting_approval", 0),
    ]
    # A second sweep finds nothing to do.
    assert (await RecoveryService(service).sweep()).claimed == 0


async def test_recovery_from_scratch_when_the_owner_died_before_any_checkpoint(
    fake_llm: FakeLLMAdapter,
):
    service, repo = make(fake_llm)
    rid = uuid.uuid4()
    await repo.create_run(rid, TASK, owner="dead", ttl_seconds=60)
    repo.expire_lease(rid)
    assert (await RecoveryService(service).sweep()).outcomes == {"interrupted": 1}
    assert repo.runs[rid].status is S.INTERRUPTED and repo.runs[rid].plan is None

    resuming, _ = make(fake_llm, FAST.model_copy(update={"auto_resume": True}), repo)
    resuming = RunService(service.graph, repo, FAST.model_copy(update={"auto_resume": True}))
    assert (await RecoveryService(resuming).sweep()).outcomes == {"resumed": 1}
    assert repo.runs[rid].status is S.WAITING_APPROVAL and repo.runs[rid].attempt == 2


# 6 ---------------------------------------------------------------------------


async def test_recovery_applies_a_decision_recorded_by_a_dead_process(fake_llm: FakeLLMAdapter):
    service, repo = make(fake_llm)
    rid = uuid.UUID((await service.create(TASK)).response.run_id)
    calls_before = len(fake_llm.calls)
    claim = await repo.claim_approval(rid, False, owner="dead", ttl_seconds=60)
    assert claim is not None and claim.claimed
    repo.expire_lease(rid)

    report = await RecoveryService(service).sweep()

    assert report.outcomes == {"approval_applied": 1}
    assert repo.runs[rid].status is S.REJECTED and repo.runs[rid].lease_owner is None
    assert repo.approvals[rid].applied_at is not None
    assert kinds(await repo.list_events(rid))[-3:] == [
        ("approval_recorded", 0),
        ("run_rejected", 0),
        ("approval_applied", 0),
    ]
    assert len(fake_llm.calls) == calls_before


# 7 ---------------------------------------------------------------------------


async def test_two_concurrent_sweepers_never_share_a_run(fake_llm: FakeLLMAdapter):
    policy = FAST.model_copy(update={"batch_size": 2})
    service_a, repo = make(fake_llm, policy, owner="sweeper-a")
    service_b = RunService(service_a.graph, repo, policy, owner="sweeper-b")
    rids = [await abandoned_after_planner(service_a, repo, fake_llm) for _ in range(4)]

    reports = await asyncio.gather(
        RecoveryService(service_a).sweep(), RecoveryService(service_b).sweep()
    )

    assert sum(r.claimed for r in reports) == 4
    assert [r.claimed for r in reports] == [2, 2]
    for rid in rids:
        row = repo.runs[rid]
        assert row.status is S.INTERRUPTED and row.lease_owner is None
        assert row.lease_generation == 2  # claimed exactly once
        assert kinds(await repo.list_events(rid)).count(("run_interrupted", 1)) == 1


# 8 ---------------------------------------------------------------------------


async def test_idempotent_creation_under_concurrency(fake_llm: FakeLLMAdapter):
    service, repo = make(fake_llm)
    results = await asyncio.gather(
        *(service.create(TASK, idempotency_key="order-42") for _ in range(5))
    )
    run_ids = {r.response.run_id for r in results}
    assert len(run_ids) == 1
    assert sum(1 for r in results if r.created) == 1
    assert len(fake_llm.calls) == 2  # one logical execution
    assert len([r for r in repo.runs.values() if r.idempotency_key == "order-42"]) == 1
    replay = await service.create(TASK, idempotency_key="order-42")
    assert replay.created is False and replay.response.status == "waiting_approval"
    with pytest.raises(IdempotencyKeyReusedError):
        await service.create("Otra tarea completamente distinta", idempotency_key="order-42")
    fresh = await service.create(TASK, idempotency_key="order-43")
    assert fresh.created and fresh.response.run_id not in run_ids


# 9 + 12 ----------------------------------------------------------------------


async def test_attempts_exhausted_marks_the_run_abandoned(fake_llm: FakeLLMAdapter):
    policy = FAST.model_copy(update={"auto_resume": True, "max_attempts": 2})
    service, repo = make(fake_llm, policy)
    rid = await abandoned_after_planner(service, repo, fake_llm, attempt=2)
    calls_before = len(fake_llm.calls)

    assert (await RecoveryService(service).sweep()).outcomes == {"abandoned": 1}

    row = repo.runs[rid]
    assert row.status is S.FAILED and row.error is not None and row.error.code == "abandoned"
    assert row.plan is not None and row.lease_owner is None
    assert ("run_failed", 0) in kinds(await repo.list_events(rid))
    assert len(fake_llm.calls) == calls_before


async def test_auto_resume_continues_from_the_checkpoint_without_repeating_nodes(
    fake_llm: FakeLLMAdapter,
):
    policy = FAST.model_copy(update={"auto_resume": True, "max_attempts": 3})
    service, repo = make(fake_llm, policy)
    rid = await abandoned_after_planner(service, repo, fake_llm)
    planner_calls = sum(1 for c in fake_llm.calls if c[2] is DevelopmentPlan)

    assert (await RecoveryService(service).sweep()).outcomes == {"resumed": 1}

    row = repo.runs[rid]
    assert row.status is S.WAITING_APPROVAL and row.attempt == 2 and row.lease_owner is None
    assert sum(1 for c in fake_llm.calls if c[2] is DevelopmentPlan) == planner_calls
    assert ("run_resumed", 2) in kinds(await repo.list_events(rid))
    assert ("architect_reviewed", 0) in kinds(await repo.list_events(rid))


async def test_default_policy_leaves_interrupted_runs_recoverable_not_failed(
    fake_llm: FakeLLMAdapter,
):
    service, repo = make(fake_llm)
    rid = await abandoned_after_planner(service, repo, fake_llm)
    report = await RecoveryService(service).sweep()
    assert report.outcomes == {"interrupted": 1}
    row = repo.runs[rid]
    assert row.status is S.INTERRUPTED and row.attempt == 1 and row.error is None
    assert ("run_interrupted", 1) in kinds(await repo.list_events(rid))
    # Not a candidate anymore while auto-resume is off.
    assert (await RecoveryService(service).sweep()).claimed == 0


# 10 --------------------------------------------------------------------------


async def test_graceful_shutdown_drains_cancels_and_releases_only_its_leases(
    fake_llm: FakeLLMAdapter,
):
    gated = GatedLLM(fake_llm)
    service, repo = make(gated)
    other = uuid.uuid4()
    await repo.create_run(other, TASK, owner="someone-else", ttl_seconds=60)
    rid = uuid.uuid4()
    pending = asyncio.create_task(service.create(TASK, run_id=str(rid)))
    await asyncio.to_thread(gated.entered.wait, 10)

    report = await service.shutdown(grace_seconds=0.2)

    assert report.drained is False and report.cancelled == [str(rid)]
    assert report.leases_released == 1
    with pytest.raises(ShuttingDownError):
        await pending
    with pytest.raises(ShuttingDownError):
        await service.create(TASK)
    gated.release.set()
    row = repo.runs[rid]
    assert row.status is S.RUNNING and row.lease_owner is None  # not marked terminal
    assert repo.runs[other].lease_owner == "someone-else"  # untouched
    # A reader (or recovery) sees pending work with no owner: recoverable.
    assert (await service.get(str(rid))).status == "interrupted"


async def test_shutdown_with_nothing_running_is_a_clean_drain(fake_llm: FakeLLMAdapter):
    service, _ = make(fake_llm)
    await service.create(TASK)
    report = await service.shutdown(grace_seconds=1)
    assert report.drained and report.cancelled == [] and report.leases_released == 0

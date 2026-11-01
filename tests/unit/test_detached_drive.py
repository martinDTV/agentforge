"""I0.1: ``create``, ``approve`` and ``resume`` answer as soon as the work is durable.

The drive runs in a tracked background task; the record of the work is the row,
its lease and the input checkpoint, never the task. Everything here is
deterministic: the model is gated with events, time never decides an outcome.
"""

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
    RunAlreadyDecidedError,
    RunBusyError,
    ShuttingDownError,
)
from app.orchestration.graph import build_graph
from app.orchestration.policy import RecoveryPolicy
from app.orchestration.ports import PersistedRun
from app.orchestration.recovery import RecoveryService
from app.orchestration.runs import RecoveryOutcome, RunService
from app.security import AuthorizationPolicy
from app.security.tokens import DisabledAuthentication

T = TypeVar("T", bound=BaseModel)
S = RunLifecycleStatus
TASK = "Diseña un módulo de pacientes con pruebas"
ADMIN = DisabledAuthentication().principal()
LENIENT = AuthorizationPolicy(separation_of_duties=False)
FAST = RecoveryPolicy(lease_ttl_seconds=0.9, heartbeat_seconds=0.15)
pytestmark = pytest.mark.anyio


def cfg(run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": run_id}}


def kinds(events: list[Any]) -> list[str]:
    return [e.kind.value for e in events]


class GatedLLM:
    """Blocks the planner until ``release`` is set; signals ``entered`` first."""

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
    repo: InMemoryRunRepository | None = None,
    *,
    owner: str = "api-1",
    graph: Any = None,
    settled: Any = None,
) -> tuple[RunService, InMemoryRunRepository]:
    repo = repo or InMemoryRunRepository()
    service = RunService(
        graph or build_graph(llm),
        repo,
        FAST,
        owner=owner,
        authorization=LENIENT,
        on_settled=settled,
    )
    return service, repo


# -- the response leaves before the graph runs --------------------------------------


async def test_create_answers_running_with_the_input_checkpointed(fake_llm: FakeLLMAdapter):
    gated = GatedLLM(fake_llm)
    service, repo = make(gated)
    created = (await service.create(TASK, ADMIN)).response
    rid = created.run_id

    assert created.status == "running" and service.drive_mode == "background"
    assert service.active_run_ids == frozenset({rid})
    row = repo.runs[uuid.UUID(rid)]
    assert row.status is S.RUNNING and row.lease_owner == service.owner
    snapshot = await service.graph.aget_state(cfg(rid))
    assert snapshot.values["task"] == TASK and snapshot.next == ("planner",)  # input only
    await asyncio.to_thread(gated.entered.wait, 10)  # the drive really started
    assert (await service.get(rid, ADMIN)).status == "running"

    gated.release.set()
    assert await service.wait_until_settled(rid, 10)
    assert (await service.get(rid, ADMIN)).status == "waiting_approval"
    assert service.active_run_ids == frozenset() and repo.runs[uuid.UUID(rid)].lease_owner is None
    assert kinds(await repo.list_events(uuid.UUID(rid))) == [
        "run_created",
        "planner_completed",
        "architect_reviewed",
        "waiting_approval",
    ]


async def test_approve_records_once_and_applies_in_the_background(fake_llm: FakeLLMAdapter):
    service, repo = make(fake_llm)
    rid = (await service.create(TASK, ADMIN)).response.run_id
    assert await service.wait_until_settled(rid, 10)
    calls_before = len(fake_llm.calls)

    decided = await service.approve(rid, True, ADMIN)

    assert decided.status in ("resume_pending", "approved")  # recorded; applied in the background
    assert len(repo.approvals) == 1  # durable before the response
    assert await service.wait_until_settled(rid, 10)
    assert (await service.get(rid, ADMIN)).status == "approved"
    assert len(fake_llm.calls) == calls_before  # applying a decision never calls the model
    events = kinds(await repo.list_events(uuid.UUID(rid)))
    assert events.count("approval_recorded") == 1
    assert events.count("run_approved") == 1 and events.count("approval_applied") == 1


async def test_concurrent_approvals_record_exactly_one_decision(fake_llm: FakeLLMAdapter):
    service, repo = make(fake_llm)
    rid = (await service.create(TASK, ADMIN)).response.run_id
    assert await service.wait_until_settled(rid, 10)
    calls_before = len(fake_llm.calls)

    outcomes = await asyncio.gather(
        service.approve(rid, True, ADMIN),
        service.approve(rid, False, ADMIN),
        service.approve(rid, True, ADMIN),
        return_exceptions=True,
    )

    errors = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], RunAlreadyDecidedError)
    assert await service.wait_until_settled(rid, 10)
    assert (await service.get(rid, ADMIN)).status == "approved"
    assert len(repo.approvals) == 1 and len(fake_llm.calls) == calls_before
    assert kinds(await repo.list_events(uuid.UUID(rid))).count("run_approved") == 1


# -- durability: the task is never the record ----------------------------------------


async def test_a_run_accepted_but_never_started_is_resumed_by_recovery(
    fake_llm: FakeLLMAdapter, monkeypatch: pytest.MonkeyPatch
):
    """The process dies between the HTTP response and the first node."""
    dead, repo = make(fake_llm, owner="dead-process")

    async def never_runs(rid: uuid.UUID, work: Any, *, purpose: str) -> None:
        work.close()  # the task was scheduled... and the process died before it ran

    monkeypatch.setattr(dead, "_dispatch", never_runs)
    created = (await dead.create(TASK, ADMIN)).response
    rid = uuid.UUID(created.run_id)
    assert created.status == "running"
    assert kinds(await repo.list_events(rid)) == ["run_created"]
    planner_calls = sum(1 for c in fake_llm.calls if c[2] is DevelopmentPlan)
    repo.expire_lease(rid)  # nobody renewed it

    survivor, _ = make(fake_llm, repo, owner="survivor", graph=dead.graph)
    report = await RecoveryService(survivor).sweep()

    assert report.outcomes == {"resumed": 1}
    assert await survivor.wait_until_settled(str(rid), 10)  # launched by the sweep (I0.2)
    row = repo.runs[rid]
    assert row.status is S.WAITING_APPROVAL and row.attempt == 2 and row.lease_owner is None
    assert sum(1 for c in fake_llm.calls if c[2] is DevelopmentPlan) == planner_calls + 1
    events = kinds(await repo.list_events(rid))
    assert events.count("run_created") == 1 and events.count("run_resumed") == 1
    assert events[-1] == "waiting_approval"
    assert (await RecoveryService(survivor).sweep()).claimed == 0  # nothing left to do


async def test_two_processes_never_drive_the_same_run(fake_llm: FakeLLMAdapter):
    gated = GatedLLM(fake_llm)
    alpha, repo = make(gated, owner="proc-a")
    rid = (await alpha.create(TASK, ADMIN)).response.run_id
    await asyncio.to_thread(gated.entered.wait, 10)
    beta, _ = make(gated, repo, owner="proc-b", graph=alpha.graph)

    # While the lease lives, the other process can neither decide nor recover it.
    with pytest.raises(RunAlreadyDecidedError):
        await beta.approve(rid, True, ADMIN)
    assert (await RecoveryService(beta).sweep()).claimed == 0

    # The lease expires (as if proc-a hung): proc-b takes over and proc-a stops itself.
    # The sweep launches the drive and returns at once (I0.2); the planner stays gated.
    repo.expire_lease(uuid.UUID(rid))
    report = await asyncio.wait_for(RecoveryService(beta).sweep(), 5)
    assert report.claimed == 1 and report.outcomes == {"resumed": 1}
    assert await alpha.wait_until_settled(rid, 10)  # proc-a's drive ended (lease lost)
    assert repo.runs[uuid.UUID(rid)].lease_owner == "proc-b"
    gated.release.set()
    assert await beta.wait_until_settled(rid, 10)

    row = repo.runs[uuid.UUID(rid)]
    assert row.status is S.WAITING_APPROVAL and row.lease_owner is None
    assert row.lease_generation == 2 and row.attempt == 2  # exactly one takeover
    events = kinds(await repo.list_events(uuid.UUID(rid)))
    assert events.count("waiting_approval") == 1 and events.count("planner_completed") == 1


async def test_shutdown_cancels_background_drives_and_recovery_resumes_them(
    fake_llm: FakeLLMAdapter,
):
    gated = GatedLLM(fake_llm)
    service, repo = make(gated)
    rid = (await service.create(TASK, ADMIN)).response.run_id
    await asyncio.to_thread(gated.entered.wait, 10)

    report = await service.shutdown(grace_seconds=0.2)

    assert report.drained is False and report.cancelled == [rid]
    with pytest.raises(ShuttingDownError):
        await service.create(TASK, ADMIN)
    gated.release.set()
    row = repo.runs[uuid.UUID(rid)]
    assert row.status is S.RUNNING and row.lease_owner is None  # not terminal; recoverable
    # Accepted, owner-less, never started: still ``running`` for the platform (the
    # sweep restarts it alone), never ``interrupted`` (which would need a human).
    assert (await service.get(rid, ADMIN)).status == "running"
    fresh, _ = make(gated, repo, owner="next-process", graph=service.graph)
    assert (await RecoveryService(fresh).sweep()).outcomes == {"resumed": 1}
    assert await fresh.wait_until_settled(rid, 10)
    assert repo.runs[uuid.UUID(rid)].status is S.WAITING_APPROVAL


async def test_a_launch_during_shutdown_leaves_the_row_to_recovery(fake_llm: FakeLLMAdapter):
    service, repo = make(fake_llm)
    rid = (await service.create(TASK, ADMIN)).response.run_id
    assert await service.wait_until_settled(rid, 10)
    service.stop_accepting()
    # A task already scheduled when the service stopped accepting does nothing.
    await service._supervise(uuid.UUID(rid), asyncio.sleep(0), "test")
    assert (await service.get(rid, ADMIN)).status == "waiting_approval"


async def test_only_one_background_drive_per_run_in_a_process(fake_llm: FakeLLMAdapter):
    gated = GatedLLM(fake_llm)
    service, _ = make(gated)
    rid = uuid.UUID((await service.create(TASK, ADMIN)).response.run_id)
    await asyncio.to_thread(gated.entered.wait, 10)
    with pytest.raises(RunBusyError):
        await service._dispatch(rid, asyncio.sleep(0), purpose="duplicate")
    gated.release.set()
    assert await service.wait_until_settled(str(rid), 10)


# -- the sweep launches, it never waits (I0.2) ---------------------------------------


async def never_started(
    fake_llm: Any, repo: InMemoryRunRepository | None, monkeypatch: pytest.MonkeyPatch
) -> tuple[uuid.UUID, RunService, InMemoryRunRepository]:
    """A run accepted by a process that died before its first node, lease expired."""
    dead, repo = make(fake_llm, repo, owner="dead-process")

    async def never_runs(rid: uuid.UUID, work: Any, *, purpose: str) -> None:
        work.close()

    monkeypatch.setattr(dead, "_dispatch", never_runs)
    rid = uuid.UUID((await dead.create(TASK, ADMIN)).response.run_id)
    repo.expire_lease(rid)
    return rid, dead, repo


async def test_the_sweep_launches_recovered_drives_and_returns(
    fake_llm: FakeLLMAdapter, monkeypatch: pytest.MonkeyPatch
):
    """The sweep classifies and launches under the lease it took; it never waits for
    the model, so other sweeps, reads and new work go on meanwhile."""
    gated = GatedLLM(fake_llm)
    rid, dead, repo = await never_started(gated, None, monkeypatch)
    survivor, _ = make(gated, repo, owner="survivor", graph=dead.graph)

    report = await asyncio.wait_for(RecoveryService(survivor).sweep(), 5)

    assert report.claimed == 1 and report.outcomes == {"resumed": 1}
    await asyncio.to_thread(gated.entered.wait, 10)
    assert not gated.release.is_set()  # the model is still "thinking"
    assert str(rid) in survivor.active_run_ids
    row = repo.runs[rid]
    assert row.status is S.RUNNING and row.lease_owner == "survivor" and row.attempt == 2
    assert (await asyncio.wait_for(survivor.get(str(rid), ADMIN), 5)).status == "running"
    assert (await asyncio.wait_for(RecoveryService(survivor).sweep(), 5)).claimed == 0
    other = (await asyncio.wait_for(survivor.create(TASK, ADMIN), 5)).response
    assert other.status == "running"

    gated.release.set()
    assert await survivor.wait_until_settled(str(rid), 10)
    assert await survivor.wait_until_settled(other.run_id, 10)
    row = repo.runs[rid]
    assert row.status is S.WAITING_APPROVAL and row.lease_owner is None
    events = kinds(await repo.list_events(rid))
    assert events.count("run_resumed") == 1 and events.count("waiting_approval") == 1


async def test_a_recovery_drive_cancelled_by_shutdown_is_picked_up_again(
    fake_llm: FakeLLMAdapter, monkeypatch: pytest.MonkeyPatch
):
    gated = GatedLLM(fake_llm)
    rid, dead, repo = await never_started(gated, None, monkeypatch)
    survivor, _ = make(gated, repo, owner="survivor", graph=dead.graph)
    assert (await RecoveryService(survivor).sweep()).outcomes == {"resumed": 1}
    await asyncio.to_thread(gated.entered.wait, 10)

    report = await survivor.shutdown(grace_seconds=0.2)

    assert report.drained is False and report.cancelled == [str(rid)]
    row = repo.runs[rid]
    assert row.status is S.RUNNING and row.lease_owner is None and row.attempt == 2
    gated.release.set()
    third, _ = make(gated, repo, owner="third", graph=dead.graph)
    assert (await RecoveryService(third).sweep()).outcomes == {"resumed": 1}
    assert await third.wait_until_settled(str(rid), 10)
    row = repo.runs[rid]
    assert row.status is S.WAITING_APPROVAL and row.attempt == 3 and row.lease_owner is None
    assert kinds(await repo.list_events(rid)).count("planner_completed") == 1


async def test_recovery_skips_a_run_this_process_already_drives(fake_llm: FakeLLMAdapter):
    """``RunBusyError`` inside ``recover``: the lease is released and the sweep moves on;
    the run is never driven twice in one process."""
    gated = GatedLLM(fake_llm)
    service, repo = make(gated)
    rid = uuid.UUID((await service.create(TASK, ADMIN)).response.run_id)
    await asyncio.to_thread(gated.entered.wait, 10)
    repo.expire_lease(rid)  # as if our own heartbeat had stalled
    claimed = await repo.claim_expired_leases(
        owner=service.owner, ttl_seconds=5, statuses=frozenset({S.RUNNING}), limit=1
    )
    assert len(claimed) == 1
    row, lease = claimed[0]

    outcome = await service.recover(row, lease)

    assert outcome is RecoveryOutcome.SKIPPED
    assert repo.runs[rid].lease_owner is None  # released for the next sweep
    assert repo.runs[rid].attempt == 1  # a skip never spends an attempt
    # The first drive notices the lost lease on its next heartbeat and stops without
    # projecting; only then is the gate released, so its late result is discarded.
    assert await service.wait_until_settled(str(rid), 10)
    assert (await RecoveryService(service).sweep()).outcomes == {"resumed": 1}
    gated.release.set()
    assert await service.wait_until_settled(str(rid), 10)
    assert repo.runs[rid].status is S.WAITING_APPROVAL and repo.runs[rid].attempt == 2
    assert kinds(await repo.list_events(rid)).count("waiting_approval") == 1


async def test_inline_recovery_still_returns_after_the_drive(
    fake_llm: FakeLLMAdapter, monkeypatch: pytest.MonkeyPatch
):
    """Operator scripts and engine tests keep the synchronous sweep."""
    rid, dead, repo = await never_started(fake_llm, None, monkeypatch)
    inline = RunService(
        dead.graph,
        repo,
        FAST,
        owner="inline-sweeper",
        authorization=LENIENT,
        drive_mode="inline",
    )
    assert (await RecoveryService(inline).sweep()).outcomes == {"resumed": 1}
    assert inline.active_run_ids == frozenset()
    assert repo.runs[rid].status is S.WAITING_APPROVAL  # already there when sweep returns


# -- hooks and modes -----------------------------------------------------------------


async def test_the_settled_hook_fires_after_each_background_stop(fake_llm: FakeLLMAdapter):
    seen: list[tuple[str, str]] = []

    async def settled(row: PersistedRun) -> None:
        seen.append((str(row.id), row.status.value))

    service, _ = make(fake_llm, settled=settled)
    rid = (await service.create(TASK, ADMIN)).response.run_id
    assert await service.wait_until_settled(rid, 10)
    await service.approve(rid, True, ADMIN)
    assert await service.wait_until_settled(rid, 10)
    assert seen == [(rid, "waiting_approval"), (rid, "approved")]


async def test_wait_until_settled_is_true_when_nothing_is_pending(fake_llm: FakeLLMAdapter):
    service, _ = make(fake_llm)
    assert await service.wait_until_settled(str(uuid.uuid4()), 0.01) is True


async def test_inline_mode_keeps_the_synchronous_outcome_for_operators(fake_llm: FakeLLMAdapter):
    service = RunService(
        build_graph(fake_llm),
        InMemoryRunRepository(),
        FAST,
        authorization=LENIENT,
        drive_mode="inline",
    )
    created = (await service.create(TASK, ADMIN)).response
    assert created.status == "waiting_approval" and service.active_run_ids == frozenset()
    assert (await service.approve(created.run_id, True, ADMIN)).status == "approved"

"""The unit-suite fake enforces the same rules as the PostgreSQL adapter."""

import uuid

import pytest

from app.contracts.lifecycle import RunEventKind, RunLifecycleStatus
from app.infrastructure.db import InMemoryRunRepository
from app.orchestration.exceptions import RunAlreadyExistsError
from app.orchestration.ports import RunEvent, RunProjection

pytestmark = pytest.mark.anyio
S = RunLifecycleStatus


async def test_create_registers_the_run_as_running_with_its_first_event():
    repo = InMemoryRunRepository()
    run_id = uuid.uuid4()
    outcome = await repo.create_run(run_id, "tarea", owner="p1", ttl_seconds=60)
    row = outcome.run
    assert outcome.created and outcome.lease is not None and outcome.lease.generation == 1
    assert row.status is S.RUNNING and row.attempt == 1 and row.lease_active
    assert [e.kind for e in await repo.list_events(run_id)] == [RunEventKind.RUN_CREATED]
    with pytest.raises(RunAlreadyExistsError):
        await repo.create_run(run_id, "tarea", owner="p1", ttl_seconds=60)


async def test_project_refuses_regressions_and_terminal_changes():
    repo = InMemoryRunRepository()
    run_id = uuid.uuid4()
    await repo.create_run(run_id, "tarea", owner="p1", ttl_seconds=60)
    assert await repo.project(run_id, RunProjection(status=S.WAITING_APPROVAL, checkpoint_id="1"))
    assert await repo.project(run_id, RunProjection(status=S.RUNNING)) is None
    repo.expire_lease(run_id)
    claim = await repo.claim_approval(run_id, True, owner="p2", ttl_seconds=60)
    assert claim is not None and claim.claimed and claim.lease is not None
    assert await repo.project(run_id, RunProjection(status=S.WAITING_APPROVAL)) is None
    approved = await repo.project(run_id, RunProjection(status=S.APPROVED, checkpoint_id="2"))
    assert approved is not None and approved.status is S.APPROVED
    assert await repo.project(run_id, RunProjection(status=S.FAILED)) is None
    assert await repo.project(uuid.uuid4(), RunProjection(status=S.FAILED)) is None


async def test_events_dedupe_on_kind_and_ordinal():
    repo = InMemoryRunRepository()
    run_id = uuid.uuid4()
    await repo.create_run(run_id, "tarea", owner="p1", ttl_seconds=60)
    events = [
        RunEvent(kind=RunEventKind.PLANNER_COMPLETED, ordinal=0),
        RunEvent(kind=RunEventKind.PLANNER_COMPLETED, ordinal=1),
    ]
    assert await repo.append_events(run_id, events) == 2
    assert await repo.append_events(run_id, events) == 0
    assert len(await repo.list_events(run_id)) == 3


async def test_claim_is_a_compare_and_set_with_one_decision_per_run():
    repo = InMemoryRunRepository()
    run_id = uuid.uuid4()
    assert await repo.claim_approval(run_id, True, owner="p9", ttl_seconds=60) is None
    await repo.create_run(run_id, "tarea", owner="p1", ttl_seconds=60)
    await repo.project(run_id, RunProjection(status=S.WAITING_APPROVAL), release=True)
    first = await repo.claim_approval(run_id, True, owner="p2", ttl_seconds=60)
    second = await repo.claim_approval(run_id, False, owner="p3", ttl_seconds=60)
    assert first is not None and first.claimed and first.status is S.RESUME_PENDING
    assert second is not None and not second.claimed and second.status is S.RESUME_PENDING
    assert second.existing is not None and second.existing.approved is True
    assert (await repo.get_approval(run_id)) is not None
    assert await repo.mark_approval_applied(run_id) is True
    assert await repo.mark_approval_applied(run_id) is False
    applied = await repo.get_approval(run_id)
    assert applied is not None and applied.applied_at is not None

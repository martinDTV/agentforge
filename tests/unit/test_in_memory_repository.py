"""The unit-suite fake enforces the same rules as the PostgreSQL adapter."""

import uuid

import pytest

from app.contracts.lifecycle import RunEventKind, RunLifecycleStatus
from app.infrastructure.db import InMemoryRunRepository
from app.orchestration.exceptions import RunAlreadyExistsError
from app.orchestration.ports import RunEvent, RunProjection
from app.security.tokens import DisabledAuthentication

pytestmark = pytest.mark.anyio
S = RunLifecycleStatus

# Local administrator of the unit suite: explicit, not implicit (see test_api_authz).
ADMIN = DisabledAuthentication().principal()


async def test_create_registers_the_run_as_running_with_its_first_event():
    repo = InMemoryRunRepository()
    run_id = uuid.uuid4()
    outcome = await repo.create_run(
        run_id,
        "tarea",
        owner="p1",
        ttl_seconds=60,
        owner_subject=ADMIN.subject,
        project_id="default",
    )
    row = outcome.run
    assert outcome.created and outcome.lease is not None and outcome.lease.generation == 1
    assert row.status is S.RUNNING and row.attempt == 1 and row.lease_active
    assert [e.kind for e in await repo.list_events(run_id)] == [RunEventKind.RUN_CREATED]
    with pytest.raises(RunAlreadyExistsError):
        await repo.create_run(
            run_id,
            "tarea",
            owner="p1",
            ttl_seconds=60,
            owner_subject=ADMIN.subject,
            project_id="default",
        )


async def test_project_refuses_regressions_and_terminal_changes():
    repo = InMemoryRunRepository()
    run_id = uuid.uuid4()
    await repo.create_run(
        run_id,
        "tarea",
        owner="p1",
        ttl_seconds=60,
        owner_subject=ADMIN.subject,
        project_id="default",
    )
    assert await repo.project(run_id, RunProjection(status=S.WAITING_APPROVAL, checkpoint_id="1"))
    assert await repo.project(run_id, RunProjection(status=S.RUNNING)) is None
    repo.expire_lease(run_id)
    claim = await repo.claim_approval(
        run_id, True, owner="p2", ttl_seconds=60, decided_by=ADMIN.subject
    )
    assert claim is not None and claim.claimed and claim.lease is not None
    assert await repo.project(run_id, RunProjection(status=S.WAITING_APPROVAL)) is None
    approved = await repo.project(run_id, RunProjection(status=S.APPROVED, checkpoint_id="2"))
    assert approved is not None and approved.status is S.APPROVED
    assert await repo.project(run_id, RunProjection(status=S.FAILED)) is None
    assert await repo.project(uuid.uuid4(), RunProjection(status=S.FAILED)) is None


async def test_events_dedupe_on_kind_and_ordinal():
    repo = InMemoryRunRepository()
    run_id = uuid.uuid4()
    await repo.create_run(
        run_id,
        "tarea",
        owner="p1",
        ttl_seconds=60,
        owner_subject=ADMIN.subject,
        project_id="default",
    )
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
    assert (
        await repo.claim_approval(
            run_id, True, owner="p9", ttl_seconds=60, decided_by=ADMIN.subject
        )
        is None
    )
    await repo.create_run(
        run_id,
        "tarea",
        owner="p1",
        ttl_seconds=60,
        owner_subject=ADMIN.subject,
        project_id="default",
    )
    await repo.project(run_id, RunProjection(status=S.WAITING_APPROVAL), release=True)
    first = await repo.claim_approval(
        run_id, True, owner="p2", ttl_seconds=60, decided_by=ADMIN.subject
    )
    second = await repo.claim_approval(
        run_id, False, owner="p3", ttl_seconds=60, decided_by=ADMIN.subject
    )
    assert first is not None and first.claimed and first.status is S.RESUME_PENDING
    assert second is not None and not second.claimed and second.status is S.RESUME_PENDING
    assert second.existing is not None and second.existing.approved is True
    assert (await repo.get_approval(run_id)) is not None
    assert await repo.mark_approval_applied(run_id) is True
    assert await repo.mark_approval_applied(run_id) is False
    applied = await repo.get_approval(run_id)
    assert applied is not None and applied.applied_at is not None


async def test_two_stages_record_distinct_ordinals_and_a_duplicate_is_refused_like_postgres():
    from app.contracts.lifecycle import DecisionStage, WorkflowKind

    repo = InMemoryRunRepository()
    run_id = uuid.uuid4()
    await repo.create_run(
        run_id,
        "task",
        owner="p",
        ttl_seconds=60,
        owner_subject="olivia",
        project_id="sandbox",
        workflow_kind=WorkflowKind.DEVELOPMENT,
    )
    # A row that already occupies the publish ordinal (what pre-fix data looked like).
    await repo.append_events(
        run_id,
        [
            RunEvent(
                kind=RunEventKind.APPROVAL_RECORDED,
                ordinal=DecisionStage.PUBLISH.event_ordinal,
                payload={"stage": "legacy"},
            )
        ],
    )
    await repo.project(
        run_id,
        RunProjection(
            status=RunLifecycleStatus.WAITING_APPROVAL, pending_stage=DecisionStage.SCOPE
        ),
        release=True,
    )
    claim = await repo.claim_decision(
        run_id, DecisionStage.SCOPE, True, owner="api", ttl_seconds=30, decided_by="martin"
    )
    assert claim is not None and claim.claimed
    assert await repo.mark_decision_applied(run_id, DecisionStage.SCOPE)
    recorded = [
        (e.ordinal, e.payload.get("stage"))
        for e in repo.events[run_id]
        if e.kind is RunEventKind.APPROVAL_RECORDED
    ]
    assert recorded == [(2, "legacy"), (1, "scope")]
    await repo.project(
        run_id,
        RunProjection(
            status=RunLifecycleStatus.WAITING_APPROVAL, pending_stage=DecisionStage.PUBLISH
        ),
        release=True,
    )
    # The natural key is enforced like uq_run_events_run_id_kind_ordinal would be.
    with pytest.raises(ValueError, match="duplicate run_events key"):
        await repo.claim_decision(
            run_id, DecisionStage.PUBLISH, True, owner="api", ttl_seconds=30, decided_by="martin"
        )

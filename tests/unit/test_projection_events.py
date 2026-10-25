"""Projection rules and audit events, with no graph and no database."""

from langgraph.types import Interrupt

from app.contracts.lifecycle import RunEventKind, RunLifecycleStatus
from app.orchestration.events import events_from_update, revision_from_update, transition_events
from app.orchestration.projection import build_projection, projection_differs, resolve_status
from tests.unit.test_status import snapshot

S = RunLifecycleStatus
PLAN = {"summary": "s", "tasks": [{"id": "t1"}, {"id": "t2"}], "risks": ["r"]}


def test_resolve_status_keeps_terminal_and_recorded_decisions():
    assert resolve_status(S.APPROVED, S.INTERRUPTED) is S.APPROVED
    assert resolve_status(S.FAILED, S.WAITING_APPROVAL) is S.FAILED
    assert resolve_status(S.RESUME_PENDING, S.WAITING_APPROVAL) is S.RESUME_PENDING
    assert resolve_status(S.RESUME_PENDING, S.INTERRUPTED) is S.RESUME_PENDING
    assert resolve_status(S.RESUME_PENDING, S.APPROVED) is S.APPROVED
    assert resolve_status(S.RUNNING, S.WAITING_APPROVAL) is S.WAITING_APPROVAL


def test_build_projection_reads_counters_results_and_checkpoint_id():
    snap = snapshot(
        {"task": "t", "plan": PLAN, "revision": 1, "llm_calls": 4, "architect_approved": True},
        ("approval",),
        interrupts=(Interrupt(value={"kind": "approve_plan", "revision": 1}, id="i"),),
    )
    snap = snap._replace(config={"configurable": {"thread_id": "r", "checkpoint_id": "cp-9"}})
    projection = build_projection(snap, current=S.RUNNING, in_flight=True)
    assert projection.status is S.WAITING_APPROVAL
    assert (projection.revision, projection.llm_calls) == (1, 4)
    assert projection.plan == PLAN and projection.checkpoint_id == "cp-9"
    assert projection.error is None


def test_projection_differs_detects_checkpoint_drift_only_when_present():
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.orchestration.ports import PersistedRun, RunProjection

    now = datetime.now(UTC)
    row = PersistedRun(
        id=uuid4(),
        task="t",
        status=S.WAITING_APPROVAL,
        owner_subject="alice",
        project_id="alpha",
        last_checkpoint_id="cp-1",
        created_at=now,
        updated_at=now,
    )
    same = RunProjection(status=S.WAITING_APPROVAL, checkpoint_id="cp-1")
    drift = RunProjection(status=S.WAITING_APPROVAL, checkpoint_id="cp-2")
    assert projection_differs(row, same) is False
    assert projection_differs(row, drift) is True


def test_events_from_planner_architect_interrupt_and_approval_updates():
    assert revision_from_update({"planner": {"plan": PLAN, "revision": 2}}, 0) == 2
    assert revision_from_update({"architect": {}}, 1) == 1

    first_plan = events_from_update({"planner": {"plan": PLAN, "revision": 0}}, revision=0)
    assert [(e.kind, e.ordinal) for e in first_plan] == [(RunEventKind.PLANNER_COMPLETED, 0)]
    assert first_plan[0].payload == {"revision": 0, "tasks": 2, "risks": 1}

    revised = events_from_update({"planner": {"plan": PLAN, "revision": 1}}, revision=1)
    assert [(e.kind, e.ordinal) for e in revised] == [
        (RunEventKind.REVISION_REQUESTED, 1),
        (RunEventKind.PLANNER_COMPLETED, 1),
    ]

    review = events_from_update(
        {
            "architect": {
                "architecture": {"issues": ["a", "b"], "decisions": []},
                "architect_approved": False,
            }
        },
        revision=1,
        checkpoint_id="cp-3",
    )
    assert review[0].kind is RunEventKind.ARCHITECT_REVIEWED
    assert review[0].payload == {"revision": 1, "approved": False, "issues": 2, "decisions": 0}
    assert review[0].checkpoint_id == "cp-3"

    interrupt = events_from_update(
        {"__interrupt__": (Interrupt(value={"kind": "approve_plan", "revision": 1}, id="i"),)},
        revision=0,
    )
    assert [(e.kind, e.ordinal) for e in interrupt] == [(RunEventKind.WAITING_APPROVAL, 1)]

    failed = events_from_update({"planner": {"error": {"code": "llm_timeout"}}}, revision=0)
    assert failed[0].kind is RunEventKind.RUN_FAILED
    assert failed[0].payload == {"node": "planner", "code": "llm_timeout"}

    approved = events_from_update({"approval": {"approved": True}}, revision=1)
    rejected = events_from_update({"approval": {"approved": False}}, revision=1)
    assert approved[0].kind is RunEventKind.RUN_APPROVED
    assert rejected[0].kind is RunEventKind.RUN_REJECTED


def test_payloads_never_carry_prompts_or_plan_content():
    plan = {"summary": "SECRET SUMMARY", "tasks": [{"title": "SECRET TASK"}], "risks": []}
    events = events_from_update({"planner": {"plan": plan, "revision": 0}}, revision=0)
    assert "SECRET" not in str([e.payload for e in events])


def test_transition_events_cover_what_the_stream_cannot_report():
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.contracts.execution import RunError
    from app.orchestration.ports import PersistedRun

    now = datetime.now(UTC)

    def row(status: RunLifecycleStatus, **extra: object) -> PersistedRun:
        return PersistedRun(
            id=uuid4(),
            task="t",
            status=status,
            owner_subject="a",
            project_id="p",
            created_at=now,
            updated_at=now,
            **extra,
        )  # type: ignore[arg-type]

    assert transition_events(S.RUNNING, row(S.RUNNING)) == []
    interrupted = transition_events(S.RUNNING, row(S.INTERRUPTED, attempt=1))
    assert [(e.kind, e.ordinal) for e in interrupted] == [(RunEventKind.RUN_INTERRUPTED, 1)]
    failed = transition_events(
        S.RUNNING, row(S.FAILED, error=RunError(code="internal_error", message="m"))
    )
    assert failed[0].payload == {"code": "internal_error"}
    assert transition_events(S.RESUME_PENDING, row(S.REJECTED))[0].kind is RunEventKind.RUN_REJECTED

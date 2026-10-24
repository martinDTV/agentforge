"""Audit backfill from checkpoint history is evidence-based and dedupes."""

from langgraph.types import Interrupt

from app.contracts.lifecycle import RunEventKind
from app.orchestration.events import events_from_history
from tests.unit.test_status import snapshot

PLAN = {"summary": "s", "tasks": [{"id": "t1"}], "risks": []}
REVIEW = {"summary": "r", "decisions": [], "issues": ["x"], "approved": False}


def test_history_yields_one_event_per_observed_change():
    history = [
        snapshot({"task": "t"}, ("planner",)),
        snapshot({"task": "t", "plan": PLAN, "revision": 0, "llm_calls": 1}, ("architect",)),
        snapshot(
            {
                "task": "t",
                "plan": PLAN,
                "architecture": REVIEW,
                "architect_approved": False,
                "revision": 0,
                "llm_calls": 2,
            },
            ("planner",),
        ),
        snapshot(
            {
                "task": "t",
                "plan": {**PLAN, "summary": "v2"},
                "architecture": REVIEW,
                "architect_approved": False,
                "revision": 1,
                "llm_calls": 3,
            },
            ("architect",),
        ),
        snapshot(
            {
                "task": "t",
                "plan": {**PLAN, "summary": "v2"},
                "architecture": {**REVIEW, "approved": True, "issues": []},
                "architect_approved": True,
                "revision": 1,
                "llm_calls": 4,
            },
            ("approval",),
            interrupts=(Interrupt(value={"kind": "approve_plan", "revision": 1}, id="i"),),
        ),
    ]
    events = events_from_history(history)
    assert [(e.kind, e.ordinal) for e in events] == [
        (RunEventKind.PLANNER_COMPLETED, 0),
        (RunEventKind.ARCHITECT_REVIEWED, 0),
        (RunEventKind.REVISION_REQUESTED, 1),
        (RunEventKind.PLANNER_COMPLETED, 1),
        (RunEventKind.ARCHITECT_REVIEWED, 1),
        (RunEventKind.WAITING_APPROVAL, 1),
    ]
    assert events[1].payload == {"revision": 0, "approved": False, "issues": 1, "decisions": 0}


def test_history_without_changes_yields_nothing_and_errors_once():
    error = {"code": "llm_timeout", "message": "m"}
    history = [
        snapshot({"task": "t"}, ("planner",)),
        snapshot({"task": "t", "llm_calls": 1, "error": error}, ()),
        snapshot({"task": "t", "llm_calls": 1, "error": error}, ()),
    ]
    events = events_from_history(history)
    assert [(e.kind, e.ordinal) for e in events] == [(RunEventKind.RUN_FAILED, 0)]
    assert events[0].payload == {"code": "llm_timeout"}
    assert events_from_history([]) == []

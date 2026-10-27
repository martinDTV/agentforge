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
    assert events[1].payload == {
        "revision": 0,
        "approved": False,
        "issues": 1,
        "decisions": 0,
        "executed_by": "agentforge:architect",
    }


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


def test_development_history_yields_node_events_and_completion():
    changes = {"summary": "s", "edits": [{"path": "src/app.py", "content": "x"}]}
    review = {"approved": False, "summary": "r", "issues": [{"code": "x"}]}
    base = {"task": "t", "workflow_kind": "development"}
    history = [
        snapshot(base, ("developer",)),
        snapshot({**base, "changes": changes, "revision": 0}, ("apply_changes",)),
        snapshot(
            {
                **base,
                "changes": changes,
                "revision": 0,
                "developer_checks": [],
                "changed_files": ["src/app.py"],
            },
            ("qa",),
        ),
        snapshot(
            {
                **base,
                "changes": changes,
                "revision": 0,
                "developer_checks": [],
                "qa_review": review,
                "validation": [{"exit_code": 1}],
            },
            ("revise_development",),
        ),
        snapshot(
            {
                **base,
                "changes": changes,
                "revision": 1,
                "developer_checks": [],
                "qa_review": review,
            },
            ("developer",),
        ),
        # Same proposal again in revision 1: still a Developer round (revision moved).
        snapshot(
            {
                **base,
                "changes": changes,
                "revision": 1,
                "developer_checks": [],
                "qa_review": review,
            },
            ("apply_changes",),
        ),
        snapshot(
            {
                **base,
                "changes": changes,
                "revision": 1,
                "developer_checks": [],
                "qa_review": {**review, "approved": True, "issues": []},
                "validation": [{"exit_code": 0}],
                "completed": True,
            },
            (),
        ),
    ]
    events = events_from_history(history)
    assert [(e.kind, e.ordinal) for e in events] == [
        (RunEventKind.DEVELOPER_COMPLETED, 0),
        (RunEventKind.DEVELOPER_APPLIED, 0),
        (RunEventKind.QA_REVIEWED, 0),
        (RunEventKind.DEVELOPER_COMPLETED, 1),
        (RunEventKind.DEVELOPER_APPLIED, 1),
        (RunEventKind.QA_REVIEWED, 1),
        (RunEventKind.QA_REVIEWED, 1),
        (RunEventKind.RUN_COMPLETED, 0),
    ]
    assert events[2].payload == {
        "approved": False,
        "issues": 1,
        "checks": 1,
        "failed_checks": 1,
        "executed_by": "agentforge:qa",
    }

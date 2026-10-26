"""Status derivation from checkpoint snapshots (no graph, no database)."""

from typing import Any

import pytest
from langgraph.types import Interrupt, PregelTask, StateSnapshot

from app.orchestration.status import (
    INTERNAL_ERROR_CODE,
    derive_error,
    derive_status,
    run_response_from_snapshot,
)

TASK = {"task": "Diseña un módulo de pacientes"}


def snapshot(
    values: dict[str, Any],
    next_nodes: tuple[str, ...] = (),
    *,
    interrupts: tuple[Interrupt, ...] = (),
    tasks: tuple[PregelTask, ...] = (),
) -> StateSnapshot:
    return StateSnapshot(
        values=values,
        next=next_nodes,
        config={"configurable": {"thread_id": "r1"}},
        metadata=None,
        created_at=None,
        parent_config=None,
        tasks=tasks,
        interrupts=interrupts,
    )


def task(name: str, *, error: BaseException | None = None, interrupts: tuple[Interrupt, ...] = ()):
    return PregelTask(id="t", name=name, path=(), error=error, interrupts=interrupts)


APPROVAL_INTERRUPT = Interrupt(value={"kind": "approve_plan"}, id="i1")


def test_finished_runs_are_approved_or_rejected():
    assert derive_status(snapshot({**TASK, "approved": True})) == "approved"
    assert derive_status(snapshot({**TASK, "approved": False})) == "rejected"


def test_workflows_without_a_human_decision_end_completed_unless_they_failed():
    assert derive_status(snapshot({**TASK, "completed": True})) == "completed"
    assert (
        derive_status(snapshot({**TASK, "completed": False}, ("qa",)), in_flight=True) == "running"
    )
    failed = snapshot({**TASK, "completed": True, "error": {"code": "x", "message": "m"}})
    assert derive_status(failed) == "failed"


def test_error_in_state_is_failed_even_with_pending_nodes():
    snap = snapshot({**TASK, "error": {"code": "llm_timeout", "message": "m"}}, ("architect",))
    assert derive_status(snap) == "failed"
    assert derive_error(snap) is not None and derive_error(snap).code == "llm_timeout"


def test_task_that_raised_is_failed_with_internal_error():
    snap = snapshot(TASK, ("planner",), tasks=(task("planner", error=RuntimeError("boom")),))
    assert derive_status(snap) == "failed"
    error = derive_error(snap)
    assert error is not None and error.code == INTERNAL_ERROR_CODE
    assert "boom" not in error.message


def test_pending_interrupt_is_a_real_wait_for_approval():
    by_snapshot = snapshot(TASK, ("approval",), interrupts=(APPROVAL_INTERRUPT,))
    by_task = snapshot(
        TASK, ("approval",), tasks=(task("approval", interrupts=(APPROVAL_INTERRUPT,)),)
    )
    assert derive_status(by_snapshot) == "waiting_approval"
    assert derive_status(by_task) == "waiting_approval"
    # Being in flight does not change a real interrupt.
    assert derive_status(by_snapshot, in_flight=True) == "waiting_approval"


def test_pending_work_without_interrupt_is_running_or_interrupted():
    snap = snapshot({**TASK, "plan": {"summary": "p", "tasks": []}}, ("architect",))
    assert derive_status(snap, in_flight=True) == "running"
    assert derive_status(snap, in_flight=False) == "interrupted"
    assert derive_error(snap) is None


@pytest.mark.parametrize("in_flight", [True, False])
def test_run_response_reflects_in_flight_flag(in_flight: bool):
    response = run_response_from_snapshot("r1", snapshot(TASK, ("planner",)), in_flight=in_flight)
    assert response.status == ("running" if in_flight else "interrupted")
    assert response.plan is None and response.error is None and response.llm_calls == 0


def test_between_supersteps_is_work_in_progress_not_a_terminal_state():
    """All tasks of a step done, next checkpoint not yet written: next is empty."""
    between = snapshot({**TASK, "plan": {"summary": "p", "tasks": []}, "llm_calls": 1}, ())
    assert derive_status(between, in_flight=True) == "running"
    assert derive_status(between, in_flight=False) == "interrupted"
    assert derive_error(between) is None


def test_terminal_states_need_the_approval_nodes_evidence():
    assert derive_status(snapshot({**TASK, "approved": True}, ("ghost",))) == "approved"
    assert derive_status(snapshot({**TASK, "approved": False}, ())) == "rejected"
    assert derive_status(snapshot(TASK, ())) == "interrupted"

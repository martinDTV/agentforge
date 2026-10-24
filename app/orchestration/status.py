"""Translate a LangGraph checkpoint into the API's view of a run.

This is the only place that knows how ``RunState`` keys and snapshot
metadata map to ``RunStatus``; the HTTP layer never inspects the graph.
"""

from langgraph.types import StateSnapshot

from app.contracts.execution import RunError, RunResponse, RunStatus
from app.contracts.planning import ArchitectureReview, DevelopmentPlan

INTERNAL_ERROR_CODE = "internal_error"


def derive_error(snapshot: StateSnapshot) -> RunError | None:
    error = snapshot.values.get("error")
    if error:
        return RunError.model_validate(error)
    # A node raised something that was not an LLMError: LangGraph keeps the
    # failed task (with its exception) in the checkpoint. Surface it as a
    # terminal failure without leaking the exception itself.
    if any(task.error is not None for task in snapshot.tasks):
        return RunError(code=INTERNAL_ERROR_CODE, message="Error inesperado durante la ejecución.")
    return None


def derive_status(snapshot: StateSnapshot) -> RunStatus:
    if derive_error(snapshot) is not None:
        return "failed"
    if snapshot.next:
        return "waiting_approval"
    return "approved" if snapshot.values.get("approved") is True else "rejected"


def run_response_from_snapshot(run_id: str, snapshot: StateSnapshot) -> RunResponse:
    values = snapshot.values
    plan = values.get("plan")
    architecture = values.get("architecture")
    return RunResponse(
        run_id=run_id,
        status=derive_status(snapshot),
        revision=values.get("revision", 0),
        llm_calls=values.get("llm_calls", 0),
        architect_approved=values.get("architect_approved"),
        plan=DevelopmentPlan.model_validate(plan) if plan else None,
        architecture=ArchitectureReview.model_validate(architecture) if architecture else None,
        error=derive_error(snapshot),
    )

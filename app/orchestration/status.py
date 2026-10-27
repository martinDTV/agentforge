"""Translate a LangGraph checkpoint into the API's view of a run.

This is the only place that knows how ``RunState`` keys and snapshot
metadata map to ``RunStatus``; the HTTP layer never inspects the graph.

Status rules, in order. Terminal states need *positive* evidence: a recorded
error or the approval node's ``approved`` value. "Nothing left to run" is not
evidence of completion: between two supersteps LangGraph briefly exposes a
snapshot whose tasks are all done but whose next checkpoint does not exist
yet, and a concurrent reader must see that as work in progress.

1. ``error`` in the state, or a task that raised -> ``failed``.
2. ``completed`` written by a workflow without human decision -> ``completed``;
   ``approved`` written by the approval node -> ``approved`` / ``rejected``.
3. A pending ``interrupt()`` -> ``waiting_approval`` (a real pause).
4. Anything else -> ``running`` while a live lease says someone is driving
   the graph, else ``interrupted``: the process that was driving it went away.
   Such a run is recoverable from its last checkpoint.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Interrupt, StateSnapshot

from app.contracts.execution import RunError, RunResponse
from app.contracts.lifecycle import DecisionStage, RunLifecycleStatus
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


def has_pending_interrupt(snapshot: StateSnapshot) -> bool:
    return bool(snapshot.interrupts) or any(task.interrupts for task in snapshot.tasks)


_STAGE_BY_KIND: dict[str, DecisionStage] = {
    "approve_plan": DecisionStage.PLAN,
    "approve_scope": DecisionStage.SCOPE,
    "approve_publish": DecisionStage.PUBLISH,
    "approve_publication": DecisionStage.PUBLISH,
}


@dataclass(frozen=True)
class PendingDecision:
    """What the graph is waiting for, read from the interrupt payload (ADR 0009)."""

    stage: DecisionStage
    scope_digest: str | None
    deadline: datetime | None
    payload: dict[str, Any]
    publish_digest: str | None = None

    @property
    def digest(self) -> str | None:
        """The digest the decision must name: scope or publish, depending on the stage."""
        return self.publish_digest if self.stage is DecisionStage.PUBLISH else self.scope_digest


def _interrupts(snapshot: StateSnapshot) -> list[Interrupt]:
    found = list(snapshot.interrupts)
    for task in snapshot.tasks:
        found.extend(task.interrupts)
    return found


def pending_decision(snapshot: StateSnapshot) -> PendingDecision | None:
    for interrupt in _interrupts(snapshot):
        value = interrupt.value
        if not isinstance(value, dict):
            continue
        stage = _STAGE_BY_KIND.get(str(value.get("kind")))
        if stage is None:
            continue
        deadline = value.get("deadline")
        parsed: datetime | None = None
        if isinstance(deadline, str):
            try:
                parsed = datetime.fromisoformat(deadline)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
            except ValueError:
                parsed = None
        digest = value.get("scope_digest")
        publish = value.get("publish_digest")
        return PendingDecision(
            stage=stage,
            scope_digest=str(digest) if isinstance(digest, str) else None,
            deadline=parsed,
            payload=dict(value),
            publish_digest=str(publish) if isinstance(publish, str) else None,
        )
    return None


def publication_pending(values: dict[str, Any]) -> bool:
    """Positive evidence that a publication is under way (ADR 0011): a plan exists
    and no decision was taken yet, or the decision was taken and nothing final
    (published, rejected, failed) was written. Such a run is never ``completed``."""
    if "publication_plan" not in values or not values.get("publication_plan"):
        return False
    decided = values.get("publish_approved")
    if decided is None:
        return True
    return decided is True and values.get("published") is not True


def derive_status(snapshot: StateSnapshot, *, in_flight: bool = False) -> RunLifecycleStatus:
    values = snapshot.values
    if derive_error(snapshot) is not None:
        return RunLifecycleStatus.FAILED
    if values.get("published") is True:
        return RunLifecycleStatus.PUBLISHED
    if values.get("publish_approved") is False or values.get("plan_approved") is False:
        return RunLifecycleStatus.REJECTED
    if has_pending_interrupt(snapshot):
        return RunLifecycleStatus.WAITING_APPROVAL
    if values.get("publication_interrupted") is True or publication_pending(values):
        return RunLifecycleStatus.RUNNING if in_flight else RunLifecycleStatus.INTERRUPTED
    if values.get("completed") is True:
        return RunLifecycleStatus.COMPLETED
    approved = values.get("approved")
    if approved is True:
        return RunLifecycleStatus.APPROVED
    if approved is False:
        return RunLifecycleStatus.REJECTED
    if has_pending_interrupt(snapshot):
        return RunLifecycleStatus.WAITING_APPROVAL
    return RunLifecycleStatus.RUNNING if in_flight else RunLifecycleStatus.INTERRUPTED


def checkpoint_id_of(snapshot: StateSnapshot) -> str | None:
    configurable = snapshot.config.get("configurable", {}) if snapshot.config else {}
    value = configurable.get("checkpoint_id")
    return str(value) if value is not None else None


def run_response_from_snapshot(
    run_id: str,
    snapshot: StateSnapshot,
    *,
    in_flight: bool = False,
    owner: str = "unknown",
    project_id: str = "unknown",
) -> RunResponse:
    """Diagnostic view built from the checkpoint alone (ownership is not in it)."""
    values = snapshot.values
    plan = values.get("plan")
    architecture = values.get("architecture")
    return RunResponse(
        run_id=run_id,
        status=derive_status(snapshot, in_flight=in_flight),
        owner=owner,
        project_id=project_id,
        revision=values.get("revision", 0),
        llm_calls=values.get("llm_calls", 0),
        architect_approved=values.get("architect_approved"),
        plan=DevelopmentPlan.model_validate(plan) if plan else None,
        architecture=ArchitectureReview.model_validate(architecture) if architecture else None,
        error=derive_error(snapshot),
    )

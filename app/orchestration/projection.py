"""Checkpoint -> business projection, and the rules that keep both consistent.

Writing a checkpoint (LangGraph) and updating ``runs`` (us) are two separate
transactions. The projection is therefore *derived* from the checkpoint and
re-derived whenever a reader notices the stored ``last_checkpoint_id`` is
not the latest one. Terminal rows are never touched; a recorded decision
(``resume_pending``) is never overwritten by what the checkpoint still says
(``waiting_approval``).
"""

from langgraph.types import StateSnapshot

from app.contracts.lifecycle import TERMINAL_STATUSES, DecisionStage, RunLifecycleStatus
from app.orchestration.ports import PersistedRun, RunProjection
from app.orchestration.status import (
    checkpoint_id_of,
    derive_error,
    derive_status,
    pending_decision,
)

_S = RunLifecycleStatus
_WAITING = frozenset({_S.WAITING_APPROVAL, _S.RESUME_PENDING})


def resolve_status(
    current: RunLifecycleStatus,
    derived: RunLifecycleStatus,
    *,
    positive_interrupt: bool = False,
    next_stage: bool = False,
) -> RunLifecycleStatus:
    """``positive_interrupt``: the checkpoint itself says the work stopped (a
    publication that failed transiently, ADR 0011), so even a recorded decision
    may move to ``interrupted`` and wait for a retry. ``next_stage``: the graph
    paused again at a *different* stage than the one just decided (scope ->
    publish), which is forward progress, not a regression."""
    if current in TERMINAL_STATUSES:
        return current
    if current is _S.RESUME_PENDING and derived is _S.INTERRUPTED and positive_interrupt:
        return derived
    if current is _S.RESUME_PENDING and derived is _S.WAITING_APPROVAL and next_stage:
        return derived
    if current is _S.RESUME_PENDING and derived in (
        _S.WAITING_APPROVAL,
        _S.INTERRUPTED,
        _S.RUNNING,
    ):
        return current
    return derived


def build_projection(
    snapshot: StateSnapshot,
    *,
    current: RunLifecycleStatus,
    in_flight: bool,
    current_stage: DecisionStage | None = None,
) -> RunProjection:
    values = snapshot.values
    derived = derive_status(snapshot, in_flight=in_flight)
    pending = pending_decision(snapshot) if derived in _WAITING else None
    status = resolve_status(
        current,
        derived,
        positive_interrupt=values.get("publication_interrupted") is True,
        next_stage=(
            pending is not None and current_stage is not None and pending.stage is not current_stage
        ),
    )
    if status not in _WAITING:
        pending = None
    return RunProjection(
        status=status,
        revision=values.get("revision", 0),
        llm_calls=values.get("llm_calls", 0),
        architect_approved=values.get("architect_approved"),
        plan=values.get("plan"),
        architecture=values.get("architecture"),
        error=derive_error(snapshot),
        checkpoint_id=checkpoint_id_of(snapshot),
        pending_stage=pending.stage if pending else None,
        approval_deadline=pending.deadline if pending else None,
    )


def projection_differs(row: PersistedRun, projection: RunProjection) -> bool:
    return (
        row.status != projection.status
        or row.last_checkpoint_id != projection.checkpoint_id
        or row.revision != projection.revision
        or row.llm_calls != projection.llm_calls
        or row.architect_approved != projection.architect_approved
        or row.plan != projection.plan
        or row.architecture != projection.architecture
        or row.error != projection.error
        or row.pending_stage != projection.pending_stage
        or row.approval_deadline != projection.approval_deadline
    )

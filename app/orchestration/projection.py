"""Checkpoint -> business projection, and the rules that keep both consistent.

Writing a checkpoint (LangGraph) and updating ``runs`` (us) are two separate
transactions. The projection is therefore *derived* from the checkpoint and
re-derived whenever a reader notices the stored ``last_checkpoint_id`` is
not the latest one. Terminal rows are never touched; a recorded decision
(``resume_pending``) is never overwritten by what the checkpoint still says
(``waiting_approval``).
"""

from langgraph.types import StateSnapshot

from app.contracts.lifecycle import TERMINAL_STATUSES, RunLifecycleStatus
from app.orchestration.ports import PersistedRun, RunProjection
from app.orchestration.status import (
    checkpoint_id_of,
    derive_error,
    derive_status,
    pending_decision,
)

_S = RunLifecycleStatus
_WAITING = frozenset({_S.WAITING_APPROVAL, _S.RESUME_PENDING})


def resolve_status(current: RunLifecycleStatus, derived: RunLifecycleStatus) -> RunLifecycleStatus:
    if current in TERMINAL_STATUSES:
        return current
    if current is _S.RESUME_PENDING and derived in (
        _S.WAITING_APPROVAL,
        _S.INTERRUPTED,
        _S.RUNNING,
    ):
        return current
    return derived


def build_projection(
    snapshot: StateSnapshot, *, current: RunLifecycleStatus, in_flight: bool
) -> RunProjection:
    values = snapshot.values
    status = resolve_status(current, derive_status(snapshot, in_flight=in_flight))
    pending = pending_decision(snapshot) if status in _WAITING else None
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

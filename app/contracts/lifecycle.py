"""Run lifecycle vocabulary: the single definition shared by the API contract,
the orchestration layer and the persistence tables.

``RunLifecycleStatus`` is both the value stored in ``runs.status`` and the
``status`` field of ``RunResponse``. ``ALLOWED_TRANSITIONS`` is the only
place that says which status changes are legal; the repositories enforce it
in their conditional writes so a stale or late projection can never move a
run backwards or out of a terminal state.
"""

from enum import StrEnum


class RunLifecycleStatus(StrEnum):
    RUNNING = "running"  # a process is driving the graph right now
    WAITING_APPROVAL = "waiting_approval"  # graph paused at the approval interrupt
    RESUME_PENDING = "resume_pending"  # human decision recorded, not yet confirmed applied
    INTERRUPTED = "interrupted"  # pending work, no interrupt, nobody driving it
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


TERMINAL_STATUSES: frozenset[RunLifecycleStatus] = frozenset(
    {
        RunLifecycleStatus.APPROVED,
        RunLifecycleStatus.REJECTED,
        RunLifecycleStatus.FAILED,
    }
)

_S = RunLifecycleStatus

# from -> allowed targets (re-projecting the same status is always allowed).
ALLOWED_TRANSITIONS: dict[RunLifecycleStatus, frozenset[RunLifecycleStatus]] = {
    _S.RUNNING: frozenset(
        {_S.WAITING_APPROVAL, _S.INTERRUPTED, _S.APPROVED, _S.REJECTED, _S.FAILED}
    ),
    _S.WAITING_APPROVAL: frozenset({_S.RESUME_PENDING, _S.FAILED}),
    # resume_pending never goes back to waiting_approval: the decision is already recorded.
    _S.RESUME_PENDING: frozenset({_S.APPROVED, _S.REJECTED, _S.FAILED}),
    _S.INTERRUPTED: frozenset(
        {_S.RUNNING, _S.WAITING_APPROVAL, _S.APPROVED, _S.REJECTED, _S.FAILED}
    ),
    _S.APPROVED: frozenset(),
    _S.REJECTED: frozenset(),
    _S.FAILED: frozenset(),
}


def can_transition(current: RunLifecycleStatus, new: RunLifecycleStatus) -> bool:
    return current == new or new in ALLOWED_TRANSITIONS[current]


def sources_for(new: RunLifecycleStatus) -> frozenset[RunLifecycleStatus]:
    """Statuses from which ``new`` may be written (used in conditional UPDATEs)."""
    return frozenset(status for status in RunLifecycleStatus if can_transition(status, new))


class RunEventKind(StrEnum):
    RUN_CREATED = "run_created"
    PLANNER_COMPLETED = "planner_completed"
    ARCHITECT_REVIEWED = "architect_reviewed"
    REVISION_REQUESTED = "revision_requested"
    WAITING_APPROVAL = "waiting_approval"
    APPROVAL_RECORDED = "approval_recorded"
    APPROVAL_APPLIED = "approval_applied"
    RUN_APPROVED = "run_approved"
    RUN_REJECTED = "run_rejected"
    RUN_FAILED = "run_failed"
    RUN_INTERRUPTED = "run_interrupted"
    RUN_RESUMED = "run_resumed"

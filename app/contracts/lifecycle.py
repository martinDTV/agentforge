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
    # A workflow that needs no human decision (development, Fase 6) finished
    # with its own success criterion met; ``approved`` stays a human verdict.
    COMPLETED = "completed"
    # A human withdrew a run that was waiting (or interrupted); nothing ran after it.
    CANCELLED = "cancelled"
    # Nobody decided within the window the pending stage allowed (Fase 7).
    EXPIRED = "expired"
    # A human approved publication and the draft pull request exists (Fase 7, Paso 3).
    PUBLISHED = "published"
    FAILED = "failed"


TERMINAL_STATUSES: frozenset[RunLifecycleStatus] = frozenset(
    {
        RunLifecycleStatus.APPROVED,
        RunLifecycleStatus.REJECTED,
        RunLifecycleStatus.COMPLETED,
        RunLifecycleStatus.CANCELLED,
        RunLifecycleStatus.EXPIRED,
        RunLifecycleStatus.PUBLISHED,
        RunLifecycleStatus.FAILED,
    }
)


class DecisionStage(StrEnum):
    """Which human decision a run is waiting for (ADR 0009).

    ``plan`` is the Fase 1 approval of the planning workflow; ``scope`` authorises
    the exact files Developer may write; ``publish`` authorises leaving the host
    (a branch and a draft pull request, ADR 0011).
    """

    PLAN = "plan"
    SCOPE = "scope"
    PUBLISH = "publish"


class WorkflowKind(StrEnum):
    """Which graph drives a run; stored with the row so recovery never guesses."""

    PLANNING = "planning"  # Planner -> Architect -> human approval
    DEVELOPMENT = "development"  # Developer -> QA, bounded corrections, no approval


_S = RunLifecycleStatus

# from -> allowed targets (re-projecting the same status is always allowed).
ALLOWED_TRANSITIONS: dict[RunLifecycleStatus, frozenset[RunLifecycleStatus]] = {
    _S.RUNNING: frozenset(
        {
            _S.WAITING_APPROVAL,
            _S.INTERRUPTED,
            _S.APPROVED,
            _S.REJECTED,
            _S.COMPLETED,
            _S.PUBLISHED,
            _S.FAILED,
        }
    ),
    _S.WAITING_APPROVAL: frozenset({_S.RESUME_PENDING, _S.FAILED, _S.CANCELLED, _S.EXPIRED}),
    # resume_pending never goes back to the *same* wait (the projection pins it);
    # it may advance to the next stage's wait (scope -> publish, ADR 0011) or
    # to interrupted when the checkpoint positively says the work stopped.
    _S.RESUME_PENDING: frozenset(
        {
            _S.WAITING_APPROVAL,
            _S.APPROVED,
            _S.REJECTED,
            _S.COMPLETED,
            _S.PUBLISHED,
            _S.FAILED,
            _S.INTERRUPTED,
        }
    ),
    _S.INTERRUPTED: frozenset(
        {
            _S.RUNNING,
            _S.WAITING_APPROVAL,
            _S.APPROVED,
            _S.REJECTED,
            _S.COMPLETED,
            _S.PUBLISHED,
            _S.CANCELLED,
            _S.FAILED,
        }
    ),
    _S.APPROVED: frozenset(),
    _S.REJECTED: frozenset(),
    _S.COMPLETED: frozenset(),
    _S.CANCELLED: frozenset(),
    _S.EXPIRED: frozenset(),
    _S.PUBLISHED: frozenset(),
    _S.FAILED: frozenset(),
}


def can_transition(current: RunLifecycleStatus, new: RunLifecycleStatus) -> bool:
    return current == new or new in ALLOWED_TRANSITIONS[current]


def sources_for(new: RunLifecycleStatus) -> frozenset[RunLifecycleStatus]:
    """Statuses from which ``new`` may be written (used in conditional UPDATEs)."""
    return frozenset(status for status in RunLifecycleStatus if can_transition(status, new))


class RunEventKind(StrEnum):
    DEVELOPER_COMPLETED = "developer_completed"
    DEVELOPER_APPLIED = "developer_applied"
    QA_REVIEWED = "qa_reviewed"
    RUN_CREATED = "run_created"
    PLANNER_COMPLETED = "planner_completed"
    ARCHITECT_REVIEWED = "architect_reviewed"
    REVISION_REQUESTED = "revision_requested"
    WAITING_APPROVAL = "waiting_approval"
    APPROVAL_RECORDED = "approval_recorded"
    APPROVAL_APPLIED = "approval_applied"
    RUN_APPROVED = "run_approved"
    RUN_REJECTED = "run_rejected"
    RUN_COMPLETED = "run_completed"
    RUN_CANCELLED = "run_cancelled"
    RUN_EXPIRED = "run_expired"
    SCOPE_PROPOSED = "scope_proposed"
    MIRROR_REFRESHED = "mirror_refreshed"
    # Publication (Fase 7, Paso 3; ADR 0011)
    PUBLICATION_PREPARED = "publication_prepared"
    BRANCH_PUSHED = "branch_pushed"
    PULL_REQUEST_OPENED = "pull_request_opened"
    PUBLICATION_FAILED = "publication_failed"
    RUN_PUBLISHED = "run_published"
    RUN_FAILED = "run_failed"
    RUN_INTERRUPTED = "run_interrupted"
    RUN_RESUMED = "run_resumed"

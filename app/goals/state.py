"""State machines of goals and goal tasks: the only place that says which moves are legal.

Repositories apply these tables in their conditional writes, like
``ALLOWED_TRANSITIONS`` does for runs, so a late or duplicated reconciliation
can never move a goal or a task backwards or out of a terminal state.
"""

from app.contracts.goals import (
    GOAL_TERMINAL_STATUSES,
    TERMINAL_TASK_STATUSES,
    GoalStatus,
    GoalTaskStatus,
)

_G = GoalStatus
_T = GoalTaskStatus

GOAL_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    _G.PLANNING: frozenset({_G.ACTIVE, _G.BLOCKED, _G.FAILED, _G.CANCELLED, _G.EXPIRED}),
    _G.ACTIVE: frozenset(
        {_G.BLOCKED, _G.COMPLETED, _G.PARTIAL, _G.FAILED, _G.CANCELLED, _G.EXPIRED}
    ),
    # ``blocked`` is reversible: the human decision or the merge arrives and work resumes.
    _G.BLOCKED: frozenset(
        {_G.ACTIVE, _G.PLANNING, _G.COMPLETED, _G.PARTIAL, _G.FAILED, _G.CANCELLED, _G.EXPIRED}
    ),
    _G.COMPLETED: frozenset(),
    _G.PARTIAL: frozenset(),
    _G.FAILED: frozenset(),
    _G.CANCELLED: frozenset(),
    _G.EXPIRED: frozenset(),
}

TASK_TRANSITIONS: dict[GoalTaskStatus, frozenset[GoalTaskStatus]] = {
    _T.PENDING: frozenset({_T.READY, _T.SKIPPED, _T.DUPLICATE, _T.FAILED}),
    _T.READY: frozenset({_T.CREATING, _T.SKIPPED, _T.DUPLICATE, _T.FAILED}),
    # A reserved run id may be given back (service shutting down) and taken again later.
    _T.CREATING: frozenset({_T.READY, _T.RUNNING, _T.WAITING_HUMAN, _T.PUBLISHED, _T.FAILED}),
    _T.RUNNING: frozenset({_T.WAITING_HUMAN, _T.PUBLISHED, _T.FAILED}),
    # A stale base cancels the child before any human decides: the task is recreated.
    _T.WAITING_HUMAN: frozenset({_T.RUNNING, _T.PUBLISHED, _T.FAILED, _T.READY}),
    _T.PUBLISHED: frozenset({_T.MERGED, _T.FAILED}),
    _T.MERGED: frozenset(),
    _T.FAILED: frozenset(),
    _T.SKIPPED: frozenset(),
    _T.DUPLICATE: frozenset(),
}


def can_transition_goal(current: GoalStatus, new: GoalStatus) -> bool:
    if current == new:
        return True
    if current in GOAL_TERMINAL_STATUSES:
        return False
    return new in GOAL_TRANSITIONS[current]


def can_transition_task(current: GoalTaskStatus, new: GoalTaskStatus) -> bool:
    if current == new:
        return True
    if current in TERMINAL_TASK_STATUSES:
        return False
    return new in TASK_TRANSITIONS[current]

"""Deterministic rules of the Supervisor (``supervisor-v1``).

Every input is a durable fact (a ``runs`` row, a timestamp, a policy value);
same inputs, same outputs. No model is consulted anywhere in this module.
"""

from datetime import datetime, timedelta
from enum import StrEnum

from app.contracts.goals import GoalTaskStatus
from app.contracts.lifecycle import DecisionStage, RunLifecycleStatus

POLICY_VERSION = "supervisor-v1"
# Audit identity of the coordinator. Platform work, never an agent (like the Publisher).
SUPERVISOR_ACTOR = "agentforge:supervisor"

_S = RunLifecycleStatus
_T = GoalTaskStatus


class NeedsHumanReason(StrEnum):
    PLAN_PENDING = "plan_pending"
    DECISION_PENDING = "decision_pending"
    MERGE_PENDING = "merge_pending"
    MERGE_UNVERIFIED = "merge_unverified"
    TASK_FAILED = "task_failed"
    PR_CLOSED = "pr_closed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PLAN_REFUSED = "plan_refused"


_REASON_INDEX: dict[NeedsHumanReason, int] = {
    reason: index for index, reason in enumerate(NeedsHumanReason)
}
REASONS_PER_TASK = 8
assert len(_REASON_INDEX) <= REASONS_PER_TASK


STAGES_PER_REASON = 4


def needs_human_ordinal(task_ordinal: int, reason: NeedsHumanReason, *, stage: int = 0) -> int:
    """``goal_events`` is unique per ``(goal, kind, ordinal)``: one ``needs_human`` per task,
    reason and decision stage. The goal itself (plan pending, plan refused) uses task
    ordinal ``-1``. ``stage`` is ``DecisionStage.event_ordinal`` (0..2) or 0."""
    if not 0 <= stage < STAGES_PER_REASON:
        raise ValueError("stage fuera de rango")
    slot = (task_ordinal + 1) * REASONS_PER_TASK + _REASON_INDEX[reason]
    return slot * STAGES_PER_REASON + stage


def stage_ordinal(stage: DecisionStage | None) -> int:
    return stage.event_ordinal if stage is not None else 0


def task_status_for_run(status: RunLifecycleStatus) -> GoalTaskStatus:
    """Projection of a child's lifecycle status onto its task (ADR 0012 §2.2).

    ``interrupted`` stays ``running``: recovery decides what becomes of it, the
    goal only waits. ``approved`` (a planning run) is mapped by the coordinator
    itself, never by this table.
    """
    if status is _S.WAITING_APPROVAL:
        return _T.WAITING_HUMAN
    if status in (_S.PUBLISHED, _S.COMPLETED, _S.APPROVED):
        return _T.PUBLISHED
    if status in (_S.FAILED, _S.REJECTED, _S.EXPIRED, _S.CANCELLED):
        return _T.FAILED
    return _T.RUNNING


def failure_code_for_run(
    status: RunLifecycleStatus,
    *,
    pending_stage: DecisionStage | None,
    error_code: str | None,
) -> str:
    """Stable code of why a child is a failure for its goal."""
    if status is _S.FAILED:
        return error_code or "failed"
    stage = pending_stage.value if pending_stage is not None else "plan"
    if status is _S.REJECTED:
        return f"rejected:{stage}"
    if status is _S.EXPIRED:
        return f"expired:{stage}"
    if status is _S.CANCELLED:
        return "cancelled"
    return status.value


def reminder_due(
    *,
    since: datetime,
    now: datetime,
    reminder_seconds: float,
    deadline: datetime | None = None,
) -> bool:
    """A human has had the decision for too long: at ``since + reminder`` or, when the
    decision has a deadline, at half of the window, whichever comes first."""
    at = since + timedelta(seconds=reminder_seconds)
    if deadline is not None and deadline > since:
        at = min(at, since + (deadline - since) / 2)
    return now >= at

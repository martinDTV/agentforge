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
    # H2: a last notice shortly before a decision window closes. Appended at the end so
    # the ordinal slots of the reasons above stay where goals created earlier put them.
    PLAN_FINAL_NOTICE = "plan_final_notice"
    DECISION_FINAL_NOTICE = "decision_final_notice"
    # P2: a child ended without a Pull Request where one was required, or stopped because
    # publication is disabled in this instance. Neither is ever read as a merge.
    PUBLICATION_MISSING = "publication_missing"
    PUBLICATION_UNAVAILABLE = "publication_unavailable"


# A final notice blocks the goal under the reason it reminds about, not under its own name.
BLOCKING_REASON: dict[NeedsHumanReason, NeedsHumanReason] = {
    NeedsHumanReason.PLAN_FINAL_NOTICE: NeedsHumanReason.PLAN_PENDING,
    NeedsHumanReason.DECISION_FINAL_NOTICE: NeedsHumanReason.DECISION_PENDING,
}

_REASON_INDEX: dict[NeedsHumanReason, int] = {
    reason: index for index, reason in enumerate(NeedsHumanReason)
}
# Slots per task in the ``needs_human`` ordinal space. Raised from 8 to 16 with the final
# notices (2026-10-31): ordinals of goals that were active across that change may differ
# from the ones they would get today, which can only produce one extra reminder, never a
# collision between different reasons.
REASONS_PER_TASK = 16
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


def task_status_for_run(status: RunLifecycleStatus, *, remote: bool = False) -> GoalTaskStatus:
    """Projection of a child's lifecycle status onto its task (ADR 0012 §2.2).

    ``interrupted`` stays ``running``: recovery decides what becomes of it, the
    goal only waits. ``approved`` (a planning run) is mapped by the coordinator
    itself, never by this table. ``completed`` counts as published only for a
    repository without remote (P2): with a remote, the task's deliverable is a
    Pull Request, so a run that ended without one did not deliver.
    """
    if status is _S.WAITING_APPROVAL:
        return _T.WAITING_HUMAN
    if status is _S.COMPLETED:
        return _T.FAILED if remote else _T.PUBLISHED
    if status in (_S.PUBLISHED, _S.APPROVED):
        return _T.PUBLISHED
    if status in (_S.FAILED, _S.REJECTED, _S.EXPIRED, _S.CANCELLED):
        return _T.FAILED
    return _T.RUNNING


def failure_code_for_run(
    status: RunLifecycleStatus,
    *,
    pending_stage: DecisionStage | None,
    error_code: str | None,
    decided_stage: DecisionStage | None = None,
) -> str:
    """Stable code of why a child is a failure for its goal.

    The stage of a refused or expired decision comes, in order, from the run's own
    error code (the sweep writes ``expired:<stage>`` before clearing ``pending_stage``),
    from the stage still pending on the row, or from the stage of the decision a human
    recorded (``decided_stage``). ``plan`` is only the last resort.
    """
    if status is _S.FAILED:
        return error_code or "failed"
    if status is _S.EXPIRED and error_code and error_code.startswith("expired:"):
        return error_code
    known = pending_stage or decided_stage
    stage = known.value if known is not None else "plan"
    if status is _S.REJECTED:
        return f"rejected:{stage}"
    if status is _S.EXPIRED:
        return f"expired:{stage}"
    if status is _S.CANCELLED:
        return "cancelled"
    return status.value


def reminder_at(
    *, since: datetime, reminder_seconds: float, deadline: datetime | None = None
) -> datetime:
    """When the midway reminder is due: ``since + reminder`` or, when the decision has a
    deadline, half of the window, whichever comes first."""
    at = since + timedelta(seconds=reminder_seconds)
    if deadline is not None and deadline > since:
        at = min(at, since + (deadline - since) / 2)
    return at


def reminder_due(
    *,
    since: datetime,
    now: datetime,
    reminder_seconds: float,
    deadline: datetime | None = None,
) -> bool:
    """A human has had the decision for too long (see ``reminder_at``)."""
    return now >= reminder_at(since=since, reminder_seconds=reminder_seconds, deadline=deadline)


def final_notice_at(
    *,
    since: datetime,
    reminder_seconds: float,
    final_notice_seconds: float,
    deadline: datetime | None,
) -> datetime | None:
    """When the last notice before the window closes is due, or ``None`` when the window
    has no deadline or is too short for a second, distinct notice (the final notice must
    fall strictly after the midway reminder)."""
    if deadline is None or deadline <= since:
        return None
    at = deadline - timedelta(seconds=final_notice_seconds)
    midway = reminder_at(since=since, reminder_seconds=reminder_seconds, deadline=deadline)
    return at if at > midway else None


def reminders_due(
    *,
    since: datetime,
    now: datetime,
    reminder_seconds: float,
    final_notice_seconds: float,
    deadline: datetime | None = None,
) -> tuple[bool, bool]:
    """``(midway_due, final_due)`` for a pending human decision (H2).

    Both are pure functions of durable facts, so every tick and every process after a
    restart compute the same answer; the ``needs_human`` ordinal space keeps each notice
    unique per task, reason and stage, which is what makes them idempotent.
    """
    midway = now >= reminder_at(since=since, reminder_seconds=reminder_seconds, deadline=deadline)
    final_at = final_notice_at(
        since=since,
        reminder_seconds=reminder_seconds,
        final_notice_seconds=final_notice_seconds,
        deadline=deadline,
    )
    return midway, final_at is not None and now >= final_at

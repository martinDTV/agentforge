"""Audit events derived from LangGraph ``stream_mode="updates"`` items.

Each update is ``{node_name: state_delta}`` (or ``{"__interrupt__": (...)}``).
The agents never know about auditing; this module turns their outputs into
structured, bounded events. Payloads hold counts and flags only.
"""

from collections.abc import Sequence
from typing import Any

from langgraph.types import Interrupt, StateSnapshot

from app.contracts.lifecycle import RunEventKind, RunLifecycleStatus
from app.orchestration.ports import PersistedRun, RunEvent
from app.orchestration.status import checkpoint_id_of, has_pending_interrupt

INTERRUPT_KEY = "__interrupt__"


def revision_from_update(update: dict[str, Any], current: int) -> int:
    planner = update.get("planner")
    if isinstance(planner, dict) and isinstance(planner.get("revision"), int):
        return int(planner["revision"])
    return current


def events_from_update(
    update: dict[str, Any], *, revision: int, checkpoint_id: str | None = None
) -> list[RunEvent]:
    events: list[RunEvent] = []
    for node, delta in update.items():
        if node == INTERRUPT_KEY:
            ordinal = _interrupt_revision(delta, revision)
            events.append(RunEvent(kind=RunEventKind.WAITING_APPROVAL, ordinal=ordinal))
            continue
        if not isinstance(delta, dict):
            continue
        if "error" in delta:
            error = delta["error"] or {}
            events.append(
                RunEvent(
                    kind=RunEventKind.RUN_FAILED,
                    payload={"node": node, "code": error.get("code", "unknown")},
                )
            )
            continue
        if node == "planner":
            plan = delta.get("plan") or {}
            if revision > 0:
                events.append(
                    RunEvent(
                        kind=RunEventKind.REVISION_REQUESTED,
                        ordinal=revision,
                        payload={"revision": revision},
                    )
                )
            events.append(
                RunEvent(
                    kind=RunEventKind.PLANNER_COMPLETED,
                    ordinal=revision,
                    payload={
                        "revision": revision,
                        "tasks": len(plan.get("tasks") or []),
                        "risks": len(plan.get("risks") or []),
                    },
                )
            )
        elif node == "architect":
            review = delta.get("architecture") or {}
            events.append(
                RunEvent(
                    kind=RunEventKind.ARCHITECT_REVIEWED,
                    ordinal=revision,
                    payload={
                        "revision": revision,
                        "approved": bool(delta.get("architect_approved")),
                        "issues": len(review.get("issues") or []),
                        "decisions": len(review.get("decisions") or []),
                    },
                )
            )
        elif node == "approval" and "approved" in delta:
            kind = RunEventKind.RUN_APPROVED if delta["approved"] else RunEventKind.RUN_REJECTED
            events.append(RunEvent(kind=kind))
    for event in events:
        object.__setattr__(event, "checkpoint_id", checkpoint_id)
    return events


def transition_events(previous: RunLifecycleStatus, updated: PersistedRun) -> list[RunEvent]:
    """Events implied by a status change the stream could not report itself."""
    if previous == updated.status:
        return []
    status = updated.status
    if status is RunLifecycleStatus.INTERRUPTED:
        return [RunEvent(kind=RunEventKind.RUN_INTERRUPTED, ordinal=updated.attempt)]
    if status is RunLifecycleStatus.FAILED:
        code = updated.error.code if updated.error else "unknown"
        return [RunEvent(kind=RunEventKind.RUN_FAILED, payload={"code": code})]
    if status is RunLifecycleStatus.APPROVED:
        return [RunEvent(kind=RunEventKind.RUN_APPROVED)]
    if status is RunLifecycleStatus.REJECTED:
        return [RunEvent(kind=RunEventKind.RUN_REJECTED)]
    return []


def _interrupt_revision(delta: Any, fallback: int) -> int:
    for item in delta if isinstance(delta, tuple | list) else (delta,):
        if isinstance(item, Interrupt) and isinstance(item.value, dict):
            value = item.value.get("revision")
            if isinstance(value, int):
                return value
    return fallback


def events_from_history(history: Sequence[StateSnapshot]) -> list[RunEvent]:
    """Rebuild node events from persisted checkpoints, oldest first.

    Used by recovery when the owner died between writing a checkpoint and
    writing its audit. Every event here is backed by a stored checkpoint whose
    values changed in the corresponding way; nothing is inferred beyond that.
    Duplicates are harmless: the natural key dedups on insert.
    """
    events: list[RunEvent] = []
    previous: dict[str, Any] = {}
    previously_interrupted = False
    for snapshot in history:
        values = snapshot.values
        revision = int(values.get("revision", 0) or 0)
        checkpoint_id = checkpoint_id_of(snapshot)
        plan = values.get("plan")
        if plan and plan != previous.get("plan"):
            if revision > 0:
                events.append(
                    RunEvent(
                        kind=RunEventKind.REVISION_REQUESTED,
                        ordinal=revision,
                        payload={"revision": revision},
                        checkpoint_id=checkpoint_id,
                    )
                )
            events.append(
                RunEvent(
                    kind=RunEventKind.PLANNER_COMPLETED,
                    ordinal=revision,
                    payload={
                        "revision": revision,
                        "tasks": len(plan.get("tasks") or []),
                        "risks": len(plan.get("risks") or []),
                    },
                    checkpoint_id=checkpoint_id,
                )
            )
        review = values.get("architecture")
        if review and review != previous.get("architecture"):
            events.append(
                RunEvent(
                    kind=RunEventKind.ARCHITECT_REVIEWED,
                    ordinal=revision,
                    payload={
                        "revision": revision,
                        "approved": bool(values.get("architect_approved")),
                        "issues": len(review.get("issues") or []),
                        "decisions": len(review.get("decisions") or []),
                    },
                    checkpoint_id=checkpoint_id,
                )
            )
        if values.get("error") and not previous.get("error"):
            events.append(
                RunEvent(
                    kind=RunEventKind.RUN_FAILED,
                    payload={"code": values["error"].get("code", "unknown")},
                    checkpoint_id=checkpoint_id,
                )
            )
        interrupted = has_pending_interrupt(snapshot)
        if interrupted and not previously_interrupted:
            events.append(
                RunEvent(
                    kind=RunEventKind.WAITING_APPROVAL,
                    ordinal=revision,
                    checkpoint_id=checkpoint_id,
                )
            )
        if "approved" in values and "approved" not in previous:
            kind = RunEventKind.RUN_APPROVED if values["approved"] else RunEventKind.RUN_REJECTED
            events.append(RunEvent(kind=kind, checkpoint_id=checkpoint_id))
        previous = dict(values)
        previously_interrupted = interrupted
    return events

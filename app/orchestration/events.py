"""Audit events derived from LangGraph ``stream_mode="updates"`` items.

Each update is ``{node_name: state_delta}`` (or ``{"__interrupt__": (...)}``).
The agents never know about auditing; this module turns their outputs into
structured, bounded events. Payloads hold counts and flags only.
"""

from collections.abc import Sequence
from typing import Any

from langgraph.types import Interrupt, StateSnapshot

from app.contracts.lifecycle import DecisionStage, RunEventKind, RunLifecycleStatus
from app.orchestration.ports import PersistedRun, RunEvent
from app.orchestration.status import checkpoint_id_of, has_pending_interrupt

INTERRUPT_KEY = "__interrupt__"
# Bounds of the ephemeral notification context (never persisted; see RunEvent.context).
_MAX_LIST = 16
_MAX_TEXT = 400


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    return " ".join(str(value).split())[:limit] if value is not None else ""


def _interrupt_context(delta: Any) -> dict[str, Any]:
    """What a mail may say about a pending decision: a bounded view of the proposal the
    human will see anyway, cut by the platform (counts stay in the payload)."""
    for item in delta if isinstance(delta, tuple | list) else (delta,):
        if not (isinstance(item, Interrupt) and isinstance(item.value, dict)):
            continue
        value = item.value
        stage = str(value.get("stage") or "plan")
        context: dict[str, Any] = {"stage": stage}
        if stage == "plan":
            plan = value.get("plan") or {}
            review = value.get("architecture") or {}
            context.update(
                plan_summary=_text(plan.get("summary")),
                tasks=[
                    {
                        "id": _text(t.get("id"), 32),
                        "title": _text(t.get("title"), 160),
                        "dependencies": [_text(d, 32) for d in (t.get("dependencies") or [])][:8],
                    }
                    for t in (plan.get("tasks") or [])[:8]
                    if isinstance(t, dict)
                ],
                risks=[_text(r, 200) for r in (plan.get("risks") or [])[:5]],
                architect_approved=value.get("architect_approved"),
                architecture_summary=_text(review.get("summary")),
                architecture_issues=[_text(i, 200) for i in (review.get("issues") or [])[:5]],
                revision=value.get("revision", 0),
                route=value.get("route"),
            )
        elif stage == "scope":
            base = value.get("base") or {}
            context.update(
                summary=_text(value.get("summary")),
                files=[
                    {"action": _text(f.get("action"), 8), "path": _text(f.get("path"), 200)}
                    for f in (value.get("files") or [])[:_MAX_LIST]
                    if isinstance(f, dict)
                ],
                tests=[_text(t, 200) for t in (value.get("tests") or [])[:8]],
                risks=[_text(r, 200) for r in (value.get("risks") or [])[:8]],
                rejected=len(value.get("rejected") or []),
                hidden_paths=value.get("hidden_paths", 0),
                signals=len(value.get("signals") or []),
                repository=_text(base.get("repository"), 120) if isinstance(base, dict) else "",
                base_sha=_text(base.get("sha"), 40) if isinstance(base, dict) else "",
                limits=value.get("limits") if isinstance(value.get("limits"), dict) else {},
            )
        elif stage == "publish":
            scan = value.get("scan") or {}
            context.update(
                repository=_text(value.get("repository"), 120),
                remote_branch=_text(value.get("remote_branch"), 200),
                base_branch=_text(value.get("base_branch"), 200),
                base_sha=_text(value.get("base_sha"), 40),
                head_sha=_text(value.get("head_sha"), 40),
                title=_text(value.get("title"), 160),
                summary=_text(value.get("summary")),
                files=[
                    {"change": _text(f.get("change"), 12), "path": _text(f.get("path"), 200)}
                    for f in (value.get("files") or [])[:32]
                    if isinstance(f, dict)
                ],
                qa_checks=[
                    {"program": _text(c.get("program"), 32), "exit_code": c.get("exit_code")}
                    for c in (value.get("qa_checks") or [])[:8]
                    if isinstance(c, dict)
                ],
                scan_findings=len(scan.get("findings") or []) if isinstance(scan, dict) else 0,
            )
        return context
    return {}


# Which automation identity executes each node (ADR 0009, attribution). Node
# events are never attributed to a human: ``actor_subject`` stays NULL and the
# payload names the agent in ``executed_by``.
EXECUTED_BY: dict[str, str] = {
    "planner": "agentforge:planner",
    "architect": "agentforge:architect",
    "scoper": "agentforge:scoper",
    "developer": "agentforge:developer",
    "apply_changes": "agentforge:developer",
    "qa": "agentforge:qa",
    # Publication steps are platform work, never an agent's (ADR 0011).
    "prepare_publication": "agentforge:publisher",
    "publish": "agentforge:publisher",
    "router": "agentforge:platform",
    "plan_gate": "agentforge:platform",  # H1: deterministic, no model
}
# Events that a human decision causes directly: attributed to that human.
HUMAN_CAUSED: frozenset[RunEventKind] = frozenset(
    {RunEventKind.RUN_APPROVED, RunEventKind.RUN_REJECTED}
)


def revision_from_update(update: dict[str, Any], current: int) -> int:
    for name in ("developer", "revise_development"):
        value = update.get(name)
        if isinstance(value, dict) and isinstance(value.get("revision"), int):
            return int(value["revision"])
    planner = update.get("planner")
    if isinstance(planner, dict) and isinstance(planner.get("revision"), int):
        return int(planner["revision"])
    return current


def events_from_update(
    update: dict[str, Any],
    *,
    revision: int,
    checkpoint_id: str | None = None,
    approved_by: str | None = None,
) -> list[RunEvent]:
    """Events for one ``stream_mode="updates"`` item.

    ``approved_by`` is the human whose decision caused this drive (a resume
    after an approval); it lands in the payload of the terminal events of that
    drive, never in ``actor_subject`` of node events.
    """
    events: list[RunEvent] = []
    for node, delta in update.items():
        if node == INTERRUPT_KEY:
            ordinal = _interrupt_revision(delta, revision)
            events.append(
                RunEvent(
                    kind=RunEventKind.WAITING_APPROVAL,
                    ordinal=ordinal,
                    payload=_interrupt_payload(delta),
                    context=_interrupt_context(delta),
                )
            )
            continue
        if not isinstance(delta, dict):
            continue
        if node == "qa" and "qa_review" in delta:
            # The review happened even when it ends the run (correction limit).
            review = delta.get("qa_review") or {}
            validation = delta.get("validation", [])
            events.append(
                RunEvent(
                    kind=RunEventKind.QA_REVIEWED,
                    ordinal=revision,
                    payload={
                        "approved": bool(review.get("approved")),
                        "issues": len(review.get("issues", [])),
                        "checks": len(validation),
                        "failed_checks": sum(1 for r in validation if r.get("exit_code") != 0),
                        "usage": delta.get("usage", {}),
                    },
                    context={
                        "summary": _text(review.get("summary")),
                        "issues": [
                            {"code": _text(i.get("code"), 80), "path": _text(i.get("path"), 200)}
                            for i in (review.get("issues") or [])[:6]
                            if isinstance(i, dict)
                        ],
                        "checks": [
                            {
                                "program": _text(r.get("program"), 32),
                                "exit_code": r.get("exit_code"),
                            }
                            for r in validation[:8]
                            if isinstance(r, dict)
                        ],
                        "round": revision,
                    },
                )
            )
        if "error" in delta:
            error = delta["error"] or {}
            if node == "plan_gate" and delta.get("plan_violations"):
                # H1: the last, decisive rejection keeps its violation codes in the audit.
                events.append(
                    RunEvent(
                        kind=RunEventKind.PLAN_GATE_REJECTED,
                        ordinal=revision,
                        payload={
                            "revision": revision,
                            "violations": [str(v.get("code")) for v in delta["plan_violations"]][
                                :16
                            ],
                            "final": True,
                            "executed_by": EXECUTED_BY["plan_gate"],
                        },
                    )
                )
            failed: dict[str, Any] = {"node": node, "code": error.get("code", "unknown")}
            if node in EXECUTED_BY:
                failed["executed_by"] = EXECUTED_BY[node]
            if approved_by is not None:
                failed["approved_by"] = approved_by
            events.append(
                RunEvent(
                    kind=RunEventKind.RUN_FAILED,
                    payload=failed,
                    context={"message": _text(error.get("message"))},
                )
            )
            continue
        if node == "router" and delta.get("route_decision"):
            decision = delta["route_decision"]
            events.append(
                RunEvent(
                    kind=RunEventKind.ROUTE_SELECTED,
                    payload={
                        "route": decision.get("route"),
                        "mode": decision.get("mode"),
                        "reason": decision.get("reason"),
                        "policy_version": decision.get("policy_version"),
                        "confidence": decision.get("confidence"),
                        "classifier_used": decision.get("classifier_used"),
                        "executed_by": "agentforge:platform",
                    },
                )
            )
            continue
        if node == "scoper" and delta.get("route_escalation"):
            escalation = delta["route_escalation"]
            events.append(
                RunEvent(
                    kind=RunEventKind.ROUTE_ESCALATED,
                    payload={
                        "route": escalation.get("route"),
                        "escalated_from": escalation.get("escalated_from"),
                        "reason": escalation.get("reason"),
                        "executed_by": "agentforge:platform",
                    },
                )
            )
        if node == "scoper":
            proposal = delta.get("scope_proposal") or {}
            base = proposal.get("base") or {}
            if base.get("sha"):
                # The platform, not the Scoper, synchronised the mirror (ADR 0010).
                events.append(
                    RunEvent(
                        kind=RunEventKind.MIRROR_REFRESHED,
                        ordinal=revision,
                        payload={
                            "repository": base.get("repository"),
                            "branch": base.get("branch"),
                            "base_sha": base.get("sha"),
                            "changed": bool(base.get("changed")),
                            "executed_by": "agentforge:platform",
                        },
                    )
                )
            events.append(
                RunEvent(
                    kind=RunEventKind.SCOPE_PROPOSED,
                    ordinal=revision,
                    payload={
                        "files": len(proposal.get("files") or []),
                        "rejected": len(proposal.get("rejected") or []),
                        "tests": len(proposal.get("tests") or []),
                        "scope_digest": proposal.get("digest"),
                        "base_sha": (proposal.get("base") or {}).get("sha"),
                        "hidden_paths": proposal.get("hidden_paths", 0),
                        "signals": len(proposal.get("signals") or []),
                        "usage": delta.get("usage", {}),
                    },
                )
            )
        elif node == "developer":
            changes = delta.get("changes") or {}
            events.append(
                RunEvent(
                    kind=RunEventKind.DEVELOPER_COMPLETED,
                    ordinal=revision,
                    payload={
                        "files": len(changes.get("edits", [])),
                        "usage": delta.get("usage", {}),
                    },
                    context={
                        "summary": _text(changes.get("summary")),
                        "files": [
                            _text(e.get("path"), 200)
                            for e in (changes.get("edits") or [])[:_MAX_LIST]
                            if isinstance(e, dict)
                        ],
                        "round": revision,
                    },
                )
            )
        elif node == "apply_changes":
            events.append(
                RunEvent(
                    kind=RunEventKind.DEVELOPER_APPLIED,
                    ordinal=revision,
                    payload={"files": len(delta.get("changed_files", []))},
                )
            )
        elif node == "qa":
            if delta.get("completed") is True:
                done: dict[str, Any] = {}
                if approved_by is not None:
                    done["approved_by"] = approved_by
                events.append(
                    RunEvent(
                        kind=RunEventKind.RUN_COMPLETED,
                        payload=done,
                        context={"publication_expected": delta.get("publication_expected")},
                    )
                )
        elif node == "planner":
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
        elif node == "plan_gate":
            violations = delta.get("plan_violations") or []
            if violations:
                events.append(
                    RunEvent(
                        kind=RunEventKind.PLAN_GATE_REJECTED,
                        ordinal=revision,
                        payload={
                            "revision": revision,
                            "violations": [str(v.get("code")) for v in violations][:16],
                            "final": "error" in delta,
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
        elif node in ("approval", "approve_scope") and "approved" in delta:
            kind = RunEventKind.RUN_APPROVED if delta["approved"] else RunEventKind.RUN_REJECTED
            verdict: dict[str, Any] = {}
            if approved_by is not None:
                verdict["approved_by" if delta["approved"] else "rejected_by"] = approved_by
            ordinal = DecisionStage.SCOPE.event_ordinal if node == "approve_scope" else 0
            events.append(RunEvent(kind=kind, ordinal=ordinal, payload=verdict))
        elif node == "approve_plan" and "plan_approved" in delta:
            decided = bool(delta["plan_approved"])
            kind = RunEventKind.RUN_APPROVED if decided else RunEventKind.RUN_REJECTED
            verdict = {"stage": "plan"}
            if approved_by is not None:
                verdict["approved_by" if decided else "rejected_by"] = approved_by
            events.append(
                RunEvent(kind=kind, ordinal=DecisionStage.PLAN.event_ordinal, payload=verdict)
            )
        elif node == "approve_publish" and "publish_approved" in delta:
            decided = bool(delta["publish_approved"])
            kind = RunEventKind.RUN_APPROVED if decided else RunEventKind.RUN_REJECTED
            verdict = {"stage": "publish"}
            if approved_by is not None:
                verdict["approved_by" if decided else "rejected_by"] = approved_by
            events.append(
                RunEvent(kind=kind, ordinal=DecisionStage.PUBLISH.event_ordinal, payload=verdict)
            )
    for event in events:
        object.__setattr__(event, "checkpoint_id", checkpoint_id)
        if event.kind is not RunEventKind.RUN_FAILED:
            node_name = _node_of(event.kind)
            if node_name is not None and "executed_by" not in event.payload:
                event.payload["executed_by"] = EXECUTED_BY[node_name]
    return events


_NODE_BY_KIND: dict[RunEventKind, str] = {
    RunEventKind.PLANNER_COMPLETED: "planner",
    RunEventKind.REVISION_REQUESTED: "planner",
    RunEventKind.PLAN_GATE_REJECTED: "plan_gate",
    RunEventKind.ARCHITECT_REVIEWED: "architect",
    RunEventKind.SCOPE_PROPOSED: "scoper",
    RunEventKind.DEVELOPER_COMPLETED: "developer",
    RunEventKind.DEVELOPER_APPLIED: "developer",
    RunEventKind.QA_REVIEWED: "qa",
}


def _node_of(kind: RunEventKind) -> str | None:
    return _NODE_BY_KIND.get(kind)


def attribute(events: list[RunEvent], *, actor: str | None) -> list[RunEvent]:
    """Attach the human actor only to the events that human caused directly."""
    if actor is None:
        return events
    return [
        event.model_copy(update={"actor_subject": actor}) if event.kind in HUMAN_CAUSED else event
        for event in events
    ]


def transition_events(previous: RunLifecycleStatus, updated: PersistedRun) -> list[RunEvent]:
    """Events implied by a status change the stream could not report itself."""
    if previous == updated.status:
        return []
    status = updated.status
    if status is RunLifecycleStatus.INTERRUPTED:
        return [RunEvent(kind=RunEventKind.RUN_INTERRUPTED, ordinal=updated.attempt)]
    if status is RunLifecycleStatus.FAILED:
        code = updated.error.code if updated.error else "unknown"
        message = updated.error.message if updated.error else ""
        return [
            RunEvent(
                kind=RunEventKind.RUN_FAILED,
                payload={"code": code},
                context={"message": _text(message)},
            )
        ]
    if status is RunLifecycleStatus.APPROVED:
        return [RunEvent(kind=RunEventKind.RUN_APPROVED)]
    if status is RunLifecycleStatus.REJECTED:
        return [RunEvent(kind=RunEventKind.RUN_REJECTED)]
    if status is RunLifecycleStatus.COMPLETED:
        return [RunEvent(kind=RunEventKind.RUN_COMPLETED)]
    if status is RunLifecycleStatus.PUBLISHED:
        publication = updated.publication or {}
        return [
            RunEvent(
                kind=RunEventKind.RUN_PUBLISHED,
                payload={
                    "remote_branch": publication.get("remote_branch"),
                    "pr_number": publication.get("pr_number"),
                    "approved_by": publication.get("approved_by"),
                    "executed_by": "agentforge:publisher",
                },
            )
        ]
    if status is RunLifecycleStatus.CANCELLED:
        return [RunEvent(kind=RunEventKind.RUN_CANCELLED)]
    if status is RunLifecycleStatus.EXPIRED:
        expired = updated.error.code if updated.error else ""
        payload: dict[str, Any] = {}
        if expired.startswith("expired:"):
            payload = {"code": expired, "stage": expired.split(":", 1)[1]}
        return [RunEvent(kind=RunEventKind.RUN_EXPIRED, payload=payload)]
    return []


def _interrupt_payload(delta: Any) -> dict[str, Any]:
    """Stage and scope digest of an interrupt, never the proposal itself."""
    for item in delta if isinstance(delta, tuple | list) else (delta,):
        if isinstance(item, Interrupt) and isinstance(item.value, dict):
            payload: dict[str, Any] = {}
            for key in ("stage", "scope_digest", "publish_digest", "deadline"):
                if item.value.get(key) is not None:
                    payload[key] = item.value[key]
            return payload
    return {}


def _interrupt_ordinal(value: dict[str, Any], fallback: int) -> int:
    """Ordinal that keeps ``(run, kind, ordinal)`` unique for ``waiting_approval``:
    the stage ordinal when the interrupt names a decision stage (development flow),
    else the planner revision the planning flow puts in its payload."""
    stage = value.get("stage")
    if isinstance(stage, str):
        try:
            return DecisionStage(stage).event_ordinal
        except ValueError:
            pass
    revision = value.get("revision")
    return revision if isinstance(revision, int) else fallback


def _interrupt_revision(delta: Any, fallback: int) -> int:
    for item in delta if isinstance(delta, tuple | list) else (delta,):
        if isinstance(item, Interrupt) and isinstance(item.value, dict):
            return _interrupt_ordinal(item.value, fallback)
    return fallback


def _snapshot_interrupt_ordinal(snapshot: StateSnapshot, fallback: int) -> int:
    pending = [*snapshot.interrupts, *(i for task in snapshot.tasks for i in task.interrupts)]
    for item in pending:
        if isinstance(item, Interrupt) and isinstance(item.value, dict):
            return _interrupt_ordinal(item.value, fallback)
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
        events.extend(_development_events(values, previous, revision, checkpoint_id))
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
                    ordinal=_snapshot_interrupt_ordinal(snapshot, revision),
                    checkpoint_id=checkpoint_id,
                )
            )
        if "approved" in values and "approved" not in previous:
            kind = RunEventKind.RUN_APPROVED if values["approved"] else RunEventKind.RUN_REJECTED
            events.append(RunEvent(kind=kind, checkpoint_id=checkpoint_id))
        if values.get("completed") is True and not previous.get("completed"):
            events.append(RunEvent(kind=RunEventKind.RUN_COMPLETED, checkpoint_id=checkpoint_id))
        previous = dict(values)
        previously_interrupted = interrupted
    for event in events:
        node_name = _node_of(event.kind)
        if node_name is not None and "executed_by" not in event.payload:
            event.payload["executed_by"] = EXECUTED_BY[node_name]
    return events


def _development_events(
    values: dict[str, Any], previous: dict[str, Any], revision: int, checkpoint_id: str | None
) -> list[RunEvent]:
    """Developer/QA node events backed by a checkpoint whose values changed.

    A node ran when its output key changed or the revision moved since the
    previous checkpoint; re-emitting an identical ``(kind, ordinal)`` is a
    no-op on insert, so erring on the side of emitting is safe.
    """
    moved = int(previous.get("revision", 0) or 0) != revision
    events: list[RunEvent] = []

    def changed(key: str) -> bool:
        return key in values and (moved or values.get(key) != previous.get(key))

    if "scope_proposal" in values and values.get("scope_proposal") != previous.get(
        "scope_proposal"
    ):
        proposal = values.get("scope_proposal") or {}
        events.append(
            RunEvent(
                kind=RunEventKind.SCOPE_PROPOSED,
                ordinal=0,
                payload={
                    "files": len(proposal.get("files") or []),
                    "rejected": len(proposal.get("rejected") or []),
                    "tests": len(proposal.get("tests") or []),
                    "scope_digest": proposal.get("digest"),
                },
                checkpoint_id=checkpoint_id,
            )
        )

    if changed("changes"):
        events.append(
            RunEvent(
                kind=RunEventKind.DEVELOPER_COMPLETED,
                ordinal=revision,
                payload={"files": len((values.get("changes") or {}).get("edits", []))},
                checkpoint_id=checkpoint_id,
            )
        )
    if changed("developer_checks"):
        events.append(
            RunEvent(
                kind=RunEventKind.DEVELOPER_APPLIED,
                ordinal=revision,
                payload={"files": len(values.get("changed_files") or [])},
                checkpoint_id=checkpoint_id,
            )
        )
    if changed("qa_review"):
        validation = values.get("validation") or []
        events.append(
            RunEvent(
                kind=RunEventKind.QA_REVIEWED,
                ordinal=revision,
                payload={
                    "approved": bool((values.get("qa_review") or {}).get("approved")),
                    "issues": len((values.get("qa_review") or {}).get("issues", [])),
                    "checks": len(validation),
                    "failed_checks": sum(1 for r in validation if r.get("exit_code") != 0),
                },
                checkpoint_id=checkpoint_id,
            )
        )
    return events

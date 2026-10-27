from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import RunServiceDep
from app.api.pagination import (
    decode_event_cursor,
    decode_run_cursor,
    encode_event_cursor,
    encode_run_cursor,
    fingerprint,
)
from app.api.security import CurrentPrincipalDep
from app.contracts import (
    Approval,
    CreateRun,
    DecisionRead,
    EventPage,
    PendingDecisionRead,
    RunDecisions,
    RunEventRead,
    RunPage,
    RunResponse,
    RunSummary,
)
from app.contracts.execution import PROJECT_ID_PATTERN
from app.contracts.lifecycle import DecisionStage, RunLifecycleStatus
from app.orchestration.ports import PersistedEvent, PersistedRun

router = APIRouter(prefix="/runs", tags=["runs"])

# Path operations are ``async def``: RunService drives the graph with
# ``ainvoke`` so the async checkpointer can persist it. The synchronous LLM
# call inside the nodes runs in LangGraph's executor, never on the event loop.
# Routers never decide authorization: they hand the principal to the service.

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        description=(
            "Opcional. Misma clave y misma tarea: devuelve la ejecución existente (200, "
            "Idempotency-Replayed: true) sin ejecutar nada. Misma clave y otra tarea: 409. "
            "Ámbito: proyecto + principal; la clave queda ligada al run aunque este falle."
        ),
    ),
]
Limit = Annotated[int, Query(ge=1, le=100, description="Tamaño de página")]
Cursor = Annotated[
    str | None, Query(max_length=512, description="Cursor opaco de la página anterior")
]


def _summary(row: PersistedRun) -> RunSummary:
    return RunSummary(
        run_id=str(row.id),
        status=row.status,
        owner=row.owner_subject,
        project_id=row.project_id,
        workflow_kind=row.workflow_kind,
        revision=row.revision,
        llm_calls=row.llm_calls,
        architect_approved=row.architect_approved,
        attempt=row.attempt,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _event(event: PersistedEvent) -> RunEventRead:
    return RunEventRead(
        id=event.id,
        kind=event.kind,
        ordinal=event.ordinal,
        payload=event.payload,
        checkpoint_id=event.checkpoint_id,
        actor=event.actor_subject,
        initiated_by=event.payload.get("initiated_by"),
        approved_by=event.payload.get("approved_by"),
        executed_by=event.payload.get("executed_by"),
        created_at=event.created_at,
    )


@router.get("", response_model=RunPage)
async def list_runs(
    service: RunServiceDep,
    principal: CurrentPrincipalDep,
    limit: Limit = 20,
    cursor: Cursor = None,
    status_filter: Annotated[RunLifecycleStatus | None, Query(alias="status")] = None,
    project_id: Annotated[str | None, Query(pattern=PROJECT_ID_PATTERN)] = None,
    owner: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    mine: Annotated[bool, Query(description="Solo mis ejecuciones")] = False,
) -> RunPage:
    """Runs visible to the caller, newest first. Keyset pagination."""
    effective_owner = principal.subject if mine else owner
    fp = fingerprint(
        subject=principal.subject,
        status=status_filter,
        project_id=project_id,
        owner=effective_owner,
    )
    before = decode_run_cursor(cursor, filters_fingerprint=fp) if cursor else None
    listing = await service.list_runs(
        principal,
        status=status_filter,
        project_id=project_id,
        owner=effective_owner,
        before=before,
        limit=limit,
    )
    next_cursor = None
    if listing.has_more and listing.items:
        last = listing.items[-1]
        next_cursor = encode_run_cursor(last.created_at, last.id, filters_fingerprint=fp)
    return RunPage(items=[_summary(r) for r in listing.items], limit=limit, next_cursor=next_cursor)


@router.post("", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    body: CreateRun,
    service: RunServiceDep,
    principal: CurrentPrincipalDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> RunResponse:
    """Create a run in one of the caller's projects. A run that fails inside
    the graph is still created (201) and reports ``status == "failed"``."""
    outcome = await service.create(
        body.task,
        principal,
        project_id=body.project_id,
        idempotency_key=idempotency_key,
        workflow_kind=body.workflow_kind,
        repository_id=body.repository_id,
    )
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.response


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str, service: RunServiceDep, principal: CurrentPrincipalDep
) -> RunResponse:
    return await service.get(run_id, principal)


@router.post("/{run_id}/approve", response_model=RunResponse)
async def approve_run(
    run_id: str, body: Approval, service: RunServiceDep, principal: CurrentPrincipalDep
) -> RunResponse:
    """Decide the stage the run waits for (``runs:approve``).

    A ``scope`` decision also needs ``repos:write`` and the ``scope_digest`` of
    the proposal shown by ``GET /runs/{run_id}/decisions``; a ``publish`` decision
    needs ``repos:publish``, a human distinct from the creator (and from the
    scope approver where configured) and the ``publish_digest`` of the plan
    shown. A different or missing digest is ``409 decision_stale``; a wrong
    stage is ``409 decision_stage_mismatch``; a closed window is
    ``409 decision_expired``."""
    return await service.approve(
        run_id,
        body.approved,
        principal,
        stage=body.stage,
        scope_digest=body.scope_digest,
        publish_digest=body.publish_digest,
    )


@router.get("/{run_id}/decisions", response_model=RunDecisions)
async def list_run_decisions(
    run_id: str, service: RunServiceDep, principal: CurrentPrincipalDep
) -> RunDecisions:
    """Decisions taken so far and the pending one with its proposal (visible runs only)."""
    view = await service.decisions(run_id, principal)
    pending = (
        PendingDecisionRead(
            stage=view.pending.stage,
            scope_digest=view.pending.scope_digest,
            publish_digest=view.pending.publish_digest,
            deadline=view.pending.deadline,
            proposal={
                k: v
                for k, v in view.pending.payload.items()
                if k not in ("kind", "stage", "scope_digest", "publish_digest", "deadline")
            },
        )
        if view.pending is not None
        else None
    )
    return RunDecisions(
        run_id=view.run_id,
        status=view.status,
        pending=pending,
        decisions=[
            DecisionRead(
                stage=d.stage,
                approved=d.approved,
                decided_by=d.decided_by,
                decided_at=d.decided_at,
                applied_at=d.applied_at,
                scope_digest=d.scope_digest if d.stage is not DecisionStage.PUBLISH else None,
                publish_digest=d.scope_digest if d.stage is DecisionStage.PUBLISH else None,
            )
            for d in view.decisions
        ],
    )


@router.post("/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(
    run_id: str, service: RunServiceDep, principal: CurrentPrincipalDep
) -> RunResponse:
    """Withdraw a run that waits for a decision or sits interrupted (``runs:cancel``).

    ``409 run_not_cancellable`` for any other state; a run being driven is not
    cancelled through the API in this phase."""
    return await service.cancel(run_id, principal)


@router.post("/{run_id}/resume", response_model=RunResponse)
async def resume_run(
    run_id: str, service: RunServiceDep, principal: CurrentPrincipalDep
) -> RunResponse:
    """Re-drive an ``interrupted`` run from its last checkpoint (``runs:resume``).

    Synchronous like creation: returns the run once it reached its next stop.
    409 ``run_not_resumable`` when it is not interrupted, another process holds
    its lease, or it exhausted its attempts."""
    return await service.resume(run_id, principal)


@router.get("/{run_id}/events", response_model=EventPage)
async def list_run_events(
    run_id: str,
    service: RunServiceDep,
    principal: CurrentPrincipalDep,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> EventPage:
    """Audit trail of a run, oldest first (``runs:audit:read``)."""
    fp = fingerprint(subject=principal.subject, run_id=run_id)
    after_id = decode_event_cursor(cursor, filters_fingerprint=fp) if cursor else None
    listing = await service.list_events(run_id, principal, after_id=after_id, limit=limit)
    next_cursor = None
    if listing.has_more and listing.items:
        next_cursor = encode_event_cursor(listing.items[-1].id, filters_fingerprint=fp)
    return EventPage(items=[_event(e) for e in listing.items], limit=limit, next_cursor=next_cursor)

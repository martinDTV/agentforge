"""Goals (Fase 8, ADR 0012): create an objective, follow its tasks, withdraw it.

Routers never decide authorization: they hand the principal to ``GoalService``.
Human decisions on the children keep living in ``POST /runs/{run_id}/approve``.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from app.api.pagination import (
    decode_event_cursor,
    decode_goal_cursor,
    encode_event_cursor,
    encode_goal_cursor,
    fingerprint,
)
from app.api.security import CurrentPrincipalDep
from app.contracts.execution import PROJECT_ID_PATTERN
from app.contracts.goals import (
    CreateGoal,
    GoalEventPage,
    GoalPage,
    GoalResponse,
    GoalStatus,
)
from app.orchestration.exceptions import SupervisorUnavailableError
from app.orchestration.goals import GoalService, event_read, summary_of

router = APIRouter(prefix="/goals", tags=["goals"])

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        description=(
            "Opcional. Misma clave y mismo objetivo: devuelve el objetivo existente (200, "
            "Idempotency-Replayed: true). Misma clave y otro objetivo: 409. "
            "Ámbito: proyecto + principal."
        ),
    ),
]
Limit = Annotated[int, Query(ge=1, le=100, description="Tamaño de página")]
Cursor = Annotated[
    str | None, Query(max_length=512, description="Cursor opaco de la página anterior")
]


def get_goal_service(request: Request) -> GoalService:
    service: GoalService | None = getattr(request.app.state, "goal_service", None)
    if service is None:
        raise SupervisorUnavailableError()
    return service


GoalServiceDep = Annotated[GoalService, Depends(get_goal_service)]


@router.post("", response_model=GoalResponse, status_code=status.HTTP_201_CREATED)
async def create_goal(
    body: CreateGoal,
    service: GoalServiceDep,
    principal: CurrentPrincipalDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> GoalResponse:
    """Create a goal (``runs:create`` + ``repos:write``). Returns once the first
    reconciliation ran: the planning run or the first child is already reserved."""
    outcome = await service.create(body, principal, idempotency_key=idempotency_key)
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.response


@router.get("", response_model=GoalPage)
async def list_goals(
    service: GoalServiceDep,
    principal: CurrentPrincipalDep,
    limit: Limit = 20,
    cursor: Cursor = None,
    status_filter: Annotated[GoalStatus | None, Query(alias="status")] = None,
    project_id: Annotated[str | None, Query(pattern=PROJECT_ID_PATTERN)] = None,
    mine: Annotated[bool, Query(description="Solo mis objetivos")] = False,
) -> GoalPage:
    owner = principal.subject if mine else None
    fp = fingerprint(
        subject=principal.subject, status=status_filter, project_id=project_id, owner=owner
    )
    before = decode_goal_cursor(cursor, filters_fingerprint=fp) if cursor else None
    listing = await service.list(
        principal,
        status=status_filter,
        project_id=project_id,
        owner=owner,
        before=before,
        limit=limit,
    )
    items = [
        summary_of(goal, await service.coordinator.goals.list_tasks(goal.id))
        for goal in listing.items
    ]
    next_cursor = None
    if listing.has_more and listing.items:
        last = listing.items[-1]
        next_cursor = encode_goal_cursor(last.created_at, last.id, filters_fingerprint=fp)
    return GoalPage(items=items, limit=limit, next_cursor=next_cursor)


@router.get("/{goal_id}", response_model=GoalResponse)
async def get_goal(
    goal_id: str, service: GoalServiceDep, principal: CurrentPrincipalDep
) -> GoalResponse:
    return await service.get(goal_id, principal)


@router.get("/{goal_id}/events", response_model=GoalEventPage)
async def list_goal_events(
    goal_id: str,
    service: GoalServiceDep,
    principal: CurrentPrincipalDep,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> GoalEventPage:
    """Audit trail of a goal, oldest first (``runs:audit:read``)."""
    fp = fingerprint(subject=principal.subject, goal_id=goal_id)
    after_id = decode_event_cursor(cursor, filters_fingerprint=fp) if cursor else None
    listing = await service.events(goal_id, principal, after_id=after_id, limit=limit)
    next_cursor = None
    if listing.has_more and listing.items:
        next_cursor = encode_event_cursor(listing.items[-1].id, filters_fingerprint=fp)
    return GoalEventPage(
        items=[event_read(e) for e in listing.items], limit=limit, next_cursor=next_cursor
    )


@router.post("/{goal_id}/cancel", response_model=GoalResponse)
async def cancel_goal(
    goal_id: str, service: GoalServiceDep, principal: CurrentPrincipalDep
) -> GoalResponse:
    """Withdraw a goal (``runs:cancel``): children waiting for a decision are cancelled,
    running ones finish on their own. ``409 goal_not_cancellable`` once terminal."""
    return await service.cancel(goal_id, principal)


@router.post("/{goal_id}/reconcile", response_model=GoalResponse)
async def reconcile_goal(
    goal_id: str, service: GoalServiceDep, principal: CurrentPrincipalDep
) -> GoalResponse:
    """Run one reconciliation now (``recovery:sweep``). ``409 goal_reconcile_in_progress``
    when another process holds the goal's lease."""
    return await service.reconcile(goal_id, principal)

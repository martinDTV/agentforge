"""Administrative operations (``recovery:sweep``).

The sweep runs *inside* the request and the response carries the real
report: no 202 backed only by an in-memory task. The work is bounded by
``RECOVERY_BATCH_SIZE`` and may include LLM calls when auto-resume is on, so
clients should allow a long timeout; the periodic sweeper keeps running
regardless of this endpoint.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import RecoveryServiceDep
from app.api.security import CurrentPrincipalDep
from app.contracts import SweepResult
from app.contracts.execution import PROJECT_ID_PATTERN
from app.security import AccessScope

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/recovery/sweeps", response_model=SweepResult)
async def run_recovery_sweep(
    recovery: RecoveryServiceDep,
    principal: CurrentPrincipalDep,
    project_id: Annotated[str | None, Query(pattern=PROJECT_ID_PATTERN)] = None,
) -> SweepResult:
    """Run one recovery sweep now, limited to the caller's projects unless it
    holds ``projects:all``. Synchronous; returns what actually happened.
    ``409 sweep_in_progress`` when this instance is already sweeping."""
    scope: AccessScope = recovery.service.authorization.sweep_scope(principal).narrowed_to(
        project_id
    )
    # Fail fast (409) if this instance is already sweeping: never stack sweeps.
    report = await recovery.sweep(projects=scope.projects, actor=principal.subject, wait=False)
    return SweepResult(
        claimed=report.claimed,
        outcomes=report.outcomes,
        errors=report.errors,
        projects=None if scope.projects is None else sorted(scope.projects),
    )

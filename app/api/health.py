"""Liveness and readiness probes.

Not versioned and not in the OpenAPI schema. Liveness only proves the process
is up; readiness proves the database answers, runs the expected business
schema and (when persistent) has the LangGraph checkpoint tables, so an
orchestrator stops routing traffic while any of that is false.
"""

from fastapi import APIRouter, Request, Response, status

from app.infrastructure.checkpoints import CheckpointSchemaError, PostgresCheckpointer
from app.infrastructure.db import (
    Database,
    DatabaseUnavailableError,
    SchemaMismatchError,
    verify_schema,
)

router = APIRouter(prefix="/health", tags=["health"], include_in_schema=False)
_UNAVAILABLE = status.HTTP_503_SERVICE_UNAVAILABLE


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


def _notifications(state: object) -> str:
    """``worker`` while the in-process mail worker runs; ``disabled`` without e-mail."""
    if getattr(state, "notification_worker", None) is None:
        return "disabled"
    task = getattr(state, "notification_task", None)
    return "stopped" if task is None or task.done() else "worker"


@router.get("/ready")
async def ready(request: Request, response: Response) -> dict[str, str]:
    state = request.app.state
    database: Database | None = state.database
    if database is None:
        response.status_code = _UNAVAILABLE
        return {"status": "unavailable", "database": "not_configured"}
    try:
        await database.ping()
    except DatabaseUnavailableError:
        response.status_code = _UNAVAILABLE
        return {"status": "unavailable", "database": "unreachable"}
    if not state.schema_verified:
        try:
            await verify_schema(database.engine)
        except SchemaMismatchError:
            response.status_code = _UNAVAILABLE
            return {"status": "unavailable", "database": "ok", "schema": "mismatch"}
        except DatabaseUnavailableError:
            response.status_code = _UNAVAILABLE
            return {"status": "unavailable", "database": "unreachable"}
        state.schema_verified = True

    checkpoints: PostgresCheckpointer | None = state.checkpoints
    if checkpoints is None:
        return {
            "status": "ok",
            "database": "ok",
            "schema": "ok",
            "checkpoints": "memory",
            "notifications": _notifications(state),
        }
    if not state.checkpoints_verified:
        try:
            await checkpoints.verify_schema()
        except CheckpointSchemaError:
            response.status_code = _UNAVAILABLE
            return {
                "status": "unavailable",
                "database": "ok",
                "schema": "ok",
                "checkpoints": "missing",
            }
        except DatabaseUnavailableError:
            response.status_code = _UNAVAILABLE
            return {"status": "unavailable", "database": "unreachable"}
        state.checkpoints_verified = True
    return {
        "status": "ok",
        "database": "ok",
        "schema": "ok",
        "checkpoints": "ok",
        "notifications": _notifications(state),
    }

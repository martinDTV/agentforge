from typing import Annotated

from fastapi import APIRouter, Header, Response, status

from app.api.dependencies import RunServiceDep
from app.contracts import Approval, CreateRun, RunResponse

router = APIRouter(prefix="/runs", tags=["runs"])

# Path operations are ``async def``: RunService drives the graph with
# ``ainvoke`` so the async checkpointer can persist it. The synchronous LLM
# call inside the nodes runs in LangGraph's executor, never on the event loop.

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        description=(
            "Opcional. Misma clave y misma tarea: devuelve la ejecución existente (200, "
            "Idempotency-Replayed: true) sin ejecutar nada. Misma clave y otra tarea: 409. "
            "Ámbito global del despliegue; la clave queda ligada al run aunque este falle."
        ),
    ),
]


@router.post("", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    body: CreateRun,
    service: RunServiceDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> RunResponse:
    """Create a run. A run that fails inside the graph is still created (201)
    and reports ``status == "failed"`` with a structured ``error``."""
    outcome = await service.create(body.task, idempotency_key=idempotency_key)
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotency-Replayed"] = "true"
    return outcome.response


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, service: RunServiceDep) -> RunResponse:
    return await service.get(run_id)


@router.post("/{run_id}/approve", response_model=RunResponse)
async def approve_run(run_id: str, body: Approval, service: RunServiceDep) -> RunResponse:
    return await service.approve(run_id, body.approved)

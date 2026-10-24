from fastapi import APIRouter, status

from app.api.dependencies import RunServiceDep
from app.contracts import Approval, CreateRun, RunResponse

router = APIRouter(prefix="/runs", tags=["runs"])

# Path operations are plain ``def`` on purpose: RunService drives a blocking
# LangGraph invocation (and, in production, a blocking Vertex AI call), so
# FastAPI runs them in its threadpool instead of stalling the event loop.


@router.post("", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
def create_run(body: CreateRun, service: RunServiceDep) -> RunResponse:
    """Create a run. A run that fails inside the graph is still created (201)
    and reports ``status == "failed"`` with a structured ``error``."""
    return service.create(body.task)


@router.get("/{run_id}", response_model=RunResponse)
def get_run(run_id: str, service: RunServiceDep) -> RunResponse:
    return service.get(run_id)


@router.post("/{run_id}/approve", response_model=RunResponse)
def approve_run(run_id: str, body: Approval, service: RunServiceDep) -> RunResponse:
    return service.approve(run_id, body.approved)

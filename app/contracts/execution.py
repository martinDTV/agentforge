from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .lifecycle import RunEventKind, RunLifecycleStatus
from .planning import ArchitectureReview, DevelopmentPlan

# One definition of the run states for API, orchestration and persistence.
RunStatus = RunLifecycleStatus


PROJECT_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"


class CreateRun(BaseModel):
    task: str = Field(min_length=10, max_length=4000)
    # Optional when the token names exactly one project; required otherwise.
    project_id: str | None = Field(default=None, pattern=PROJECT_ID_PATTERN)


class Approval(BaseModel):
    approved: bool


class RunError(BaseModel):
    """Why a run ended in ``failed``. Stable code, human-readable message, no traces."""

    code: str
    message: str


class RunResponse(BaseModel):
    run_id: str
    status: RunStatus
    owner: str = Field(description="Sujeto opaco del principal que creó la ejecución")
    project_id: str
    revision: int = Field(default=0, ge=0, description="Plan revisions triggered by the architect")
    llm_calls: int = Field(default=0, ge=0, description="Agent calls consumed by this run")
    architect_approved: bool | None = Field(
        default=None, description="Architect verdict on the current plan; null if it never ran"
    )
    plan: DevelopmentPlan | None = None
    architecture: ArchitectureReview | None = None
    error: RunError | None = None


class RunSummary(BaseModel):
    """List item: everything but the plan and the review (fetch the run for those)."""

    run_id: str
    status: RunStatus
    owner: str
    project_id: str
    revision: int = 0
    llm_calls: int = 0
    architect_approved: bool | None = None
    attempt: int = 0
    created_at: datetime
    updated_at: datetime


class RunPage(BaseModel):
    items: list[RunSummary]
    limit: int
    next_cursor: str | None = Field(
        default=None,
        description="Cursor opaco para la página siguiente; ausente en la última. "
        "Ligado a los filtros de la consulta.",
    )


class RunEventRead(BaseModel):
    id: int
    kind: RunEventKind
    ordinal: int
    payload: dict[str, Any]
    checkpoint_id: str | None = None
    actor: str | None = Field(
        default=None, description="Sujeto del principal; null para agentes y sistema"
    )
    created_at: datetime


class EventPage(BaseModel):
    items: list[RunEventRead]
    limit: int
    next_cursor: str | None = None


class SweepResult(BaseModel):
    """Outcome of one synchronous recovery sweep."""

    claimed: int
    outcomes: dict[str, int]
    errors: int
    projects: list[str] | None = Field(
        default=None, description="Proyectos barridos; null = todos (projects:all)"
    )

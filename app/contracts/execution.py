from typing import Literal

from pydantic import BaseModel, Field

from .planning import ArchitectureReview, DevelopmentPlan

RunStatus = Literal["waiting_approval", "approved", "rejected", "failed"]


class CreateRun(BaseModel):
    task: str = Field(min_length=10, max_length=4000)


class Approval(BaseModel):
    approved: bool


class RunError(BaseModel):
    """Why a run ended in ``failed``. Stable code, human-readable message, no traces."""

    code: str
    message: str


class RunResponse(BaseModel):
    run_id: str
    status: RunStatus
    revision: int = Field(default=0, ge=0, description="Plan revisions triggered by the architect")
    llm_calls: int = Field(default=0, ge=0, description="Agent calls consumed by this run")
    architect_approved: bool | None = Field(
        default=None, description="Architect verdict on the current plan; null if it never ran"
    )
    plan: DevelopmentPlan | None = None
    architecture: ArchitectureReview | None = None
    error: RunError | None = None

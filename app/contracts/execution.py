from typing import Literal

from pydantic import BaseModel, Field

from .planning import ArchitectureReview, DevelopmentPlan


class CreateRun(BaseModel):
    task: str = Field(min_length=10, max_length=4000)


class Approval(BaseModel):
    approved: bool


class RunResponse(BaseModel):
    run_id: str
    status: Literal["waiting_approval", "approved", "rejected"]
    plan: DevelopmentPlan
    architecture: ArchitectureReview | None = None

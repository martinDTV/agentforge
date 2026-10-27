from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .lifecycle import DecisionStage, RunEventKind, RunLifecycleStatus, WorkflowKind
from .planning import ArchitectureReview, DevelopmentPlan
from .routing import Route, RouteMode

# One definition of the run states for API, orchestration and persistence.
RunStatus = RunLifecycleStatus


PROJECT_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"


class CreateRun(BaseModel):
    task: str = Field(min_length=10, max_length=4000)
    # Optional when the token names exactly one project; required otherwise.
    project_id: str | None = Field(default=None, pattern=PROJECT_ID_PATTERN)
    workflow_kind: WorkflowKind = Field(
        default=WorkflowKind.PLANNING,
        description="``development`` requires ``repository_id`` and ``repos:write`` (ADR 0009)",
    )
    repository_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    route_mode: RouteMode | None = Field(
        default=None,
        description=(
            "Smart Router: ``auto`` (rules + classifier), ``fast`` (Scoper first; mandatory "
            "rules may still force ``full``) or ``full`` (Planner and Architect first). "
            "Development runs only."
        ),
    )


class Approval(BaseModel):
    approved: bool
    stage: DecisionStage | None = Field(
        default=None, description="Stage being decided; must match the pending one when given"
    )
    scope_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="Required for ``scope``: digest of the exact proposal shown",
    )
    publish_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="Required for ``publish``: digest of the exact publication plan shown",
    )


class DecisionRead(BaseModel):
    stage: DecisionStage
    approved: bool
    decided_by: str | None
    decided_at: datetime
    applied_at: datetime | None
    scope_digest: str | None
    publish_digest: str | None = None


class PendingDecisionRead(BaseModel):
    stage: DecisionStage
    scope_digest: str | None
    publish_digest: str | None = None
    deadline: datetime | None
    proposal: dict[str, Any] = Field(
        default_factory=dict, description="What the human must decide on (files, tests, limits)"
    )


class RunDecisions(BaseModel):
    run_id: str
    status: RunStatus
    pending: PendingDecisionRead | None
    decisions: list[DecisionRead]


class RunError(BaseModel):
    """Why a run ended in ``failed``. Stable code, human-readable message, no traces."""

    code: str
    message: str


class RunResponse(BaseModel):
    run_id: str
    status: RunStatus
    owner: str = Field(description="Sujeto opaco del principal que creó la ejecución")
    project_id: str
    workflow_kind: WorkflowKind = Field(
        default=WorkflowKind.PLANNING, description="Graph that drives the run (ADR 0008)"
    )
    pending_stage: DecisionStage | None = Field(
        default=None, description="Human decision the run waits for (ADR 0009)"
    )
    approval_deadline: datetime | None = Field(
        default=None, description="When the pending decision expires, if it does"
    )
    publication: dict[str, Any] | None = Field(
        default=None, description="Remote branch and pull request, once published (Fase 7)"
    )
    route: Route | None = Field(default=None, description="Route followed: fast or full")
    route_mode: RouteMode | None = Field(default=None, description="Route mode requested")
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
    workflow_kind: WorkflowKind = WorkflowKind.PLANNING
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
        default=None,
        description="Sujeto del principal que causó el evento; null para agentes y sistema",
    )
    # Attribution (ADR 0009): who asked, who authorised, which automation identity acted.
    initiated_by: str | None = None
    approved_by: str | None = None
    executed_by: str | None = None
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

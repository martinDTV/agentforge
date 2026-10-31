"""Goal (objetivo) vocabulary shared by the API contract, the goal domain, the
coordinator and the persistence tables (Fase 8, Paso 1; ADR 0012).

A goal is an operator-level objective that the deterministic Supervisor turns
into ordinary runs (one ``planning`` run to decompose, then one ``development``
run per task). Nothing here knows about LangGraph, FastAPI or SQL.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .execution import PROJECT_ID_PATTERN, RunError, RunStatus
from .lifecycle import DecisionStage

REPOSITORY_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
SHA_PATTERN = r"^[0-9a-f]{40}$"
MAX_GOAL_TASKS = 8


class GoalDecomposition(StrEnum):
    AUTO = "auto"  # Smart Router decides: fast -> one task, full -> a planning run
    SINGLE = "single"  # exactly one task with the objective's text, no planning
    PLAN = "plan"  # always Planner -> Architect -> human plan approval


class DependencyPolicy(StrEnum):
    # A dependent task starts only after the pull request of each dependency was
    # merged on GitHub *and* the merge commit is present in the mirrored base.
    AFTER_MERGED = "after_merged"


class GoalStatus(StrEnum):
    PLANNING = "planning"  # waiting for the planning run (and its human approval)
    ACTIVE = "active"  # tasks are being created, run and merged
    BLOCKED = "blocked"  # nothing advances until a human decides something
    COMPLETED = "completed"  # every task published (leaves) or merged
    PARTIAL = "partial"  # some tasks succeeded, at least one failed or was skipped
    FAILED = "failed"  # no task succeeded, or the plan was refused
    CANCELLED = "cancelled"  # a human withdrew the goal
    EXPIRED = "expired"  # the goal's deadline passed


GOAL_TERMINAL_STATUSES: frozenset[GoalStatus] = frozenset(
    {
        GoalStatus.COMPLETED,
        GoalStatus.PARTIAL,
        GoalStatus.FAILED,
        GoalStatus.CANCELLED,
        GoalStatus.EXPIRED,
    }
)


class GoalTaskStatus(StrEnum):
    PENDING = "pending"  # dependencies not merged yet
    READY = "ready"  # may be created when capacity and budget allow
    CREATING = "creating"  # run id reserved; RunService.create in flight
    RUNNING = "running"  # the child run is being driven (or recovery owns it)
    WAITING_HUMAN = "waiting_human"  # the child waits for plan/scope/publish approval
    PUBLISHED = "published"  # the child ended published (or completed, local repository)
    MERGED = "merged"  # merge verified on GitHub and in the mirror
    FAILED = "failed"  # the child failed, was rejected, expired or cancelled
    SKIPPED = "skipped"  # a dependency failed
    DUPLICATE = "duplicate"  # an identical task is already active in the repository


# Statuses during which the task occupies the repository's fingerprint slot
# (the partial unique index) and counts against the concurrency limits.
ACTIVE_TASK_STATUSES: frozenset[GoalTaskStatus] = frozenset(
    {
        GoalTaskStatus.PENDING,
        GoalTaskStatus.READY,
        GoalTaskStatus.CREATING,
        GoalTaskStatus.RUNNING,
        GoalTaskStatus.WAITING_HUMAN,
    }
)
# Tasks that occupy an execution slot (a worktree, model calls, the sandbox).
IN_FLIGHT_TASK_STATUSES: frozenset[GoalTaskStatus] = frozenset(
    {GoalTaskStatus.CREATING, GoalTaskStatus.RUNNING, GoalTaskStatus.WAITING_HUMAN}
)
TERMINAL_TASK_STATUSES: frozenset[GoalTaskStatus] = frozenset(
    {
        GoalTaskStatus.MERGED,
        GoalTaskStatus.FAILED,
        GoalTaskStatus.SKIPPED,
        GoalTaskStatus.DUPLICATE,
    }
)
SUCCESSFUL_TASK_STATUSES: frozenset[GoalTaskStatus] = frozenset(
    {GoalTaskStatus.PUBLISHED, GoalTaskStatus.MERGED}
)


class GoalLimits(BaseModel):
    """Aggregate limits of one goal; snapshotted into the row when the goal is created.

    ``max_total_model_calls`` may be omitted in the request: the service resolves it
    to ``planning calls + max_tasks x policy.max_model_calls`` before storing it.
    Paso 1 runs one child at a time, so ``max_concurrency`` is fixed at one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tasks: int = Field(default=5, ge=1, le=MAX_GOAL_TASKS)
    max_concurrency: int = Field(default=1, ge=1, le=1)
    max_total_model_calls: int | None = Field(default=None, ge=1)
    max_total_usd: float = Field(default=25.0, gt=0, le=100)
    deadline_seconds: float = Field(default=7 * 86_400.0, gt=0, le=14 * 86_400.0)
    merge_reminder_seconds: float = Field(default=86_400.0, gt=0, le=7 * 86_400.0)
    decision_reminder_seconds: float = Field(default=86_400.0, gt=0, le=7 * 86_400.0)
    # H2: last notice before a decision window closes (only when the window is long enough
    # for it to fall after the midway reminder). Older goals load with the default.
    final_notice_seconds: float = Field(default=3_600.0, ge=60.0, le=86_400.0)


class CreateGoal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=20, max_length=4000)
    # Optional when the token names exactly one project; required otherwise.
    project_id: str | None = Field(default=None, pattern=PROJECT_ID_PATTERN)
    repository_id: str = Field(pattern=REPOSITORY_ID_PATTERN)
    decomposition: GoalDecomposition = GoalDecomposition.AUTO
    dependency_policy: DependencyPolicy = DependencyPolicy.AFTER_MERGED
    limits: GoalLimits | None = Field(
        default=None, description="Aggregate limits; omitted values take the platform defaults"
    )


class MergeEvidence(BaseModel):
    """Why the platform believes a dependency is integrated (ADR 0012 §4.3)."""

    model_config = ConfigDict(frozen=True)

    pr_number: int = Field(gt=0)
    merged_at: datetime
    merge_commit_sha: str = Field(pattern=SHA_PATTERN)
    verified_in_mirror: bool
    base_after: str | None = Field(default=None, pattern=SHA_PATTERN)


class GoalUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    tasks_created: int = Field(default=0, ge=0)
    reserved_model_calls: int = Field(default=0, ge=0)
    reserved_usd: float = Field(default=0.0, ge=0)
    llm_calls_used: int = Field(default=0, ge=0)


class GoalTaskRead(BaseModel):
    task_key: str
    title: str
    status: GoalTaskStatus
    ordinal: int
    dependencies: list[str]
    attempt: int = 0
    run_id: str | None = None
    run_status: RunStatus | None = None
    pending_stage: DecisionStage | None = None
    approval_deadline: datetime | None = None
    pending_digest: str | None = Field(
        default=None, description="Scope digest captured when the child waited at ``scope``"
    )
    base_sha: str | None = None
    head_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    merge_evidence: MergeEvidence | None = None
    last_error_code: str | None = None


class GoalResponse(BaseModel):
    goal_id: str
    status: GoalStatus
    owner: str = Field(description="Sujeto opaco del principal que creó el objetivo")
    project_id: str
    repository_id: str
    objective: str
    decomposition: GoalDecomposition
    dependency_policy: DependencyPolicy
    limits: GoalLimits
    usage: GoalUsage
    plan_run_id: str | None = None
    route: str | None = Field(default=None, description="Route the router chose for ``auto``")
    deadline: datetime
    blocked_reasons: list[str] = Field(default_factory=list)
    tasks: list[GoalTaskRead] = Field(default_factory=list)
    error: RunError | None = None
    created_at: datetime
    updated_at: datetime


class GoalSummary(BaseModel):
    goal_id: str
    status: GoalStatus
    owner: str
    project_id: str
    repository_id: str
    decomposition: GoalDecomposition
    tasks_total: int = 0
    tasks_done: int = 0
    created_at: datetime
    updated_at: datetime


class GoalPage(BaseModel):
    items: list[GoalSummary]
    limit: int
    next_cursor: str | None = None


class GoalEventKind(StrEnum):
    GOAL_CREATED = "goal_created"
    ROUTE_SELECTED = "route_selected"
    PLAN_REQUESTED = "plan_requested"
    PLAN_APPROVED = "plan_approved"
    PLAN_REFUSED = "plan_refused"
    TASKS_MATERIALIZED = "tasks_materialized"
    TASK_READY = "task_ready"
    CHILD_CREATED = "child_created"
    CHILD_CANCELLED = "child_cancelled"
    TASK_WAITING_HUMAN = "task_waiting_human"
    TASK_BASE_RECORDED = "task_base_recorded"
    TASK_BASE_STALE = "task_base_stale"
    TASK_PUBLISHED = "task_published"
    DEPENDENCY_MERGED = "dependency_merged"
    MERGE_UNVERIFIED = "merge_unverified"
    PROBE_FAILED = "probe_failed"
    PR_CLOSED = "pr_closed"
    TASK_FAILED = "task_failed"
    TASK_SKIPPED = "task_skipped"
    TASK_DUPLICATE = "task_duplicate"
    CAPACITY_WAIT = "capacity_wait"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NEEDS_HUMAN = "needs_human"
    GOAL_COMPLETED = "goal_completed"
    GOAL_PARTIAL = "goal_partial"
    GOAL_FAILED = "goal_failed"
    GOAL_CANCELLED = "goal_cancelled"
    GOAL_EXPIRED = "goal_expired"


class GoalEventRead(BaseModel):
    id: int
    kind: GoalEventKind
    ordinal: int
    payload: dict[str, Any]
    actor: str | None = None
    created_at: datetime


class GoalEventPage(BaseModel):
    items: list[GoalEventRead]
    limit: int
    next_cursor: str | None = None


class PlannedUpstream(BaseModel):
    """Told to a child development run created from an approved goal plan (D4):
    the architecture review already happened over the whole plan, so the router
    may skip Planner and Architect when the repository policy allows the fast route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_id: str
    task_key: str
    plan_run_id: str

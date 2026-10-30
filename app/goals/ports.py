"""Ports of the goal domain: persistence of goals, tasks and events, and the
integration probe that proves a dependency was merged. Implementations live in
``app/infrastructure`` (PostgreSQL, in-memory, GitHub + mirror)."""

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.execution import RunError
from app.contracts.goals import (
    DependencyPolicy,
    GoalDecomposition,
    GoalEventKind,
    GoalLimits,
    GoalStatus,
    GoalTaskStatus,
    GoalUsage,
    MergeEvidence,
)
from app.security import AccessScope


class NewGoal(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    project_id: str
    repository_id: str
    owner_subject: str
    objective: str
    decomposition: GoalDecomposition
    dependency_policy: DependencyPolicy
    status: GoalStatus
    limits: GoalLimits
    deadline: datetime
    route: str | None = None
    route_decision: dict[str, Any] | None = None
    idempotency_key: str | None = None


class NewGoalTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_key: str
    title: str
    task_text: str
    dependencies: list[str] = Field(default_factory=list)
    ordinal: int = Field(ge=0)
    repository_id: str
    fingerprint: str
    status: GoalTaskStatus = GoalTaskStatus.PENDING


class PersistedGoal(BaseModel):
    """Read model of a ``goals`` row. Satisfies ``OwnedRun`` for the authorization policy."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    project_id: str
    repository_id: str
    owner_subject: str
    objective: str
    decomposition: GoalDecomposition
    dependency_policy: DependencyPolicy
    status: GoalStatus
    limits: GoalLimits
    deadline: datetime
    error: RunError | None = None
    route: str | None = None
    route_decision: dict[str, Any] | None = None
    plan_run_id: UUID | None = None
    plan_creating_since: datetime | None = None
    usage: GoalUsage = GoalUsage()
    blocked_reasons: list[str] = Field(default_factory=list)
    next_reconcile_at: datetime | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    idempotency_key: str | None = None
    created_at: datetime
    updated_at: datetime


class PersistedGoalTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    goal_id: UUID
    task_key: str
    title: str
    task_text: str
    dependencies: list[str] = Field(default_factory=list)
    ordinal: int
    repository_id: str
    fingerprint: str
    status: GoalTaskStatus
    attempt: int = 0
    last_error_code: str | None = None
    run_id: UUID | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    merge_evidence: MergeEvidence | None = None
    scope_digest: str | None = None
    scope_paths: list[str] | None = None
    creating_since: datetime | None = None
    waiting_since: datetime | None = None
    waiting_stage: str | None = None
    published_at: datetime | None = None
    llm_calls: int = 0
    created_at: datetime
    updated_at: datetime


class GoalEvent(BaseModel):
    """Audit event of a goal. ``(kind, ordinal)`` is unique per goal: re-appending is a no-op.

    Payloads carry identifiers, codes and counters only: never task texts, plans
    or anything a human did not already approve elsewhere."""

    model_config = ConfigDict(frozen=True)

    kind: GoalEventKind
    ordinal: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    actor_subject: str | None = None


class PersistedGoalEvent(GoalEvent):
    id: int
    created_at: datetime


class GoalCursor(BaseModel):
    model_config = ConfigDict(frozen=True)

    created_at: datetime
    id: UUID


class CreateGoalOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    goal: PersistedGoal
    created: bool


class GoalPatch(BaseModel):
    """Fields a reconciliation may change on the goal row; unset fields are left alone."""

    status: GoalStatus | None = None
    error: RunError | None = None
    plan_run_id: UUID | None = None
    plan_creating_since: datetime | None = None
    blocked_reasons: list[str] | None = None
    llm_calls_used: int | None = None
    route: str | None = None
    route_decision: dict[str, Any] | None = None


class TaskPatch(BaseModel):
    status: GoalTaskStatus | None = None
    attempt: int | None = None
    last_error_code: str | None = None
    run_id: UUID | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    merge_evidence: MergeEvidence | None = None
    scope_digest: str | None = None
    scope_paths: list[str] | None = None
    creating_since: datetime | None = None
    waiting_since: datetime | None = None
    waiting_stage: str | None = None
    published_at: datetime | None = None
    llm_calls: int | None = None


class ReserveOutcome(StrEnum):
    RESERVED = "reserved"  # task is now ``creating`` with its run id; counters charged
    CAPACITY = "capacity"  # a concurrency limit is full; nothing changed
    BUDGET = "budget"  # the goal's aggregate budget would be exceeded; nothing changed
    STALE = "stale"  # the task was no longer ``ready`` (someone else moved it)


class ReserveResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: ReserveOutcome
    reason: str | None = None  # capacity_goal | capacity_repository | capacity_global | budget_*
    task: PersistedGoalTask | None = None


class GoalRepository(Protocol):
    # -- goals ---------------------------------------------------------------------

    async def create_goal(
        self, goal: NewGoal, tasks: Sequence[NewGoalTask], *, now: datetime
    ) -> CreateGoalOutcome:
        """Insert the goal (status as given) and its initial tasks, plus ``goal_created``.
        With an ``idempotency_key`` the insert is atomic against the unique index over
        ``(project, owner_subject, key)``: a repeat by the same principal yields
        ``created=False`` and the existing goal (the caller compares the objective)."""
        ...

    async def get_goal(self, goal_id: UUID) -> PersistedGoal | None: ...

    async def find_goal_by_plan_run(self, run_id: UUID) -> PersistedGoal | None: ...

    async def list_goals(
        self,
        *,
        scope: AccessScope,
        status: GoalStatus | None = None,
        owner_subject: str | None = None,
        before: GoalCursor | None = None,
        limit: int,
    ) -> list[PersistedGoal]:
        """Newest first, keyset-paginated, always filtered by ``scope``."""
        ...

    async def update_goal(
        self,
        goal_id: UUID,
        patch: GoalPatch,
        *,
        lease_owner: str | None = None,
        expected_status: GoalStatus | None = None,
    ) -> PersistedGoal | None:
        """Conditional write: legal transition (``GOAL_TRANSITIONS``), current lease
        owner when given, expected status when given. ``None`` when refused."""
        ...

    # -- leases --------------------------------------------------------------------

    async def claim_goal(
        self, goal_id: UUID, *, owner: str, now: datetime, lease_seconds: float
    ) -> PersistedGoal | None:
        """Take the reconciliation lease if it is free or expired and the goal is not
        terminal; ``None`` otherwise."""
        ...

    async def claim_due_goals(
        self, *, owner: str, now: datetime, lease_seconds: float, limit: int
    ) -> list[PersistedGoal]:
        """Non-terminal goals whose ``next_reconcile_at`` passed and whose lease is free or
        expired, claimed atomically (``FOR UPDATE SKIP LOCKED`` in PostgreSQL)."""
        ...

    async def release_goal(
        self, goal_id: UUID, *, owner: str, next_reconcile_at: datetime | None
    ) -> bool: ...

    # -- tasks ---------------------------------------------------------------------

    async def add_tasks(
        self, goal_id: UUID, tasks: Sequence[NewGoalTask], *, now: datetime
    ) -> list[PersistedGoalTask]:
        """Materialise tasks; a repeated ``(goal, task_key)`` is ignored (idempotent)."""
        ...

    async def list_tasks(self, goal_id: UUID) -> list[PersistedGoalTask]: ...

    async def find_task_by_run(self, run_id: UUID) -> PersistedGoalTask | None: ...

    async def find_active_duplicate(
        self, repository_id: str, fingerprint: str, *, exclude_task_id: UUID | None = None
    ) -> PersistedGoalTask | None:
        """An active task (``ACTIVE_TASK_STATUSES``) with the same fingerprint, in any goal."""
        ...

    async def update_task(
        self,
        task_id: UUID,
        patch: TaskPatch,
        *,
        expected_status: GoalTaskStatus | None = None,
        now: datetime,
    ) -> PersistedGoalTask | None:
        """Conditional write: legal transition (``TASK_TRANSITIONS``) and, when given,
        the expected current status. ``None`` when refused."""
        ...

    async def reserve_child(
        self,
        goal_id: UUID,
        task_id: UUID,
        *,
        run_id: UUID,
        attempt: int,
        calls: int,
        usd: float,
        max_in_flight_per_goal: int,
        max_in_flight_per_repository: int,
        max_in_flight_global: int,
        now: datetime,
    ) -> ReserveResult:
        """Atomically: check the concurrency limits (goal, repository, platform) and the
        goal's aggregate budget, then move the task ``ready -> creating`` with ``run_id``
        and charge the reservation. Serialised across processes (advisory lock) so two
        coordinators can never exceed a limit by racing."""
        ...

    async def charge_planning(self, goal_id: UUID, *, calls: int, usd: float) -> bool:
        """Reserve the planning run's budget once (idempotent per goal)."""
        ...

    # -- audit ---------------------------------------------------------------------

    async def append_events(self, goal_id: UUID, events: Sequence[GoalEvent]) -> int:
        """Insert events ignoring duplicates (and enqueue their notifications in the same
        transaction); returns how many were new."""
        ...

    async def list_events(
        self, goal_id: UUID, *, after_id: int | None = None, limit: int | None = None
    ) -> list[PersistedGoalEvent]: ...


# -- integration probe --------------------------------------------------------------


class IntegrationError(Exception):
    """The platform could not establish whether a dependency is integrated.

    ``code`` is stable (``integration_unverifiable``, ``github_unreachable``,
    ``merge_commit_unknown``, ``mirror_sync_failed`` ...). The coordinator never
    treats an error as evidence: the dependent task simply keeps waiting.
    """

    def __init__(self, code: str, detail: str, *, retryable: bool = True) -> None:
        super().__init__(detail)
        self.code, self.detail, self.retryable = code, detail, retryable


class MergeProbe(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: Literal["open", "merged", "closed"]
    evidence: MergeEvidence | None = None


class IntegrationProbe(Protocol):
    """Platform-only (ADR 0012 §4.3). Agents never see it; the coordinator only reads it."""

    async def probe(self, repository_id: str, project_id: str, pr_number: int) -> MergeProbe:
        """State of the pull request on GitHub and, when merged, whether the merge commit
        is already present in the mirrored base branch (after a refresh)."""
        ...

    async def contains(
        self, repository_id: str, project_id: str, *, ancestor: str, descendant: str
    ) -> bool:
        """``ancestor`` is reachable from ``descendant`` in the mirror (both commit SHAs)."""
        ...

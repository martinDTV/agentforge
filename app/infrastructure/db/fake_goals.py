"""In-memory ``GoalRepository`` for the unit suite: same semantics as the PostgreSQL
implementation (conditional transitions, leases, atomic reservation, deduplicated
events with notifications), no SQL."""

import asyncio
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.contracts.goals import (
    ACTIVE_TASK_STATUSES,
    GOAL_TERMINAL_STATUSES,
    IN_FLIGHT_TASK_STATUSES,
    GoalEventKind,
    GoalLimits,
    GoalStatus,
    GoalTaskStatus,
    GoalUsage,
)
from app.goals.limits import Reservation, budget_refusal
from app.goals.ports import (
    CreateGoalOutcome,
    GoalCursor,
    GoalEvent,
    GoalPatch,
    NewGoal,
    NewGoalTask,
    PersistedGoal,
    PersistedGoalEvent,
    PersistedGoalTask,
    ReserveOutcome,
    ReserveResult,
    TaskPatch,
)
from app.goals.state import can_transition_goal, can_transition_task
from app.notifications import GoalContext, NotificationEnqueuer
from app.orchestration.exceptions import GoalAlreadyExistsError
from app.security import AccessScope

from .fake import InMemoryNotificationOutbox


class InMemoryGoalRepository:
    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        *,
        notifications: NotificationEnqueuer | None = None,
        outbox: InMemoryNotificationOutbox | None = None,
    ) -> None:
        self.clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._notifications = notifications
        self.outbox = outbox if outbox is not None else InMemoryNotificationOutbox(clock)
        self.goals: dict[UUID, PersistedGoal] = {}
        self.tasks: dict[UUID, PersistedGoalTask] = {}
        self.events: dict[UUID, list[PersistedGoalEvent]] = {}
        self.planning_charged: set[UUID] = set()
        self.fail_with: Exception | None = None
        self.calls: list[str] = []
        self._next_event_id = 1
        self._capacity = asyncio.Lock()

    # -- helpers ----------------------------------------------------------------------

    def _enter(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_with is not None:
            raise self.fail_with

    def _lease_free(self, goal: PersistedGoal, now: datetime) -> bool:
        return goal.lease_owner is None or (goal.lease_until is not None and goal.lease_until < now)

    def _enqueue(self, goal: PersistedGoal, events: Sequence[tuple[int | None, GoalEvent]]) -> None:
        if self._notifications is None or not events:
            return
        context = GoalContext(
            goal_id=goal.id,
            project_id=goal.project_id,
            repository_id=goal.repository_id,
            objective=goal.objective,
        )
        self.outbox.insert(self._notifications.goal_drafts(context, events, moment=self.clock()))

    def _task_from(self, goal_id: UUID, task: NewGoalTask, now: datetime) -> PersistedGoalTask:
        return PersistedGoalTask(
            id=uuid.uuid4(),
            goal_id=goal_id,
            task_key=task.task_key,
            title=task.title,
            task_text=task.task_text,
            dependencies=list(task.dependencies),
            ordinal=task.ordinal,
            repository_id=task.repository_id,
            fingerprint=task.fingerprint,
            status=task.status,
            created_at=now,
            updated_at=now,
        )

    # -- goals ------------------------------------------------------------------------

    async def create_goal(
        self, goal: NewGoal, tasks: Sequence[NewGoalTask], *, now: datetime
    ) -> CreateGoalOutcome:
        self._enter("create_goal")
        if goal.id in self.goals:
            raise GoalAlreadyExistsError(str(goal.id))
        if goal.idempotency_key is not None:
            for existing in self.goals.values():
                if (
                    existing.project_id == goal.project_id
                    and existing.owner_subject == goal.owner_subject
                    and existing.idempotency_key == goal.idempotency_key
                ):
                    return CreateGoalOutcome(goal=existing, created=False)
        row = PersistedGoal(
            id=goal.id,
            project_id=goal.project_id,
            repository_id=goal.repository_id,
            owner_subject=goal.owner_subject,
            objective=goal.objective,
            decomposition=goal.decomposition,
            dependency_policy=goal.dependency_policy,
            status=goal.status,
            limits=goal.limits,
            deadline=goal.deadline,
            route=goal.route,
            route_decision=goal.route_decision,
            next_reconcile_at=now,
            idempotency_key=goal.idempotency_key,
            created_at=now,
            updated_at=now,
        )
        self.goals[goal.id] = row
        for task in tasks:
            persisted = self._task_from(goal.id, task, now)
            self.tasks[persisted.id] = persisted
        await self.append_events(
            goal.id,
            [
                GoalEvent(
                    kind=GoalEventKind.GOAL_CREATED,
                    payload={
                        "decomposition": goal.decomposition.value,
                        "repository_id": goal.repository_id,
                        "tasks": len(tasks),
                        "initiated_by": goal.owner_subject,
                    },
                    actor_subject=goal.owner_subject,
                )
            ],
        )
        return CreateGoalOutcome(goal=row, created=True)

    async def get_goal(self, goal_id: UUID) -> PersistedGoal | None:
        self._enter("get_goal")
        return self.goals.get(goal_id)

    async def find_goal_by_plan_run(self, run_id: UUID) -> PersistedGoal | None:
        self._enter("find_goal_by_plan_run")
        return next((g for g in self.goals.values() if g.plan_run_id == run_id), None)

    async def list_goals(
        self,
        *,
        scope: AccessScope,
        status: GoalStatus | None = None,
        owner_subject: str | None = None,
        before: GoalCursor | None = None,
        limit: int,
    ) -> list[PersistedGoal]:
        self._enter("list_goals")
        if scope.is_empty:
            return []
        rows = [
            g
            for g in self.goals.values()
            if (scope.projects is None or g.project_id in scope.projects)
            and (scope.owner_only is None or g.owner_subject == scope.owner_only)
            and (owner_subject is None or g.owner_subject == owner_subject)
            and (status is None or g.status is status)
            and (before is None or (g.created_at, g.id.int) < (before.created_at, before.id.int))
        ]
        rows.sort(key=lambda g: (g.created_at, g.id.int), reverse=True)
        return rows[:limit]

    async def update_goal(
        self,
        goal_id: UUID,
        patch: GoalPatch,
        *,
        lease_owner: str | None = None,
        expected_status: GoalStatus | None = None,
    ) -> PersistedGoal | None:
        self._enter("update_goal")
        goal = self.goals.get(goal_id)
        if goal is None:
            return None
        if lease_owner is not None and goal.lease_owner != lease_owner:
            return None
        if expected_status is not None and goal.status is not expected_status:
            return None
        changed = patch.model_fields_set
        update: dict[str, object] = {"updated_at": self.clock()}
        if "status" in changed and patch.status is not None:
            if not can_transition_goal(goal.status, patch.status):
                return None
            update["status"] = patch.status
        for name in (
            "error",
            "plan_run_id",
            "plan_creating_since",
            "route",
            "route_decision",
        ):
            if name in changed:
                update[name] = getattr(patch, name)
        if "blocked_reasons" in changed:
            update["blocked_reasons"] = list(patch.blocked_reasons or [])
        if "llm_calls_used" in changed and patch.llm_calls_used is not None:
            update["usage"] = goal.usage.model_copy(update={"llm_calls_used": patch.llm_calls_used})
        updated = goal.model_copy(update=update)
        self.goals[goal_id] = updated
        return updated

    # -- leases -----------------------------------------------------------------------

    async def claim_goal(
        self, goal_id: UUID, *, owner: str, now: datetime, lease_seconds: float
    ) -> PersistedGoal | None:
        self._enter("claim_goal")
        goal = self.goals.get(goal_id)
        if goal is None or goal.status in GOAL_TERMINAL_STATUSES:
            return None
        if not self._lease_free(goal, now) and goal.lease_owner != owner:
            return None
        claimed = goal.model_copy(
            update={"lease_owner": owner, "lease_until": now + timedelta(seconds=lease_seconds)}
        )
        self.goals[goal_id] = claimed
        return claimed

    async def claim_due_goals(
        self, *, owner: str, now: datetime, lease_seconds: float, limit: int
    ) -> list[PersistedGoal]:
        self._enter("claim_due_goals")
        due = sorted(
            (
                g
                for g in self.goals.values()
                if g.status not in GOAL_TERMINAL_STATUSES
                and g.next_reconcile_at is not None
                and g.next_reconcile_at <= now
                and self._lease_free(g, now)
            ),
            key=lambda g: (g.next_reconcile_at or now, g.id.int),
        )[:limit]
        claimed = []
        for goal in due:
            row = goal.model_copy(
                update={
                    "lease_owner": owner,
                    "lease_until": now + timedelta(seconds=lease_seconds),
                }
            )
            self.goals[goal.id] = row
            claimed.append(row)
        return claimed

    async def release_goal(
        self, goal_id: UUID, *, owner: str, next_reconcile_at: datetime | None
    ) -> bool:
        self._enter("release_goal")
        goal = self.goals.get(goal_id)
        if goal is None or goal.lease_owner != owner:
            return False
        self.goals[goal_id] = goal.model_copy(
            update={
                "lease_owner": None,
                "lease_until": None,
                "next_reconcile_at": next_reconcile_at,
            }
        )
        return True

    # -- tasks ------------------------------------------------------------------------

    async def add_tasks(
        self, goal_id: UUID, tasks: Sequence[NewGoalTask], *, now: datetime
    ) -> list[PersistedGoalTask]:
        self._enter("add_tasks")
        existing = {t.task_key for t in self.tasks.values() if t.goal_id == goal_id}
        for task in tasks:
            if task.task_key in existing:
                continue
            persisted = self._task_from(goal_id, task, now)
            self.tasks[persisted.id] = persisted
        return await self.list_tasks(goal_id)

    async def list_tasks(self, goal_id: UUID) -> list[PersistedGoalTask]:
        self._enter("list_tasks")
        rows = [t for t in self.tasks.values() if t.goal_id == goal_id]
        rows.sort(key=lambda t: (t.ordinal, t.task_key))
        return rows

    async def find_task_by_run(self, run_id: UUID) -> PersistedGoalTask | None:
        self._enter("find_task_by_run")
        return next((t for t in self.tasks.values() if t.run_id == run_id), None)

    async def find_active_duplicate(
        self, repository_id: str, fingerprint: str, *, exclude_task_id: UUID | None = None
    ) -> PersistedGoalTask | None:
        self._enter("find_active_duplicate")
        candidates = sorted(
            (
                t
                for t in self.tasks.values()
                if t.repository_id == repository_id
                and t.fingerprint == fingerprint
                and t.status in ACTIVE_TASK_STATUSES
                and t.id != exclude_task_id
            ),
            key=lambda t: t.created_at,
        )
        return candidates[0] if candidates else None

    async def update_task(
        self,
        task_id: UUID,
        patch: TaskPatch,
        *,
        expected_status: GoalTaskStatus | None = None,
        now: datetime,
    ) -> PersistedGoalTask | None:
        self._enter("update_task")
        task = self.tasks.get(task_id)
        if task is None:
            return None
        if expected_status is not None and task.status is not expected_status:
            return None
        changed = patch.model_fields_set
        update: dict[str, object] = {"updated_at": now}
        if "status" in changed and patch.status is not None:
            if not can_transition_task(task.status, patch.status):
                return None
            update["status"] = patch.status
        for name in changed - {"status"}:
            update[name] = getattr(patch, name)
        updated = task.model_copy(update=update)
        self.tasks[task_id] = updated
        return updated

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
        self._enter("reserve_child")
        async with self._capacity:
            goal = self.goals.get(goal_id)
            task = self.tasks.get(task_id)
            if (
                goal is None
                or task is None
                or task.goal_id != goal_id
                or task.status is not GoalTaskStatus.READY
            ):
                return ReserveResult(outcome=ReserveOutcome.STALE)
            in_flight = [t for t in self.tasks.values() if t.status in IN_FLIGHT_TASK_STATUSES]
            if sum(1 for t in in_flight if t.goal_id == goal_id) >= max_in_flight_per_goal:
                return ReserveResult(outcome=ReserveOutcome.CAPACITY, reason="capacity_goal")
            if (
                sum(1 for t in in_flight if t.repository_id == task.repository_id)
                >= max_in_flight_per_repository
            ):
                return ReserveResult(outcome=ReserveOutcome.CAPACITY, reason="capacity_repository")
            if len(in_flight) >= max_in_flight_global:
                return ReserveResult(outcome=ReserveOutcome.CAPACITY, reason="capacity_global")
            refusal = budget_refusal(
                GoalLimits.model_validate(goal.limits.model_dump()),
                goal.usage,
                Reservation(calls=calls, usd=usd),
            )
            if refusal is not None:
                return ReserveResult(outcome=ReserveOutcome.BUDGET, reason=refusal)
            reserved = task.model_copy(
                update={
                    "status": GoalTaskStatus.CREATING,
                    "run_id": run_id,
                    "attempt": attempt,
                    "creating_since": now,
                    "last_error_code": None,
                    "updated_at": now,
                }
            )
            self.tasks[task_id] = reserved
            self.goals[goal_id] = goal.model_copy(
                update={
                    "usage": GoalUsage(
                        tasks_created=goal.usage.tasks_created + 1,
                        reserved_model_calls=goal.usage.reserved_model_calls + calls,
                        reserved_usd=goal.usage.reserved_usd + usd,
                        llm_calls_used=goal.usage.llm_calls_used,
                    ),
                    "updated_at": now,
                }
            )
            return ReserveResult(outcome=ReserveOutcome.RESERVED, task=reserved)

    async def charge_planning(self, goal_id: UUID, *, calls: int, usd: float) -> bool:
        self._enter("charge_planning")
        goal = self.goals.get(goal_id)
        if goal is None or goal_id in self.planning_charged:
            return False
        self.planning_charged.add(goal_id)
        self.goals[goal_id] = goal.model_copy(
            update={
                "usage": goal.usage.model_copy(
                    update={
                        "reserved_model_calls": goal.usage.reserved_model_calls + calls,
                        "reserved_usd": goal.usage.reserved_usd + usd,
                    }
                )
            }
        )
        return True

    # -- audit ------------------------------------------------------------------------

    async def append_events(self, goal_id: UUID, events: Sequence[GoalEvent]) -> int:
        self._enter("append_events")
        stored = self.events.setdefault(goal_id, [])
        seen = {(e.kind, e.ordinal) for e in stored}
        new: list[tuple[int | None, GoalEvent]] = []
        for event in events:
            if (event.kind, event.ordinal) in seen:
                continue
            seen.add((event.kind, event.ordinal))
            stored.append(
                PersistedGoalEvent(
                    id=self._next_event_id, created_at=self.clock(), **event.model_dump()
                )
            )
            new.append((self._next_event_id, event))
            self._next_event_id += 1
        goal = self.goals.get(goal_id)
        if goal is not None and new:
            self._enqueue(goal, new)
        return len(new)

    async def list_events(
        self, goal_id: UUID, *, after_id: int | None = None, limit: int | None = None
    ) -> list[PersistedGoalEvent]:
        self._enter("list_events")
        rows = [e for e in self.events.get(goal_id, []) if after_id is None or e.id > after_id]
        return rows if limit is None else rows[:limit]

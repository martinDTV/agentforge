"""PostgreSQL persistence of goals, goal tasks and goal events (Fase 8, ADR 0012).

Every method is one short transaction. Transitions are checked against the
domain tables before writing; the reconciliation lease fences goal updates;
``reserve_child`` serialises capacity and budget decisions across processes
with a transaction-scoped advisory lock, so two coordinators can never exceed
a limit by racing.
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.execution import RunError
from app.contracts.goals import (
    ACTIVE_TASK_STATUSES,
    GOAL_TERMINAL_STATUSES,
    IN_FLIGHT_TASK_STATUSES,
    DependencyPolicy,
    GoalDecomposition,
    GoalEventKind,
    GoalLimits,
    GoalStatus,
    GoalTaskStatus,
    GoalUsage,
    MergeEvidence,
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

from .errors import as_unavailable
from .models import GoalEventRecord, GoalRecord, GoalTaskRecord
from .notifications import insert_outbox

# One lock for every capacity decision of the platform (pg_advisory_xact_lock takes a bigint).
CAPACITY_LOCK_KEY = 80_100_001


def _to_goal(row: GoalRecord) -> PersistedGoal:
    return PersistedGoal(
        id=row.id,
        project_id=row.project_id,
        repository_id=row.repository_id,
        owner_subject=row.owner_subject,
        objective=row.objective,
        decomposition=GoalDecomposition(row.decomposition),
        dependency_policy=DependencyPolicy(row.dependency_policy),
        status=GoalStatus(row.status),
        limits=GoalLimits.model_validate(row.limits),
        deadline=row.deadline,
        error=(
            RunError(code=row.error_code, message=row.error_message or "")
            if row.error_code is not None
            else None
        ),
        route=row.route,
        route_decision=row.route_decision,
        plan_run_id=row.plan_run_id,
        plan_creating_since=row.plan_creating_since,
        usage=GoalUsage(
            tasks_created=row.tasks_created,
            reserved_model_calls=row.reserved_model_calls,
            reserved_usd=row.reserved_usd,
            llm_calls_used=row.llm_calls_used,
        ),
        blocked_reasons=list(row.blocked_reasons or []),
        next_reconcile_at=row.next_reconcile_at,
        lease_owner=row.lease_owner,
        lease_until=row.lease_until,
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_task(row: GoalTaskRecord) -> PersistedGoalTask:
    return PersistedGoalTask(
        id=row.id,
        goal_id=row.goal_id,
        task_key=row.task_key,
        title=row.title,
        task_text=row.task_text,
        dependencies=list(row.dependencies or []),
        ordinal=row.ordinal,
        repository_id=row.repository_id,
        fingerprint=row.fingerprint,
        status=GoalTaskStatus(row.status),
        attempt=row.attempt,
        last_error_code=row.last_error_code,
        run_id=row.run_id,
        base_sha=row.base_sha,
        head_sha=row.head_sha,
        pr_number=row.pr_number,
        pr_url=row.pr_url,
        merge_evidence=(
            MergeEvidence.model_validate(row.merge_evidence) if row.merge_evidence else None
        ),
        scope_digest=row.scope_digest,
        scope_paths=list(row.scope_paths) if row.scope_paths is not None else None,
        creating_since=row.creating_since,
        waiting_since=row.waiting_since,
        waiting_stage=row.waiting_stage,
        published_at=row.published_at,
        llm_calls=row.llm_calls,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_event(row: GoalEventRecord) -> PersistedGoalEvent:
    return PersistedGoalEvent(
        id=row.id,
        kind=GoalEventKind(row.kind),
        ordinal=row.ordinal,
        payload=dict(row.payload or {}),
        actor_subject=row.actor_subject,
        created_at=row.created_at,
    )


def _task_values(goal_id: UUID, task: NewGoalTask) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "goal_id": goal_id,
        "task_key": task.task_key,
        "title": task.title,
        "task_text": task.task_text,
        "dependencies": list(task.dependencies),
        "ordinal": task.ordinal,
        "repository_id": task.repository_id,
        "fingerprint": task.fingerprint,
        "status": task.status.value,
    }


def _lease_free(now: datetime) -> Any:
    """Free or expired *by the coordinator's clock*, so every lease decision of a goal uses
    the same time source as the deadlines and reminders it drives."""
    return or_(GoalRecord.lease_owner.is_(None), GoalRecord.lease_until < now)


class SqlAlchemyGoalRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        notifications: NotificationEnqueuer | None = None,
    ) -> None:
        self._sessions = sessions
        self._notifications = notifications

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self._sessions() as session, session.begin():
                yield session
        except SQLAlchemyError as exc:
            unavailable = as_unavailable(exc)
            if unavailable is not None:
                raise unavailable from exc
            raise

    async def _enqueue(
        self,
        session: AsyncSession,
        goal: GoalRecord,
        events: Sequence[tuple[int | None, GoalEvent]],
        *,
        now: datetime,
    ) -> None:
        if self._notifications is None or not self._notifications.active or not events:
            return
        context = GoalContext(
            goal_id=goal.id,
            project_id=goal.project_id,
            repository_id=goal.repository_id,
            objective=goal.objective,
        )
        await insert_outbox(session, self._notifications.goal_drafts(context, events, moment=now))

    # -- goals -----------------------------------------------------------------------

    async def create_goal(
        self, goal: NewGoal, tasks: Sequence[NewGoalTask], *, now: datetime
    ) -> CreateGoalOutcome:
        async with self._tx() as session:
            stmt = pg_insert(GoalRecord).values(
                id=goal.id,
                project_id=goal.project_id,
                repository_id=goal.repository_id,
                owner_subject=goal.owner_subject,
                objective=goal.objective,
                decomposition=goal.decomposition.value,
                dependency_policy=goal.dependency_policy.value,
                status=goal.status.value,
                limits=goal.limits.model_dump(mode="json"),
                route=goal.route,
                route_decision=goal.route_decision,
                deadline=goal.deadline,
                next_reconcile_at=now,
                idempotency_key=goal.idempotency_key,
            )
            if goal.idempotency_key is not None:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["project_id", "owner_subject", "idempotency_key"],
                    index_where=text("idempotency_key IS NOT NULL"),
                )
            try:
                result = await session.execute(stmt.returning(GoalRecord.id))
                inserted = result.scalar_one_or_none()
            except IntegrityError as exc:
                raise GoalAlreadyExistsError(str(goal.id)) from exc
            if inserted is None:
                existing = (
                    await session.execute(
                        select(GoalRecord).where(
                            GoalRecord.project_id == goal.project_id,
                            GoalRecord.owner_subject == goal.owner_subject,
                            GoalRecord.idempotency_key == goal.idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:  # pragma: no cover - only if the row vanished meanwhile
                    raise GoalAlreadyExistsError(str(goal.id))
                return CreateGoalOutcome(goal=_to_goal(existing), created=False)
            if tasks:
                await session.execute(
                    pg_insert(GoalTaskRecord).values([_task_values(goal.id, t) for t in tasks])
                )
            record = await session.get(GoalRecord, goal.id)
            assert record is not None
            event = GoalEventRecord(
                goal_id=goal.id,
                kind=GoalEventKind.GOAL_CREATED.value,
                ordinal=0,
                payload={
                    "decomposition": goal.decomposition.value,
                    "repository_id": goal.repository_id,
                    "tasks": len(tasks),
                    "initiated_by": goal.owner_subject,
                },
                actor_subject=goal.owner_subject,
            )
            session.add(event)
            await session.flush()
            await self._enqueue(
                session,
                record,
                [
                    (
                        int(event.id),
                        GoalEvent(
                            kind=GoalEventKind.GOAL_CREATED,
                            payload=dict(event.payload),
                            actor_subject=goal.owner_subject,
                        ),
                    )
                ],
                now=now,
            )
            await session.refresh(record)
            return CreateGoalOutcome(goal=_to_goal(record), created=True)

    async def get_goal(self, goal_id: UUID) -> PersistedGoal | None:
        async with self._tx() as session:
            row = await session.get(GoalRecord, goal_id)
            return _to_goal(row) if row is not None else None

    async def find_goal_by_plan_run(self, run_id: UUID) -> PersistedGoal | None:
        async with self._tx() as session:
            row = (
                await session.execute(select(GoalRecord).where(GoalRecord.plan_run_id == run_id))
            ).scalar_one_or_none()
            return _to_goal(row) if row is not None else None

    async def list_goals(
        self,
        *,
        scope: AccessScope,
        status: GoalStatus | None = None,
        owner_subject: str | None = None,
        before: GoalCursor | None = None,
        limit: int,
    ) -> list[PersistedGoal]:
        if scope.is_empty:
            return []
        conditions: list[Any] = []
        if scope.projects is not None:
            conditions.append(GoalRecord.project_id.in_(sorted(scope.projects)))
        if scope.owner_only is not None:
            conditions.append(GoalRecord.owner_subject == scope.owner_only)
        if owner_subject is not None:
            conditions.append(GoalRecord.owner_subject == owner_subject)
        if status is not None:
            conditions.append(GoalRecord.status == status.value)
        if before is not None:
            conditions.append(
                tuple_(GoalRecord.created_at, GoalRecord.id) < tuple_(before.created_at, before.id)
            )
        stmt = (
            select(GoalRecord)
            .where(*conditions)
            .order_by(GoalRecord.created_at.desc(), GoalRecord.id.desc())
            .limit(limit)
        )
        async with self._tx() as session:
            return [_to_goal(row) for row in (await session.execute(stmt)).scalars().all()]

    async def update_goal(
        self,
        goal_id: UUID,
        patch: GoalPatch,
        *,
        lease_owner: str | None = None,
        expected_status: GoalStatus | None = None,
    ) -> PersistedGoal | None:
        async with self._tx() as session:
            row = (
                await session.execute(
                    select(GoalRecord).where(GoalRecord.id == goal_id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if lease_owner is not None and row.lease_owner != lease_owner:
                return None
            current = GoalStatus(row.status)
            if expected_status is not None and current is not expected_status:
                return None
            changed = patch.model_fields_set
            if "status" in changed and patch.status is not None:
                if not can_transition_goal(current, patch.status):
                    return None
                row.status = patch.status.value
            if "error" in changed:
                row.error_code = patch.error.code if patch.error else None
                row.error_message = patch.error.message if patch.error else None
            if "plan_run_id" in changed:
                row.plan_run_id = patch.plan_run_id
            if "plan_creating_since" in changed:
                row.plan_creating_since = patch.plan_creating_since
            if "blocked_reasons" in changed:
                row.blocked_reasons = list(patch.blocked_reasons or [])
            if "llm_calls_used" in changed and patch.llm_calls_used is not None:
                row.llm_calls_used = patch.llm_calls_used
            if "route" in changed:
                row.route = patch.route
            if "route_decision" in changed:
                row.route_decision = patch.route_decision
            await session.flush()
            await session.refresh(row)
            return _to_goal(row)

    # -- leases ----------------------------------------------------------------------

    async def claim_goal(
        self, goal_id: UUID, *, owner: str, now: datetime, lease_seconds: float
    ) -> PersistedGoal | None:
        async with self._tx() as session:
            row = (
                await session.execute(
                    update(GoalRecord)
                    .where(
                        GoalRecord.id == goal_id,
                        GoalRecord.status.notin_([s.value for s in GOAL_TERMINAL_STATUSES]),
                        or_(_lease_free(now), GoalRecord.lease_owner == owner),
                    )
                    .values(lease_owner=owner, lease_until=now + timedelta(seconds=lease_seconds))
                    .returning(GoalRecord)
                )
            ).scalar_one_or_none()
            return _to_goal(row) if row is not None else None

    async def claim_due_goals(
        self, *, owner: str, now: datetime, lease_seconds: float, limit: int
    ) -> list[PersistedGoal]:
        candidates = (
            select(GoalRecord.id)
            .where(
                GoalRecord.status.notin_([s.value for s in GOAL_TERMINAL_STATUSES]),
                GoalRecord.next_reconcile_at.is_not(None),
                GoalRecord.next_reconcile_at <= now,
                _lease_free(now),
            )
            .order_by(GoalRecord.next_reconcile_at, GoalRecord.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("due_goals")
        )
        async with self._tx() as session:
            rows = (
                (
                    await session.execute(
                        update(GoalRecord)
                        .where(GoalRecord.id == candidates.c.id)
                        .values(
                            lease_owner=owner,
                            lease_until=now + timedelta(seconds=lease_seconds),
                        )
                        .returning(GoalRecord)
                    )
                )
                .scalars()
                .all()
            )
            return [_to_goal(row) for row in rows]

    async def release_goal(
        self, goal_id: UUID, *, owner: str, next_reconcile_at: datetime | None
    ) -> bool:
        async with self._tx() as session:
            released = (
                await session.execute(
                    update(GoalRecord)
                    .where(GoalRecord.id == goal_id, GoalRecord.lease_owner == owner)
                    .values(lease_owner=None, lease_until=None, next_reconcile_at=next_reconcile_at)
                    .returning(GoalRecord.id)
                )
            ).scalar_one_or_none()
            return released is not None

    # -- tasks -----------------------------------------------------------------------

    async def add_tasks(
        self, goal_id: UUID, tasks: Sequence[NewGoalTask], *, now: datetime
    ) -> list[PersistedGoalTask]:
        if tasks:
            async with self._tx() as session:
                await session.execute(
                    pg_insert(GoalTaskRecord)
                    .values([_task_values(goal_id, t) for t in tasks])
                    .on_conflict_do_nothing(constraint="uq_goal_tasks_goal_id_task_key")
                )
        return await self.list_tasks(goal_id)

    async def list_tasks(self, goal_id: UUID) -> list[PersistedGoalTask]:
        async with self._tx() as session:
            rows = (
                await session.execute(
                    select(GoalTaskRecord)
                    .where(GoalTaskRecord.goal_id == goal_id)
                    .order_by(GoalTaskRecord.ordinal, GoalTaskRecord.task_key)
                )
            ).scalars()
            return [_to_task(row) for row in rows]

    async def find_task_by_run(self, run_id: UUID) -> PersistedGoalTask | None:
        async with self._tx() as session:
            row = (
                await session.execute(select(GoalTaskRecord).where(GoalTaskRecord.run_id == run_id))
            ).scalar_one_or_none()
            return _to_task(row) if row is not None else None

    async def find_active_duplicate(
        self, repository_id: str, fingerprint: str, *, exclude_task_id: UUID | None = None
    ) -> PersistedGoalTask | None:
        conditions: list[Any] = [
            GoalTaskRecord.repository_id == repository_id,
            GoalTaskRecord.fingerprint == fingerprint,
            GoalTaskRecord.status.in_([s.value for s in ACTIVE_TASK_STATUSES]),
        ]
        if exclude_task_id is not None:
            conditions.append(GoalTaskRecord.id != exclude_task_id)
        async with self._tx() as session:
            row = (
                (
                    await session.execute(
                        select(GoalTaskRecord)
                        .where(*conditions)
                        .order_by(GoalTaskRecord.created_at)
                    )
                )
                .scalars()
                .first()
            )
            return _to_task(row) if row is not None else None

    @staticmethod
    def _apply_task_patch(row: GoalTaskRecord, patch: TaskPatch, *, now: datetime) -> bool:
        changed = patch.model_fields_set
        if "status" in changed and patch.status is not None:
            if not can_transition_task(GoalTaskStatus(row.status), patch.status):
                return False
            row.status = patch.status.value
        simple = (
            "attempt",
            "last_error_code",
            "run_id",
            "base_sha",
            "head_sha",
            "pr_number",
            "pr_url",
            "scope_digest",
            "creating_since",
            "waiting_since",
            "waiting_stage",
            "published_at",
            "llm_calls",
        )
        for name in simple:
            if name in changed:
                setattr(row, name, getattr(patch, name))
        if "merge_evidence" in changed:
            row.merge_evidence = (
                patch.merge_evidence.model_dump(mode="json") if patch.merge_evidence else None
            )
        if "scope_paths" in changed:
            row.scope_paths = list(patch.scope_paths) if patch.scope_paths is not None else None
        row.updated_at = now
        return True

    async def update_task(
        self,
        task_id: UUID,
        patch: TaskPatch,
        *,
        expected_status: GoalTaskStatus | None = None,
        now: datetime,
    ) -> PersistedGoalTask | None:
        async with self._tx() as session:
            row = (
                await session.execute(
                    select(GoalTaskRecord).where(GoalTaskRecord.id == task_id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if expected_status is not None and GoalTaskStatus(row.status) is not expected_status:
                return None
            if not self._apply_task_patch(row, patch, now=now):
                return None
            await session.flush()
            await session.refresh(row)
            return _to_task(row)

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
        in_flight = [s.value for s in IN_FLIGHT_TASK_STATUSES]
        async with self._tx() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": CAPACITY_LOCK_KEY}
            )
            goal = (
                await session.execute(
                    select(GoalRecord).where(GoalRecord.id == goal_id).with_for_update()
                )
            ).scalar_one_or_none()
            task = (
                await session.execute(
                    select(GoalTaskRecord).where(GoalTaskRecord.id == task_id).with_for_update()
                )
            ).scalar_one_or_none()
            if (
                goal is None
                or task is None
                or task.goal_id != goal_id
                or GoalTaskStatus(task.status) is not GoalTaskStatus.READY
            ):
                return ReserveResult(outcome=ReserveOutcome.STALE)

            async def count(*conditions: Any) -> int:
                value = await session.scalar(
                    select(func.count())
                    .select_from(GoalTaskRecord)
                    .where(GoalTaskRecord.status.in_(in_flight), *conditions)
                )
                return int(value or 0)

            if await count(GoalTaskRecord.goal_id == goal_id) >= max_in_flight_per_goal:
                return ReserveResult(outcome=ReserveOutcome.CAPACITY, reason="capacity_goal")
            if (
                await count(GoalTaskRecord.repository_id == task.repository_id)
                >= max_in_flight_per_repository
            ):
                return ReserveResult(outcome=ReserveOutcome.CAPACITY, reason="capacity_repository")
            if await count() >= max_in_flight_global:
                return ReserveResult(outcome=ReserveOutcome.CAPACITY, reason="capacity_global")
            usage = GoalUsage(
                tasks_created=goal.tasks_created,
                reserved_model_calls=goal.reserved_model_calls,
                reserved_usd=goal.reserved_usd,
                llm_calls_used=goal.llm_calls_used,
            )
            refusal = budget_refusal(
                GoalLimits.model_validate(goal.limits), usage, Reservation(calls=calls, usd=usd)
            )
            if refusal is not None:
                return ReserveResult(outcome=ReserveOutcome.BUDGET, reason=refusal)
            task.status = GoalTaskStatus.CREATING.value
            task.run_id = run_id
            task.attempt = attempt
            task.creating_since = now
            task.last_error_code = None
            task.updated_at = now
            goal.tasks_created += 1
            goal.reserved_model_calls += calls
            goal.reserved_usd += usd
            await session.flush()
            await session.refresh(task)
            return ReserveResult(outcome=ReserveOutcome.RESERVED, task=_to_task(task))

    async def charge_planning(self, goal_id: UUID, *, calls: int, usd: float) -> bool:
        async with self._tx() as session:
            charged = (
                await session.execute(
                    update(GoalRecord)
                    .where(GoalRecord.id == goal_id, GoalRecord.planning_charged.is_(False))
                    .values(
                        planning_charged=True,
                        reserved_model_calls=GoalRecord.reserved_model_calls + calls,
                        reserved_usd=GoalRecord.reserved_usd + usd,
                    )
                    .returning(GoalRecord.id)
                )
            ).scalar_one_or_none()
            return charged is not None

    # -- audit -----------------------------------------------------------------------

    async def append_events(self, goal_id: UUID, events: Sequence[GoalEvent]) -> int:
        if not events:
            return 0
        async with self._tx() as session:
            stmt = (
                pg_insert(GoalEventRecord)
                .values(
                    [
                        {
                            "goal_id": goal_id,
                            "kind": event.kind.value,
                            "ordinal": event.ordinal,
                            "payload": event.payload,
                            "actor_subject": event.actor_subject,
                        }
                        for event in events
                    ]
                )
                .on_conflict_do_nothing(constraint="uq_goal_events_goal_id_kind_ordinal")
                .returning(GoalEventRecord.id, GoalEventRecord.kind, GoalEventRecord.ordinal)
            )
            inserted = (await session.execute(stmt)).all()
            if inserted:
                goal = await session.get(GoalRecord, goal_id)
                if goal is not None:
                    by_key = {(e.kind.value, e.ordinal): e for e in events}
                    moment = (
                        await session.scalar(select(func.now()))
                    ) or datetime.now().astimezone()
                    await self._enqueue(
                        session,
                        goal,
                        [
                            (int(row_id), by_key[(str(kind), int(ordinal))])
                            for row_id, kind, ordinal in inserted
                            if (str(kind), int(ordinal)) in by_key
                        ],
                        now=moment,
                    )
            return len(inserted)

    async def list_events(
        self, goal_id: UUID, *, after_id: int | None = None, limit: int | None = None
    ) -> list[PersistedGoalEvent]:
        stmt = select(GoalEventRecord).where(GoalEventRecord.goal_id == goal_id)
        if after_id is not None:
            stmt = stmt.where(GoalEventRecord.id > after_id)
        stmt = stmt.order_by(GoalEventRecord.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._tx() as session:
            return [_to_event(row) for row in (await session.execute(stmt)).scalars().all()]


__all__ = ["CAPACITY_LOCK_KEY", "SqlAlchemyGoalRepository"]

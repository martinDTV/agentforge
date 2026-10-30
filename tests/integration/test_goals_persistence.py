"""Migration 0010 and the PostgreSQL goal repository (Fase 8, ADR 0012): schema, lossy
downgrade refusal, conditional transitions, leases with ``SKIP LOCKED``, atomic reservation
under concurrency (advisory lock), the active-fingerprint index and goal notifications."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.contracts.goals import (
    DependencyPolicy,
    GoalDecomposition,
    GoalEventKind,
    GoalLimits,
    GoalStatus,
    GoalTaskStatus,
)
from app.goals import GoalEvent, GoalPatch, NewGoal, NewGoalTask, ReserveOutcome, TaskPatch
from app.infrastructure.db import (
    Database,
    SqlAlchemyGoalRepository,
    SqlAlchemyNotificationOutbox,
)
from app.infrastructure.db.schema import expected_head_revision
from app.infrastructure.notifications import FakeEmailSender
from app.notifications import (
    NotificationEnqueuer,
    NotificationKind,
    NotificationPolicy,
    NotificationWorker,
    OutboxStatus,
)
from app.security import AccessScope
from tests.integration.conftest import downgrade, migrate

pytestmark = pytest.mark.anyio
T0 = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
G, T = GoalStatus, GoalTaskStatus


def test_0010_adds_goals_tables_and_the_outbox_goal_reference(empty_database_url: str):
    assert expected_head_revision() == "0011"
    migrate(empty_database_url, "0009")
    engine = create_engine(empty_database_url)
    try:
        migrate(empty_database_url, "0010")
        inspector = inspect(engine)
        assert {"goals", "goal_tasks", "goal_events"} <= set(inspector.get_table_names())
        columns = {c["name"]: c for c in inspector.get_columns("notification_outbox")}
        assert columns["run_id"]["nullable"] and "goal_id" in columns
        goal_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO goals (id, project_id, repository_id, owner_subject, objective,"
                    " decomposition, dependency_policy, status, limits, deadline)"
                    " VALUES (:id, 'p', 'r', 'o', 'obj', 'single', 'after_merged', 'active',"
                    " '{}'::jsonb, now())"
                ),
                {"id": goal_id},
            )
        # A notification must belong to a run or a goal.
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO notification_outbox (kind, recipient, subject, text_body,"
                    " dedup_key, message_id) VALUES ('k', 'r', 's', 't', 'd1', 'm')"
                )
            )
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO goals (id, project_id, repository_id, owner_subject, objective,"
                    " decomposition, dependency_policy, status, limits, deadline)"
                    " VALUES (:id, 'p', 'r', 'o', 'obj', 'single', 'after_merged', 'weird',"
                    " '{}'::jsonb, now())"
                ),
                {"id": uuid.uuid4()},
            )
        # Two identical *active* tasks in one repository are impossible.
        with engine.begin() as conn:
            for status in ("pending", "merged"):
                conn.execute(
                    text(
                        "INSERT INTO goal_tasks (id, goal_id, task_key, title, task_text, ordinal,"
                        " repository_id, fingerprint, status) VALUES (:id, :goal, :key, 't', 'x',"
                        " 0, 'r', 'f', :status)"
                    ),
                    {"id": uuid.uuid4(), "goal": goal_id, "key": status, "status": status},
                )
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO goal_tasks (id, goal_id, task_key, title, task_text, ordinal,"
                    " repository_id, fingerprint, status) VALUES (:id, :goal, 'again', 't', 'x',"
                    " 1, 'r', 'f', 'ready')"
                ),
                {"id": uuid.uuid4(), "goal": goal_id},
            )
        # Downgrade refuses while a goal-only notification exists, then succeeds without it.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO notification_outbox (goal_id, kind, recipient, subject,"
                    " text_body, dedup_key, message_id)"
                    " VALUES (:goal, 'goal_completed', 'r', 's', 't', 'd2', 'm2')"
                ),
                {"goal": goal_id},
            )
        with pytest.raises(RuntimeError, match="downgrade rechazado"):
            downgrade(empty_database_url, "0009")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM notification_outbox WHERE run_id IS NULL"))
        downgrade(empty_database_url, "0009")
        names = set(inspect(engine).get_table_names())
        assert not {"goals", "goal_tasks", "goal_events"} & names
        assert "goal_id" not in {
            c["name"] for c in inspect(engine).get_columns("notification_outbox")
        }
        migrate(empty_database_url, "head")
    finally:
        engine.dispose()


def _goal(**overrides: object) -> NewGoal:
    base = dict(
        id=uuid.uuid4(),
        project_id="sandbox-gh",
        repository_id="sandbox-demo",
        owner_subject="olivia",
        objective="Añade validación de correo con pruebas",
        decomposition=GoalDecomposition.PLAN,
        dependency_policy=DependencyPolicy.AFTER_MERGED,
        status=G.PLANNING,
        limits=GoalLimits(max_total_model_calls=14),
        deadline=T0 + timedelta(days=7),
    )
    base.update(overrides)
    return NewGoal(**base)  # type: ignore[arg-type]


def _task(
    key: str,
    ordinal: int,
    *,
    deps: list[str] | None = None,
    fingerprint: str | None = None,
) -> NewGoalTask:
    return NewGoalTask(
        task_key=key,
        title=f"Tarea {key}",
        task_text=f"texto {key}",
        dependencies=deps or [],
        ordinal=ordinal,
        repository_id="sandbox-demo",
        fingerprint=fingerprint or uuid.uuid4().hex * 2,
        status=T.READY if not deps else T.PENDING,
    )


async def test_goal_rows_tasks_events_and_leases_round_trip(
    fresh_migrated_database_url: str,
):
    database = Database(fresh_migrated_database_url)
    try:
        repo = SqlAlchemyGoalRepository(database.session_factory)
        goal = _goal(idempotency_key="k-1")
        created = await repo.create_goal(goal, [_task("a", 0), _task("b", 1, deps=["a"])], now=T0)
        assert created.created and created.goal.status is G.PLANNING
        assert created.goal.limits.max_total_model_calls == 14
        replay = await repo.create_goal(goal.model_copy(update={"id": uuid.uuid4()}), [], now=T0)
        assert not replay.created and replay.goal.id == goal.id
        tasks = await repo.list_tasks(goal.id)
        assert [t.task_key for t in tasks] == ["a", "b"]
        assert tasks[0].status is T.READY and tasks[1].status is T.PENDING
        events = await repo.list_events(goal.id)
        assert [e.kind for e in events] == [GoalEventKind.GOAL_CREATED]
        # Conditional transitions: terminal states and illegal moves are refused.
        assert await repo.update_task(tasks[1].id, TaskPatch(status=T.RUNNING), now=T0) is None
        ready = await repo.update_task(tasks[1].id, TaskPatch(status=T.READY), now=T0)
        assert ready is not None and ready.status is T.READY
        assert (
            await repo.update_goal(goal.id, GoalPatch(status=G.ACTIVE), lease_owner="nobody")
            is None
        )
        # Leases: claim, re-claim by the same owner, refuse others until released or expired.
        claimed = await repo.claim_goal(goal.id, owner="p1", now=T0, lease_seconds=60)
        assert claimed is not None and claimed.lease_owner == "p1"
        assert await repo.claim_goal(goal.id, owner="p2", now=T0, lease_seconds=60) is None
        assert await repo.claim_goal(goal.id, owner="p1", now=T0, lease_seconds=60) is not None
        active = await repo.update_goal(goal.id, GoalPatch(status=G.ACTIVE), lease_owner="p1")
        assert active is not None and active.status is G.ACTIVE
        assert await repo.release_goal(goal.id, owner="p2", next_reconcile_at=None) is False
        assert await repo.release_goal(goal.id, owner="p1", next_reconcile_at=T0) is True
        expired_claim = await repo.claim_goal(
            goal.id, owner="p2", now=T0 + timedelta(minutes=5), lease_seconds=60
        )
        assert expired_claim is not None
        # Events are unique per (goal, kind, ordinal); the replay inserts nothing.
        added = await repo.append_events(
            goal.id,
            [GoalEvent(kind=GoalEventKind.TASK_READY, ordinal=0, payload={"task_key": "a"})],
        )
        assert added == 1
        assert await repo.append_events(goal.id, [GoalEvent(kind=GoalEventKind.TASK_READY)]) == 0
        # Listing with the authorization scope and keyset cursor.
        listed = await repo.list_goals(
            scope=AccessScope(projects=frozenset({"sandbox-gh"})), limit=10
        )
        assert goal.id in {g.id for g in listed}
        assert await repo.list_goals(scope=AccessScope(projects=frozenset()), limit=10) == []
        assert await repo.find_goal_by_plan_run(uuid.uuid4()) is None
        assert (await repo.find_task_by_run(uuid.uuid4())) is None
        # Finish: a terminal goal refuses every further transition.
        done = await repo.update_goal(goal.id, GoalPatch(status=G.CANCELLED))
        assert done is not None and done.status is G.CANCELLED
        assert await repo.update_goal(goal.id, GoalPatch(status=G.ACTIVE)) is None
        assert await repo.claim_goal(goal.id, owner="p3", now=T0, lease_seconds=60) is None
    finally:
        await database.dispose()


async def test_due_goals_are_claimed_once_across_sessions(fresh_migrated_database_url: str):
    database = Database(fresh_migrated_database_url)
    try:
        repo = SqlAlchemyGoalRepository(database.session_factory)
        ids = []
        for _ in range(3):
            goal = _goal(status=G.ACTIVE)
            await repo.create_goal(goal, [_task("a", 0)], now=T0)
            ids.append(goal.id)
        a, b = await asyncio.gather(
            repo.claim_due_goals(owner="p1", now=T0, lease_seconds=60, limit=10),
            repo.claim_due_goals(owner="p2", now=T0, lease_seconds=60, limit=10),
        )
        mine = {g.id for g in a if g.id in ids}
        theirs = {g.id for g in b if g.id in ids}
        assert mine | theirs == set(ids) and not (mine & theirs)
        again = await repo.claim_due_goals(owner="p3", now=T0, lease_seconds=60, limit=10)
        assert not ({g.id for g in again} & set(ids))
        for goal_id in ids:
            owner = "p1" if goal_id in mine else "p2"
            await repo.release_goal(goal_id, owner=owner, next_reconcile_at=None)
    finally:
        await database.dispose()


async def test_reservations_are_atomic_against_capacity_and_budget(
    fresh_migrated_database_url: str,
):
    database = Database(fresh_migrated_database_url)
    try:
        repo = SqlAlchemyGoalRepository(database.session_factory)
        goal = _goal(status=G.ACTIVE, limits=GoalLimits(max_total_model_calls=9))
        await repo.create_goal(goal, [_task(k, i) for i, k in enumerate("abcd")], now=T0)
        tasks = await repo.list_tasks(goal.id)
        assert await repo.charge_planning(goal.id, calls=1, usd=0.5) is True
        assert await repo.charge_planning(goal.id, calls=1, usd=0.5) is False  # once

        async def reserve(task_id: uuid.UUID, *, per_goal: int = 5) -> ReserveOutcome:
            result = await repo.reserve_child(
                goal.id,
                task_id,
                run_id=uuid.uuid4(),
                attempt=1,
                calls=4,
                usd=1.0,
                max_in_flight_per_goal=per_goal,
                max_in_flight_per_repository=5,
                max_in_flight_global=5,
                now=T0,
            )
            return result.outcome

        # Budget 9 - 1 (planning) = 8 = exactly two children of 4 calls, however many race.
        outcomes = await asyncio.gather(*[reserve(t.id) for t in tasks for _ in range(2)])
        assert outcomes.count(ReserveOutcome.RESERVED) == 2
        assert ReserveOutcome.BUDGET in outcomes and ReserveOutcome.STALE in outcomes
        refreshed = await repo.get_goal(goal.id)
        assert refreshed is not None
        assert refreshed.usage.tasks_created == 2 and refreshed.usage.reserved_model_calls == 9
        creating = [t for t in await repo.list_tasks(goal.id) if t.status is T.CREATING]
        assert len(creating) == 2 and all(t.run_id is not None and t.attempt == 1 for t in creating)
        # Capacity per goal: with two in flight, a third reservation is refused for capacity
        # before the budget is even consulted.
        ready = next(t for t in await repo.list_tasks(goal.id) if t.status is T.READY)
        result = await repo.reserve_child(
            goal.id,
            ready.id,
            run_id=uuid.uuid4(),
            attempt=1,
            calls=0,
            usd=0.0,
            max_in_flight_per_goal=2,
            max_in_flight_per_repository=5,
            max_in_flight_global=5,
            now=T0,
        )
        assert result.outcome is ReserveOutcome.CAPACITY and result.reason == "capacity_goal"
        # The active-fingerprint index: a duplicate task is refused by the database itself.
        other = _goal(status=G.ACTIVE)
        await repo.create_goal(other, [], now=T0)
        with pytest.raises(IntegrityError):  # same repository and fingerprint, still active
            await repo.add_tasks(
                other.id, [_task("a", 0, fingerprint=tasks[0].fingerprint)], now=T0
            )
        assert await repo.find_active_duplicate("sandbox-demo", tasks[0].fingerprint) is not None
    finally:
        await database.dispose()


async def test_goal_notifications_are_enqueued_with_the_event_and_delivered(
    fresh_migrated_database_url: str,
):
    database = Database(fresh_migrated_database_url)
    try:
        enqueuer = NotificationEnqueuer(
            NotificationPolicy(), recipients=["ops@example.com"], level="verbose"
        )
        repo = SqlAlchemyGoalRepository(database.session_factory, notifications=enqueuer)
        outbox = SqlAlchemyNotificationOutbox(database.session_factory)
        goal = _goal(status=G.ACTIVE, decomposition=GoalDecomposition.SINGLE)
        await repo.create_goal(goal, [_task("single", 0)], now=T0)
        await repo.append_events(
            goal.id,
            [
                GoalEvent(
                    kind=GoalEventKind.NEEDS_HUMAN,
                    ordinal=5,
                    payload={"reason": "merge_pending", "task_key": "single", "pr_number": 1},
                ),
                GoalEvent(kind=GoalEventKind.CAPACITY_WAIT, payload={}),  # no mail
            ],
        )
        items = [i for i in await outbox.list_items(limit=200) if i.goal_id == goal.id]
        assert {i.kind for i in items} == {
            NotificationKind.GOAL_CREATED,
            NotificationKind.GOAL_NEEDS_HUMAN,
        }
        assert all(i.run_id is None for i in items)
        sender = FakeEmailSender()
        worker = NotificationWorker(outbox, sender, clock=lambda: datetime.now(UTC))
        await worker.run_once()
        sent = [
            i
            for i in await outbox.list_items(statuses=[OutboxStatus.SENT], limit=200)
            if i.goal_id == goal.id
        ]
        assert len(sent) == 2
        assert any(f"agentforge.goal.{goal.id}.thread" in m.references[0] for m in sender.sent)
    finally:
        await database.dispose()

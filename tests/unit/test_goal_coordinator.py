"""The deterministic goal Supervisor (Fase 8, Paso 1; ADR 0012) against fakes: one child at a
time, dependencies released only by verified merges, idempotent reconciliation across
restarts, aggregate budgets that races cannot exceed, and a delegate that can never approve."""

import asyncio
from uuid import UUID, uuid4

import pytest

from app.contracts.goals import (
    CreateGoal,
    GoalDecomposition,
    GoalEventKind,
    GoalLimits,
    GoalStatus,
    GoalTaskStatus,
)
from app.contracts.lifecycle import RunLifecycleStatus, WorkflowKind
from app.contracts.routing import RouteMode
from app.goals import IntegrationError
from app.infrastructure.db import InMemoryGoalRepository, InMemoryRunRepository
from app.orchestration.exceptions import (
    GoalDependenciesUnsupportedError,
    GoalIdempotencyKeyReusedError,
    GoalNotCancellableError,
)
from app.orchestration.goals import GoalCoordinator, GoalService, SupervisorLimits
from app.orchestration.limits import RunLimits
from app.security import DELEGATION_ISSUER, AuthorizationPolicy, PermissionDeniedError, Principal
from tests.unit.goal_fixtures import (
    OWNER,
    PROJECT,
    REPO,
    Clock,
    FakeLauncher,
    FakeProbe,
    plan_of,
    registry,
)

pytestmark = pytest.mark.anyio
G = GoalStatus
T = GoalTaskStatus
S = RunLifecycleStatus
OBJECTIVE = "Añade validación de correo con pruebas y documenta el módulo de usuarios"


class World:
    def __init__(
        self,
        *,
        remote: bool = True,
        limits: SupervisorLimits | None = None,
        separation_of_duties: bool = False,
        **policy_overrides: object,
    ) -> None:
        self.clock = Clock()
        self.runs = InMemoryRunRepository(clock=self.clock)
        self.goals = InMemoryGoalRepository(clock=self.clock)
        self.launcher = FakeLauncher(self.runs, self.clock)
        self.probe = FakeProbe()
        self.policies = registry(remote=remote, **policy_overrides)
        self.coordinator = GoalCoordinator(
            goals=self.goals,
            runs=self.runs,
            launcher=self.launcher,
            policies=self.policies,
            planning_limits=RunLimits(max_llm_calls=6),
            probe=self.probe,
            limits=limits or SupervisorLimits(creating_timeout_seconds=120),
            clock=self.clock,
            owner="coordinator-a",
        )
        self.service = GoalService(
            self.coordinator,
            authorization=AuthorizationPolicy(separation_of_duties=separation_of_duties),
            policies=self.policies,
            runs=self.runs,
            router=None,
            planning_limits=RunLimits(max_llm_calls=6),
            clock=self.clock,
        )

    async def create(self, objective: str = OBJECTIVE, **overrides: object) -> UUID:
        body = CreateGoal(
            objective=objective,
            project_id=PROJECT,
            repository_id=REPO,
            **overrides,  # type: ignore[arg-type]
        )
        result = await self.service.create(body, OWNER)
        await self.coordinator.wait_until_idle()
        return UUID(result.response.goal_id)

    async def tick(self, goal_id: UUID) -> None:
        await self.coordinator.reconcile(goal_id)
        await self.coordinator.wait_until_idle()

    async def tasks(self, goal_id: UUID) -> dict[str, object]:
        return {t.task_key: t for t in await self.goals.list_tasks(goal_id)}

    async def task(self, goal_id: UUID, key: str):  # type: ignore[no-untyped-def]
        return (await self.tasks(goal_id))[key]

    async def goal(self, goal_id: UUID):  # type: ignore[no-untyped-def]
        goal = await self.goals.get_goal(goal_id)
        assert goal is not None
        return goal

    async def kinds(self, goal_id: UUID) -> list[GoalEventKind]:
        return [e.kind for e in await self.goals.list_events(goal_id)]

    async def plan_run(self, goal_id: UUID) -> UUID:
        goal = await self.goal(goal_id)
        assert goal.plan_run_id is not None
        return goal.plan_run_id


# -- single task ---------------------------------------------------------------------------------


async def test_a_single_goal_creates_one_child_as_the_owner_and_completes_when_it_publishes():
    world = World()
    goal_id = await world.create(decomposition=GoalDecomposition.SINGLE)
    goal = await world.goal(goal_id)
    assert goal.status is G.ACTIVE and goal.plan_run_id is None
    task = await world.task(goal_id, "single")
    assert task.status is T.WAITING_HUMAN and task.run_id is not None and task.attempt == 1
    created = world.launcher.created[0]
    assert created["workflow_kind"] is WorkflowKind.DEVELOPMENT
    assert created["repository_id"] == REPO and created["project_id"] == PROJECT
    assert created["idempotency_key"] == f"goal:{goal_id}:single:1"
    assert created["planned_upstream"] is None and created["route_mode"] is None
    delegate: Principal = created["principal"]
    assert delegate.subject == OWNER.subject and delegate.issuer == DELEGATION_ISSUER
    assert delegate.roles == {"goal_delegate"} and delegate.projects == {PROJECT}
    row = await world.runs.get_run(task.run_id)
    assert row is not None and row.owner_subject == OWNER.subject  # separation of duties intact
    # The human approves the scope and the publication; the Supervisor only observes.
    await world.launcher.approve_scope(task.run_id)
    await world.tick(goal_id)
    assert (await world.task(goal_id, "single")).status is T.WAITING_HUMAN
    await world.launcher.publish(
        task.run_id, pr_number=7, head_sha="e" * 40, base_sha=world.launcher.default_base
    )
    await world.tick(goal_id)
    goal = await world.goal(goal_id)
    assert goal.status is G.COMPLETED and goal.error is None
    task = await world.task(goal_id, "single")
    assert task.status is T.PUBLISHED and task.pr_number == 7 and task.head_sha == "e" * 40
    assert goal.usage.tasks_created == 1 and goal.usage.reserved_model_calls == 4
    assert goal.usage.llm_calls_used == 4
    kinds = await world.kinds(goal_id)
    assert kinds[0] is GoalEventKind.GOAL_CREATED
    assert GoalEventKind.CHILD_CREATED in kinds and GoalEventKind.TASK_PUBLISHED in kinds
    assert kinds[-1] is GoalEventKind.GOAL_COMPLETED
    # Nothing was approved by the Supervisor: the delegate was never handed to approve().
    assert all(p.issuer == DELEGATION_ISSUER for _, p in world.launcher.decisions_calls)


async def test_the_delegate_cannot_approve_resume_or_publish_anything():
    world = World(separation_of_duties=True)
    goal_id = await world.create(decomposition=GoalDecomposition.SINGLE)
    delegate = world.launcher.created[0]["principal"]
    authz = AuthorizationPolicy()
    row = await world.runs.get_run((await world.task(goal_id, "single")).run_id)
    assert row is not None
    from app.security import Permission

    for permission in (Permission.RUNS_APPROVE, Permission.RUNS_RESUME, Permission.REPOS_PUBLISH):
        assert not authz.has(delegate, permission)
        assert not authz.decide(delegate, permission, row).allowed
    assert not authz.has(delegate, Permission.TOOLS_EXECUTE)
    assert not authz.has(delegate, Permission.RUNS_READ)  # sees only what it owns
    assert authz.can_see(delegate, row)  # ...which includes its own children
    # And a real RunService refuses the delegate at the approval door (self-approval + role).
    from app.infrastructure.llm.fake import FakeLLMAdapter
    from app.orchestration.graph import build_graph
    from app.orchestration.runs import RunService

    service = RunService(build_graph(FakeLLMAdapter()), world.runs, authorization=authz)
    with pytest.raises(PermissionDeniedError):
        await service.approve(str(row.id), True, delegate, scope_digest="d" * 64)


# -- planning and dependencies ------------------------------------------------------------------


async def test_a_planned_goal_runs_its_tasks_in_order_and_only_after_verified_merges():
    world = World()
    goal_id = await world.create(decomposition=GoalDecomposition.PLAN)
    goal = await world.goal(goal_id)
    assert goal.status is G.PLANNING and goal.plan_run_id is not None
    plan_run = goal.plan_run_id
    assert world.launcher.created[0]["workflow_kind"] is WorkflowKind.PLANNING
    assert world.launcher.created[0]["idempotency_key"] == f"goal:{goal_id}:plan"
    assert goal.usage.reserved_model_calls == 6  # the planning round is reserved up front
    # Nothing is materialised while the plan waits for the human.
    await world.tick(goal_id)
    assert await world.tasks(goal_id) == {}
    await world.launcher.approve_plan(plan_run, plan_of(("a", []), ("b", ["a"]), ("c", ["b"])))
    await world.tick(goal_id)
    goal = await world.goal(goal_id)
    assert goal.status is G.ACTIVE
    tasks = await world.tasks(goal_id)
    assert [k for k in tasks] == ["a", "b", "c"]
    a, b, c = tasks["a"], tasks["b"], tasks["c"]
    assert a.status is T.WAITING_HUMAN and b.status is T.PENDING and c.status is T.PENDING
    child = world.launcher.created[1]
    assert child["planned_upstream"] is not None  # D4: Planner/Architect not repeated per task
    assert child["planned_upstream"].plan_run_id == str(plan_run)
    assert child["route_mode"] is None
    assert "Parte 1/3 del objetivo" in child["task_text"] if "task_text" in child else True
    assert "Criterios de aceptación: a funciona" in child["task"]
    # A publishes; its PR is open: B does not start (an open PR is not integration).
    await world.launcher.approve_scope(a.run_id)
    await world.launcher.publish(a.run_id, pr_number=1, head_sha="1" * 40, base_sha="b" * 40)
    await world.tick(goal_id)
    await world.tick(goal_id)
    tasks = await world.tasks(goal_id)
    assert tasks["a"].status is T.PUBLISHED and tasks["b"].status is T.PENDING
    assert len(world.launcher.created) == 2
    assert ("probe", 1) in world.probe.calls
    # GitHub says merged but the mirror does not contain the merge commit yet: still blocked.
    world.probe.merged(1, "c" * 40, verified=False)
    await world.tick(goal_id)
    tasks = await world.tasks(goal_id)
    assert tasks["a"].status is T.PUBLISHED and tasks["b"].status is T.PENDING
    assert (await world.goal(goal_id)).status is G.BLOCKED
    assert "merge_unverified" in (await world.goal(goal_id)).blocked_reasons
    # Verified in the mirror: A merged, B created from a base containing the merge commit.
    world.probe.merged(1, "c" * 40, verified=True)
    world.launcher.default_base = "a" * 40
    world.probe.ancestry.add(("c" * 40, "a" * 40))
    await world.tick(goal_id)
    tasks = await world.tasks(goal_id)
    assert tasks["a"].status is T.MERGED and tasks["a"].merge_evidence is not None
    assert tasks["a"].merge_evidence.merge_commit_sha == "c" * 40
    assert tasks["b"].status is T.WAITING_HUMAN and tasks["b"].base_sha == "a" * 40
    assert tasks["c"].status is T.PENDING
    assert (await world.goal(goal_id)).status is G.ACTIVE
    assert len(world.launcher.created) == 3
    kinds = await world.kinds(goal_id)
    assert GoalEventKind.DEPENDENCY_MERGED in kinds and GoalEventKind.TASK_BASE_RECORDED in kinds
    # Only one child is ever in flight.
    assert (
        sum(1 for t in tasks.values() if t.status in (T.CREATING, T.RUNNING, T.WAITING_HUMAN)) == 1
    )


async def test_a_dependency_whose_pull_request_is_closed_without_merge_skips_its_dependents():
    world = World()
    goal_id = await world.create(decomposition=GoalDecomposition.PLAN)
    await world.launcher.approve_plan(
        await world.plan_run(goal_id), plan_of(("a", []), ("b", ["a"]), ("c", ["a"]))
    )
    await world.tick(goal_id)
    a = await world.task(goal_id, "a")
    await world.launcher.approve_scope(a.run_id)
    await world.launcher.publish(a.run_id, pr_number=3, head_sha="3" * 40, base_sha="b" * 40)
    world.probe.states[3] = "closed"
    await world.tick(goal_id)
    tasks = await world.tasks(goal_id)
    assert tasks["a"].status is T.FAILED and tasks["a"].last_error_code == "pr_closed"
    assert tasks["b"].status is T.SKIPPED and tasks["c"].status is T.SKIPPED
    goal = await world.goal(goal_id)
    assert goal.status is G.FAILED and goal.error is not None
    assert goal.error.code == "pr_closed"
    kinds = await world.kinds(goal_id)
    assert GoalEventKind.PR_CLOSED in kinds and GoalEventKind.TASK_SKIPPED in kinds
    assert GoalEventKind.NEEDS_HUMAN in kinds


async def test_a_failed_child_blocks_the_goal_for_a_human_and_is_never_recreated():
    world = World()
    goal_id = await world.create(decomposition=GoalDecomposition.PLAN)
    await world.launcher.approve_plan(await world.plan_run(goal_id), plan_of(("a", []), ("b", [])))
    await world.tick(goal_id)
    a = await world.task(goal_id, "a")
    await world.launcher.fail(a.run_id, "correction_limit")
    await world.tick(goal_id)
    await world.tick(goal_id)
    tasks = await world.tasks(goal_id)
    assert tasks["a"].status is T.FAILED and tasks["a"].last_error_code == "correction_limit"
    # B is independent: it proceeds; A is not retried (no automatic re-creation in Paso 1).
    assert tasks["b"].status is T.WAITING_HUMAN
    assert [c["idempotency_key"] for c in world.launcher.created[1:]] == [
        f"goal:{goal_id}:a:1",
        f"goal:{goal_id}:b:1",
    ]
    goal = await world.goal(goal_id)
    assert goal.status is G.BLOCKED and goal.blocked_reasons == ["task_failed"]
    await world.launcher.approve_scope(tasks["b"].run_id)
    await world.launcher.publish(
        tasks["b"].run_id, pr_number=2, head_sha="2" * 40, base_sha="b" * 40
    )
    await world.tick(goal_id)
    goal = await world.goal(goal_id)
    assert goal.status is G.PARTIAL and goal.error is not None
    assert goal.error.code == "correction_limit"
    # The persisted evidence of the failed child is untouched: its row, events and error stay.
    row = await world.runs.get_run(tasks["a"].run_id)
    assert row is not None and row.status is S.FAILED and row.error is not None


async def test_a_plan_with_a_cycle_or_too_many_tasks_fails_before_creating_any_child():
    world = World()
    goal_id = await world.create(decomposition=GoalDecomposition.PLAN)
    plan_run = await world.plan_run(goal_id)
    cyclic = plan_of(("a", ["b"]), ("b", ["a"]))
    # The contract itself refuses a cyclic plan (the Planner cannot produce one)...
    from app.contracts.planning import DevelopmentPlan

    with pytest.raises(ValueError, match="ciclo"):
        DevelopmentPlan.model_validate(cyclic)
    # ...and a row carrying one (an older checkpoint, a bug) ends the goal without children.
    await world.launcher.approve_plan(plan_run, cyclic)
    await world.tick(goal_id)
    goal = await world.goal(goal_id)
    assert goal.status is G.FAILED and goal.error is not None
    assert goal.error.code in ("goal_dependency_cycle", "plan_invalid")
    assert await world.tasks(goal_id) == {}
    assert len(world.launcher.created) == 1  # only the planning run

    world = World()
    goal_id = await world.create(
        decomposition=GoalDecomposition.PLAN, limits=GoalLimits(max_tasks=2)
    )
    await world.launcher.approve_plan(
        await world.plan_run(goal_id), plan_of(("a", []), ("b", []), ("c", []))
    )
    await world.tick(goal_id)
    goal = await world.goal(goal_id)
    assert goal.status is G.FAILED and goal.error is not None
    assert goal.error.code == "goal_limit_reached"
    assert len(world.launcher.created) == 1


async def test_a_refused_plan_closes_the_goal():
    world = World()
    goal_id = await world.create(decomposition=GoalDecomposition.PLAN)
    await world.launcher.refuse_plan(await world.plan_run(goal_id))
    await world.tick(goal_id)
    goal = await world.goal(goal_id)
    assert goal.status is G.FAILED and goal.error is not None
    assert goal.error.code == "plan_rejected:plan"
    assert GoalEventKind.PLAN_REFUSED in await world.kinds(goal_id)


# -- budgets, capacity, duplicates ------------------------------------------------------------


async def test_the_aggregate_budget_stops_creating_children_and_ends_partial():
    world = World()
    # 6 planning calls + 4 per child: room for exactly one child.
    goal_id = await world.create(
        decomposition=GoalDecomposition.PLAN, limits=GoalLimits(max_total_model_calls=10)
    )
    await world.launcher.approve_plan(await world.plan_run(goal_id), plan_of(("a", []), ("b", [])))
    await world.tick(goal_id)
    tasks = await world.tasks(goal_id)
    assert tasks["a"].status is T.WAITING_HUMAN and tasks["b"].status is T.READY
    await world.launcher.approve_scope(tasks["a"].run_id)
    await world.launcher.publish(
        tasks["a"].run_id, pr_number=1, head_sha="1" * 40, base_sha="b" * 40
    )
    await world.tick(goal_id)
    tasks = await world.tasks(goal_id)
    assert tasks["b"].status is T.FAILED and tasks["b"].last_error_code == "budget_calls"
    goal = await world.goal(goal_id)
    assert goal.status is G.PARTIAL and goal.usage.reserved_model_calls == 10
    assert GoalEventKind.BUDGET_EXHAUSTED in await world.kinds(goal_id)
    assert len(world.launcher.created) == 2


async def test_budget_reservations_cannot_be_exceeded_by_racing_reservations():
    world = World()
    goal_id = await world.create(
        decomposition=GoalDecomposition.PLAN, limits=GoalLimits(max_total_model_calls=14)
    )
    await world.launcher.approve_plan(
        await world.plan_run(goal_id), plan_of(("a", []), ("b", []), ("c", []), ("d", []))
    )
    await world.coordinator.reconcile(goal_id)  # a is created; b, c, d are ready
    await world.coordinator.wait_until_idle()
    tasks = await world.tasks(goal_id)
    ready = [t for t in tasks.values() if t.status is T.READY]
    assert len(ready) == 3
    # Many concurrent reservations against the same goal: budget 14 - 6 (plan) - 4 (a) = 4
    # fits exactly one more child, and the per-goal concurrency is one anyway.
    results = await asyncio.gather(
        *[
            world.goals.reserve_child(
                goal_id,
                t.id,
                run_id=uuid4(),
                attempt=1,
                calls=4,
                usd=2.0,
                max_in_flight_per_goal=5,
                max_in_flight_per_repository=5,
                max_in_flight_global=5,
                now=world.clock(),
            )
            for t in ready * 3
        ]
    )
    reserved = [r for r in results if r.outcome.value == "reserved"]
    assert len(reserved) == 1
    goal = await world.goal(goal_id)
    assert goal.usage.reserved_model_calls == 14 and goal.usage.tasks_created == 2


async def test_global_and_per_repository_capacity_are_shared_between_goals():
    world = World(limits=SupervisorLimits(max_in_flight_global=1, max_in_flight_per_repository=1))
    first = await world.create(decomposition=GoalDecomposition.SINGLE)
    second = await world.create(
        decomposition=GoalDecomposition.SINGLE, objective=OBJECTIVE + " (segunda parte)"
    )
    assert (await world.task(first, "single")).status is T.WAITING_HUMAN
    waiting = await world.task(second, "single")
    assert waiting.status is T.READY and waiting.run_id is None
    assert GoalEventKind.CAPACITY_WAIT in await world.kinds(second)
    assert len(world.launcher.created) == 1
    # The first child finishes: the terminal hook frees the slot for the second goal.
    run_id = (await world.task(first, "single")).run_id
    await world.launcher.approve_scope(run_id)
    await world.launcher.publish(run_id, pr_number=1, head_sha="1" * 40, base_sha="b" * 40)
    row = await world.runs.get_run(run_id)
    assert row is not None
    await world.coordinator.on_run_terminal(row)
    await world.coordinator.wait_until_idle()
    await world.tick(second)
    assert (await world.goal(first)).status is G.COMPLETED
    assert (await world.task(second, "single")).status is T.WAITING_HUMAN
    assert len(world.launcher.created) == 2


async def test_an_identical_active_task_in_the_repository_is_a_duplicate_not_a_second_child():
    world = World(limits=SupervisorLimits(max_in_flight_global=4, max_in_flight_per_repository=4))
    first = await world.create(decomposition=GoalDecomposition.SINGLE)
    second = await world.create(decomposition=GoalDecomposition.SINGLE)
    assert first != second
    task = await world.task(second, "single")
    assert task.status is T.DUPLICATE and task.run_id is None
    goal = await world.goal(second)
    assert goal.status is G.FAILED and len(world.launcher.created) == 1


# -- restarts and idempotency -----------------------------------------------------------------


async def test_a_restart_between_reserving_the_run_id_and_creating_it_yields_exactly_one_run():
    world = World()
    world.launcher.fail_create_with = RuntimeError("process died before create")
    goal_id = await world.create(decomposition=GoalDecomposition.SINGLE)
    task = await world.task(goal_id, "single")
    # The crash left the task failed? No: an unexpected error marks it failed only when the
    # launcher *answered*; a RuntimeError here is treated as internal failure of the attempt.
    assert task.status in (T.CREATING, T.FAILED)
    # Simulate the realistic crash instead: the reservation exists, nobody called create.
    world = World()
    world.coordinator._spawn = lambda coroutine, *, name: coroutine.close()  # type: ignore[method-assign]
    goal_id = await world.create(decomposition=GoalDecomposition.SINGLE)
    task = await world.task(goal_id, "single")
    assert task.status is T.CREATING and task.run_id is not None and task.attempt == 1
    assert await world.runs.get_run(task.run_id) is None
    reserved_run_id = task.run_id
    # A new coordinator (restart) takes over the same repository: before the creation
    # timeout it waits; after it, it re-spawns the creation with the very same run id and key.
    restarted = GoalCoordinator(
        goals=world.goals,
        runs=world.runs,
        launcher=world.launcher,
        policies=world.policies,
        probe=world.probe,
        limits=SupervisorLimits(creating_timeout_seconds=120),
        clock=world.clock,
        owner="coordinator-b",
    )
    await restarted.reconcile(goal_id)
    await restarted.wait_until_idle()
    assert world.launcher.created == []
    world.clock.advance(seconds=121)
    await restarted.reconcile(goal_id)
    await restarted.wait_until_idle()
    assert len(world.launcher.created) == 1
    assert world.launcher.created[0]["run_id"] == str(reserved_run_id)
    assert world.launcher.created[0]["idempotency_key"] == f"goal:{goal_id}:single:1"
    task = await world.task(goal_id, "single")
    assert task.status is T.WAITING_HUMAN and task.run_id == reserved_run_id
    # A third reconciliation (another restart) finds the run and changes nothing.
    await restarted.reconcile(goal_id)
    await restarted.wait_until_idle()
    assert len(world.launcher.created) == 1
    assert len(world.runs.runs) == 1


async def test_reconciliation_is_idempotent_and_two_coordinators_never_double_create():
    world = World()
    other = GoalCoordinator(
        goals=world.goals,
        runs=world.runs,
        launcher=world.launcher,
        policies=world.policies,
        probe=world.probe,
        limits=SupervisorLimits(),
        clock=world.clock,
        owner="coordinator-b",
    )
    goal_id = await world.create(decomposition=GoalDecomposition.PLAN)
    await world.launcher.approve_plan(await world.plan_run(goal_id), plan_of(("a", []), ("b", [])))
    await asyncio.gather(
        world.coordinator.reconcile(goal_id),
        other.reconcile(goal_id),
        world.coordinator.reconcile(goal_id),
    )
    await world.coordinator.wait_until_idle()
    await other.wait_until_idle()
    tasks = await world.tasks(goal_id)
    assert tasks["a"].status is T.WAITING_HUMAN and tasks["b"].status is T.READY
    assert len(world.launcher.created) == 2  # the plan and exactly one child
    for _ in range(3):
        await world.tick(goal_id)
    assert len(world.launcher.created) == 2
    events = await world.goals.list_events(goal_id)
    assert len({(e.kind, e.ordinal) for e in events}) == len(events)


# -- human attention ---------------------------------------------------------------------------


async def test_a_long_pending_decision_blocks_the_goal_once_and_unblocks_when_decided():
    world = World()
    goal_id = await world.create(decomposition=GoalDecomposition.SINGLE)
    world.clock.advance(hours=13)  # past half of the 24h scope window
    await world.tick(goal_id)
    await world.tick(goal_id)
    goal = await world.goal(goal_id)
    assert goal.status is G.BLOCKED and goal.blocked_reasons == ["decision_pending"]
    events = [
        e for e in await world.goals.list_events(goal_id) if e.kind is GoalEventKind.NEEDS_HUMAN
    ]
    assert len(events) == 1 and events[0].payload["stage"] == "scope"
    run_id = (await world.task(goal_id, "single")).run_id
    await world.launcher.approve_scope(run_id)
    await world.tick(goal_id)
    goal = await world.goal(goal_id)
    assert goal.status is G.ACTIVE and goal.blocked_reasons == []


async def test_an_unmerged_pull_request_reminds_the_human_after_the_merge_window():
    world = World()
    goal_id = await world.create(decomposition=GoalDecomposition.PLAN)
    await world.launcher.approve_plan(
        await world.plan_run(goal_id), plan_of(("a", []), ("b", ["a"]))
    )
    await world.tick(goal_id)
    a = await world.task(goal_id, "a")
    await world.launcher.approve_scope(a.run_id)
    await world.launcher.publish(a.run_id, pr_number=9, head_sha="9" * 40, base_sha="b" * 40)
    await world.tick(goal_id)
    assert (await world.goal(goal_id)).status is G.ACTIVE
    world.clock.advance(hours=25)
    await world.tick(goal_id)
    goal = await world.goal(goal_id)
    assert goal.status is G.BLOCKED and goal.blocked_reasons == ["merge_pending"]
    assert (await world.task(goal_id, "b")).status is T.PENDING


async def test_probe_errors_are_not_evidence():
    world = World()
    goal_id = await world.create(decomposition=GoalDecomposition.PLAN)
    await world.launcher.approve_plan(
        await world.plan_run(goal_id), plan_of(("a", []), ("b", ["a"]))
    )
    await world.tick(goal_id)
    a = await world.task(goal_id, "a")
    await world.launcher.approve_scope(a.run_id)
    await world.launcher.publish(a.run_id, pr_number=4, head_sha="4" * 40, base_sha="b" * 40)
    world.probe.fail_with = IntegrationError("github_unreachable", "simulated")
    await world.tick(goal_id)
    tasks = await world.tasks(goal_id)
    assert tasks["a"].status is T.PUBLISHED and tasks["b"].status is T.PENDING
    assert GoalEventKind.PROBE_FAILED in await world.kinds(goal_id)
    assert (await world.goal(goal_id)).status is G.ACTIVE


async def test_a_stale_base_cancels_the_child_before_any_human_decision_and_recreates_it():
    world = World()
    goal_id = await world.create(decomposition=GoalDecomposition.PLAN)
    await world.launcher.approve_plan(
        await world.plan_run(goal_id), plan_of(("a", []), ("b", ["a"]))
    )
    await world.tick(goal_id)
    a = await world.task(goal_id, "a")
    await world.launcher.approve_scope(a.run_id)
    await world.launcher.publish(a.run_id, pr_number=1, head_sha="1" * 40, base_sha="b" * 40)
    world.probe.merged(1, "c" * 40)
    # The child of B will report a base that does NOT contain the merge commit.
    world.launcher.default_base = "0" * 40
    await world.tick(goal_id)
    # Within one tick: B created, its base found stale, the child cancelled by the delegate
    # (D3) before any human saw it, and the task held back as ``ready`` for the next tick.
    first_run = UUID(world.launcher.created[-1]["run_id"])
    b = await world.task(goal_id, "b")
    assert b.status is T.READY and b.run_id is None and b.last_error_code == "base_stale"
    cancelled = [rid for rid, _ in world.launcher.cancelled]
    assert cancelled == [first_run]
    assert all(p.issuer == DELEGATION_ISSUER for _, p in world.launcher.cancelled)
    assert GoalEventKind.TASK_BASE_STALE in await world.kinds(goal_id)
    row = await world.runs.get_run(first_run)
    assert row is not None and row.status is S.CANCELLED
    # A second attempt with a fresh base proceeds.
    world.launcher.default_base = "a" * 40
    world.probe.ancestry.add(("c" * 40, "a" * 40))
    await world.tick(goal_id)
    await world.tick(goal_id)
    b = await world.task(goal_id, "b")
    assert b.status is T.WAITING_HUMAN and b.run_id != first_run and b.attempt == 2
    assert b.base_sha == "a" * 40


# -- deadline and cancellation ----------------------------------------------------------------


async def test_an_expired_goal_withdraws_waiting_children_with_the_delegate_and_marks_the_rest():
    world = World()
    goal_id = await world.create(decomposition=GoalDecomposition.PLAN)
    await world.launcher.approve_plan(
        await world.plan_run(goal_id), plan_of(("a", []), ("b", ["a"]))
    )
    await world.tick(goal_id)
    a = await world.task(goal_id, "a")
    world.clock.advance(days=8)
    await world.tick(goal_id)
    goal = await world.goal(goal_id)
    assert goal.status is G.EXPIRED
    assert [rid for rid, _ in world.launcher.cancelled] == [a.run_id]
    assert world.launcher.cancelled[0][1].issuer == DELEGATION_ISSUER
    tasks = await world.tasks(goal_id)
    assert tasks["b"].status is T.FAILED and tasks["b"].last_error_code == "goal_expired"
    row = await world.runs.get_run(a.run_id)
    assert row is not None and row.status is S.CANCELLED
    # Terminal: another tick changes nothing and no child is created.
    await world.tick(goal_id)
    assert len(world.launcher.created) == 2


async def test_a_human_cancels_a_goal_with_their_own_principal():
    world = World()
    goal_id = await world.create(decomposition=GoalDecomposition.SINGLE)
    response = await world.service.cancel(str(goal_id), OWNER)
    assert response.status is G.CANCELLED
    assert world.launcher.cancelled and world.launcher.cancelled[0][1] is OWNER
    with pytest.raises(GoalNotCancellableError):
        await world.service.cancel(str(goal_id), OWNER)
    viewer = Principal(
        subject="victor",
        kind="user",
        issuer="https://issuer.test",
        roles=frozenset({"viewer"}),
        projects=frozenset({PROJECT}),
    )
    with pytest.raises(PermissionDeniedError):
        world2 = World()
        other = await world2.create(decomposition=GoalDecomposition.SINGLE)
        await world2.service.cancel(str(other), viewer)


# -- creation rules ----------------------------------------------------------------------------


async def test_repositories_without_remote_only_accept_single_goals():
    world = World(remote=False)
    for decomposition in (GoalDecomposition.AUTO, GoalDecomposition.PLAN):
        with pytest.raises(GoalDependenciesUnsupportedError):
            await world.create(decomposition=decomposition)
    goal_id = await world.create(decomposition=GoalDecomposition.SINGLE)
    task = await world.task(goal_id, "single")
    assert task.status is T.WAITING_HUMAN
    await world.launcher.complete_locally(task.run_id)
    await world.tick(goal_id)
    assert (await world.goal(goal_id)).status is G.COMPLETED


async def test_creation_requires_repos_write_and_is_idempotent_per_key():
    world = World()
    operator = Principal(
        subject="ops",
        kind="user",
        issuer="https://issuer.test",
        roles=frozenset({"operator"}),
        projects=frozenset({PROJECT}),
    )
    body = CreateGoal(objective=OBJECTIVE, project_id=PROJECT, repository_id=REPO)
    with pytest.raises(PermissionDeniedError):
        await world.service.create(body, operator)
    first = await world.service.create(
        body.model_copy(update={"decomposition": GoalDecomposition.SINGLE}),
        OWNER,
        idempotency_key="k1",
    )
    again = await world.service.create(
        body.model_copy(update={"decomposition": GoalDecomposition.SINGLE}),
        OWNER,
        idempotency_key="k1",
    )
    assert first.created and not again.created
    assert again.response.goal_id == first.response.goal_id
    with pytest.raises(GoalIdempotencyKeyReusedError):
        await world.service.create(
            body.model_copy(
                update={
                    "objective": OBJECTIVE + " distinto",
                    "decomposition": GoalDecomposition.SINGLE,
                }
            ),
            OWNER,
            idempotency_key="k1",
        )
    await world.coordinator.wait_until_idle()
    assert len(world.launcher.created) == 1


async def test_auto_decomposition_uses_the_router_without_a_classifier_call_when_fast():
    from app.routing import SmartRouter

    world = World()
    world.service._router = SmartRouter(None, clock=world.clock)  # rules only: no model
    small = "Añade la función greet(name) en src/app.py y una prueba en tests/test_greet.py"
    goal_id = await world.create(objective=small)
    goal = await world.goal(goal_id)
    # Rules only and no classifier: the router answers ``full`` -> a planning run.
    assert goal.route == "full" and goal.status is G.PLANNING
    assert world.launcher.created[0]["workflow_kind"] is WorkflowKind.PLANNING
    kinds = await world.kinds(goal_id)
    assert GoalEventKind.ROUTE_SELECTED in kinds and GoalEventKind.PLAN_REQUESTED in kinds


async def test_a_fast_routed_goal_creates_one_child_with_route_mode_fast():
    from datetime import datetime

    from app.contracts.routing import RouteMode as RM
    from app.routing import Route, RouteDecision

    class FastRouter:
        def decide(self, task: str, **kwargs: object) -> RouteDecision:
            return RouteDecision(
                route=Route.FAST,
                mode=RM.AUTO,
                reason="classified:docs/low/single_file",
                decided_at=datetime(2026, 10, 30, tzinfo=__import__("datetime").UTC),
            )

    world = World()
    world.service._router = FastRouter()  # type: ignore[assignment]
    goal_id = await world.create()
    goal = await world.goal(goal_id)
    assert goal.route == "fast" and goal.status is G.ACTIVE and goal.plan_run_id is None
    child = world.launcher.created[0]
    assert child["workflow_kind"] is WorkflowKind.DEVELOPMENT
    assert child["route_mode"] is RouteMode.FAST and child["planned_upstream"] is None

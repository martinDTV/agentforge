"""Goal Supervisor (Fase 8, Paso 1; ADR 0012): deterministic coordination of goals.

``GoalService`` are the use cases a principal calls (create, read, list, cancel,
reconcile now). ``GoalCoordinator`` is the reconciliation loop: it reads durable
facts (``goals``, ``goal_tasks``, ``runs`` rows, GitHub plus the mirror through
the ``IntegrationProbe``) and takes deterministic decisions. It never calls a
model, never approves anything and never touches a repository: children are
ordinary runs created through ``RunService.create`` with a delegated principal
of the goal's owner, who keeps every human decision (plan, scope, publish).

Idempotency: a task's ``run_id`` and idempotency key are reserved *before*
``RunService.create`` and reused on every retry, so a crash at any point leaves
either no run or exactly one. Reconciliations run under a per-goal lease and
only move state forward through the domain transition tables.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.contracts.execution import RunError
from app.contracts.goals import (
    ACTIVE_TASK_STATUSES,
    GOAL_TERMINAL_STATUSES,
    IN_FLIGHT_TASK_STATUSES,
    SUCCESSFUL_TASK_STATUSES,
    CreateGoal,
    GoalDecomposition,
    GoalEventKind,
    GoalEventRead,
    GoalResponse,
    GoalStatus,
    GoalSummary,
    GoalTaskRead,
    GoalTaskStatus,
    PlannedUpstream,
)
from app.contracts.lifecycle import DecisionStage, RunLifecycleStatus, WorkflowKind
from app.contracts.planning import (
    MAX_CONTEXT_PATHS,
    PLAN_INVALID_CODE,
    DevelopmentPlan,
    PlanConstraints,
)
from app.contracts.routing import Route, RouteMode
from app.exceptions import DomainError
from app.goals import (
    BLOCKING_REASON,
    PLANNING_RESERVATION_USD,
    SUPERVISOR_ACTOR,
    DependencyCycleError,
    GoalCursor,
    GoalEvent,
    GoalPatch,
    GoalRepository,
    IntegrationError,
    IntegrationProbe,
    NeedsHumanReason,
    NewGoal,
    NewGoalTask,
    PersistedGoal,
    PersistedGoalEvent,
    PersistedGoalTask,
    ReserveOutcome,
    TaskPatch,
    dependents_of,
    failure_code_for_run,
    fingerprint,
    needs_human_ordinal,
    reminder_due,
    reminders_due,
    resolve_limits,
    single_task_text,
    stage_ordinal,
    task_status_for_run,
    task_text,
    topological_order,
)
from app.infrastructure.db.errors import DatabaseUnavailableError
from app.infrastructure.llm.errors import LLMError
from app.orchestration.exceptions import (
    GoalDependenciesUnsupportedError,
    GoalIdempotencyKeyReusedError,
    GoalNotCancellableError,
    GoalNotFoundError,
    GoalReconcileInProgressError,
    RepositoryNotAuthorizedError,
    RunAlreadyExistsError,
    RunNotCancellableError,
    RunNotFoundError,
    ShuttingDownError,
)
from app.orchestration.limits import RunLimits
from app.orchestration.ports import PersistedRun, RunRepository
from app.orchestration.runs import CreateResult, DecisionsView
from app.projects import (
    PolicyError,
    ProjectPolicyRegistry,
    RepositoryPolicy,
    constraints_from_policy,
    filter_listing,
)
from app.routing import SmartRouter
from app.security import (
    AuthorizationPolicy,
    Permission,
    PermissionDeniedError,
    Principal,
    delegated_principal,
)
from app.settings import Settings

logger = logging.getLogger(__name__)

_T = GoalTaskStatus
_G = GoalStatus
_S = RunLifecycleStatus
PLAN_IDEMPOTENCY_SUFFIX = "plan"


class RunLauncher(Protocol):
    """What the coordinator needs from ``RunService``: create, read decisions, cancel.

    Never ``approve`` nor ``resume``: a static test keeps this module free of them."""

    async def create(
        self,
        task: str,
        principal: Principal,
        *,
        project_id: str | None = None,
        run_id: str | None = None,
        idempotency_key: str | None = None,
        workflow_kind: WorkflowKind | None = None,
        repository_id: str | None = None,
        route_mode: RouteMode | None = None,
        planned_upstream: PlannedUpstream | None = None,
        plan_constraints: PlanConstraints | None = None,
    ) -> CreateResult: ...

    async def decisions(self, run_id: str, principal: Principal) -> DecisionsView: ...

    async def cancel(self, run_id: str, principal: Principal) -> Any: ...


class RepositoryListing(Protocol):
    """Read-only listing of the mirrored base of a repository (H1): the paths the Planner
    may be told about. Best effort: ``None`` or an error only means "no listing"."""

    async def list_paths(
        self, repository_id: str, project_id: str, *, limit: int
    ) -> list[str] | None: ...


@dataclass(frozen=True)
class SupervisorLimits:
    """Process-wide limits of the coordinator (Settings in production)."""

    max_in_flight_global: int = 3
    max_in_flight_per_repository: int = 1
    lease_seconds: float = 60.0
    creating_timeout_seconds: float = 1800.0  # re-spawn a creation nobody confirmed
    active_interval_seconds: float = 60.0
    waiting_interval_seconds: float = 900.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "SupervisorLimits":
        return cls(
            max_in_flight_global=settings.supervisor_max_active_children,
            max_in_flight_per_repository=settings.supervisor_max_active_per_repository,
            lease_seconds=settings.supervisor_lease_seconds,
            creating_timeout_seconds=settings.supervisor_creating_timeout_seconds,
            active_interval_seconds=max(settings.supervisor_tick_seconds, 1.0),
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _run_error(code: str, message: str) -> RunError:
    return RunError(code=code, message=message[:500])


@dataclass
class _Reconcile:
    goal: PersistedGoal
    now: datetime
    tasks: dict[UUID, PersistedGoalTask]
    events: list[GoalEvent] = field(default_factory=list)
    blocked: set[str] = field(default_factory=set)
    finished: bool = False
    plan_calls: int = 0  # agent calls of the planning run, read from its row
    hold: set[UUID] = field(default_factory=set)  # tasks not to schedule again this tick

    @property
    def ordered(self) -> list[PersistedGoalTask]:
        return sorted(self.tasks.values(), key=lambda t: (t.ordinal, t.task_key))

    @property
    def graph(self) -> dict[str, list[str]]:
        return {t.task_key: list(t.dependencies) for t in self.tasks.values()}

    def by_key(self, key: str) -> PersistedGoalTask | None:
        return next((t for t in self.tasks.values() if t.task_key == key), None)

    def put(self, task: PersistedGoalTask | None) -> None:
        if task is not None:
            self.tasks[task.id] = task

    def event(self, kind: GoalEventKind, *, ordinal: int = 0, **payload: Any) -> None:
        payload.setdefault("executed_by", SUPERVISOR_ACTOR)
        self.events.append(GoalEvent(kind=kind, ordinal=ordinal, payload=payload))

    def needs_human(
        self, reason: NeedsHumanReason, task: PersistedGoalTask | None = None, **payload: Any
    ) -> None:
        self.blocked.add(BLOCKING_REASON.get(reason, reason).value)
        if task is not None:
            payload.setdefault("task_key", task.task_key)
            if task.run_id is not None:
                payload.setdefault("run_id", str(task.run_id))
        stage = payload.get("stage")
        try:
            stage_slot = DecisionStage(str(stage)).event_ordinal if stage else 0
        except ValueError:
            stage_slot = 0
        self.event(
            GoalEventKind.NEEDS_HUMAN,
            ordinal=needs_human_ordinal(task.ordinal if task else -1, reason, stage=stage_slot),
            reason=reason.value,
            **payload,
        )


class GoalCoordinator:
    def __init__(
        self,
        *,
        goals: GoalRepository,
        runs: RunRepository,
        launcher: RunLauncher,
        policies: ProjectPolicyRegistry,
        planning_limits: RunLimits | None = None,
        probe: IntegrationProbe | None = None,
        limits: SupervisorLimits | None = None,
        clock: Callable[[], datetime] | None = None,
        owner: str | None = None,
        listing: RepositoryListing | None = None,
    ) -> None:
        self._goals = goals
        self._runs = runs
        self._launcher = launcher
        self._policies = policies
        self._planning_limits = planning_limits or RunLimits()
        self._probe = probe
        self._listing = listing
        self._limits = limits or SupervisorLimits()
        self._clock = clock or _now
        self._owner = owner or f"supervisor:{uuid4().hex[:8]}"
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._accepting = True

    # -- introspection ----------------------------------------------------------------

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def limits(self) -> SupervisorLimits:
        return self._limits

    @property
    def goals(self) -> GoalRepository:
        return self._goals

    async def wait_until_idle(self, timeout: float = 30.0) -> bool:
        """Tests and shutdown: wait for background creations and kicks to finish."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._background:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.wait(set(self._background), timeout=remaining)
        return True

    async def shutdown(self, grace_seconds: float = 5.0) -> None:
        self._accepting = False
        if not await self.wait_until_idle(grace_seconds):
            for task in list(self._background):
                task.cancel()
            await asyncio.gather(*self._background, return_exceptions=True)

    # -- triggers ---------------------------------------------------------------------

    def kick(self, goal_id: UUID) -> None:
        """Reconcile soon, in the background (terminal hook, after a creation)."""
        if self._accepting:
            self._spawn(self.reconcile(goal_id), name=f"goal-reconcile-{goal_id}")

    async def on_run_terminal(self, row: PersistedRun) -> None:
        """``RunService`` terminal hook: a child or a plan run ended."""
        task = await self._goals.find_task_by_run(row.id)
        if task is not None:
            self.kick(task.goal_id)
            return
        goal = await self._goals.find_goal_by_plan_run(row.id)
        if goal is not None:
            self.kick(goal.id)

    async def sweep_due(self, *, limit: int = 20) -> int:
        """Periodic pass: reconcile goals whose ``next_reconcile_at`` passed."""
        now = self._clock()
        claimed = await self._goals.claim_due_goals(
            owner=self._owner, now=now, lease_seconds=self._limits.lease_seconds, limit=limit
        )
        for goal in claimed:
            await self.reconcile(goal.id, claimed=goal)
        return len(claimed)

    async def run_periodically(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            if not self._accepting:
                return
            try:
                await self.sweep_due()
            except Exception:  # pragma: no cover - defensive: never kill the loop
                logger.exception("Barrido del Supervisor con error inesperado")

    # -- reconciliation ---------------------------------------------------------------

    def _lock(self, goal_id: UUID) -> asyncio.Lock:
        return self._locks.setdefault(goal_id, asyncio.Lock())

    def _spawn(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def reconcile(
        self, goal_id: UUID, *, claimed: PersistedGoal | None = None, wait: bool = True
    ) -> PersistedGoal | None:
        """One idempotent reconciliation under the goal's lease.

        Returns the goal as persisted afterwards (``None`` when it does not exist).
        With ``wait=False`` a goal another process holds raises
        ``GoalReconcileInProgressError`` instead of being skipped silently.
        """
        async with self._lock(goal_id):
            now = self._clock()
            goal = claimed
            if goal is not None:
                goal = await self._goals.get_goal(goal_id) or goal
            if goal is None:
                goal = await self._goals.claim_goal(
                    goal_id, owner=self._owner, now=now, lease_seconds=self._limits.lease_seconds
                )
            if goal is None:
                current = await self._goals.get_goal(goal_id)
                if current is None:
                    return None
                if current.status in GOAL_TERMINAL_STATUSES:
                    return current
                if not wait:
                    raise GoalReconcileInProgressError(str(goal_id))
                return current
            next_at: datetime | None = now + timedelta(seconds=self._limits.active_interval_seconds)
            try:
                next_at = await self._reconcile_claimed(goal, now)
            except DatabaseUnavailableError as exc:
                logger.warning("Reconciliación de %s sin base de datos: %s", goal_id, exc.message)
            except Exception:
                logger.exception("Reconciliación de %s falló", goal_id)
            finally:
                try:
                    await self._goals.release_goal(
                        goal_id, owner=self._owner, next_reconcile_at=next_at
                    )
                except DatabaseUnavailableError as exc:
                    logger.warning("Lease de %s no liberado: %s", goal_id, exc.message)
            return await self._goals.get_goal(goal_id)

    async def _reconcile_claimed(self, goal: PersistedGoal, now: datetime) -> datetime | None:
        tasks = {t.id: t for t in await self._goals.list_tasks(goal.id)}
        ctx = _Reconcile(goal=goal, now=now, tasks=tasks)
        if goal.plan_run_id is not None:
            plan_row = await self._runs.get_run(goal.plan_run_id)
            ctx.plan_calls = plan_row.llm_calls if plan_row is not None else 0
        if now >= goal.deadline:
            await self._finish(ctx, _G.EXPIRED, code="goal_deadline", message="plazo vencido")
            return None
        if not tasks:
            # No tasks yet: the goal is being planned, or it is inconsistent.
            planned = goal.plan_run_id is not None or (
                goal.decomposition is not GoalDecomposition.SINGLE
                and goal.route != Route.FAST.value
            )
            if not planned:
                await self._finish(
                    ctx, _G.FAILED, code="goal_without_tasks", message="objetivo sin tareas"
                )
                return None
            await self._advance_planning(ctx)
            await self._flush(ctx)
            if ctx.finished:
                return None
            if not ctx.tasks:
                await self._update_status(ctx)
                return now + timedelta(seconds=self._limits.waiting_interval_seconds)
        await self._refresh_tasks(ctx)
        await self._check_merges(ctx)
        self._propagate_failures(ctx)
        await self._apply_skips(ctx)
        await self._mark_ready(ctx)
        await self._schedule(ctx)
        await self._flush(ctx)
        if ctx.finished:
            return None
        if await self._maybe_finish(ctx):
            return None
        await self._update_status(ctx)
        in_flight = any(
            t.status in (_T.CREATING, _T.RUNNING) or t.status is _T.READY
            for t in ctx.tasks.values()
        )
        seconds = (
            self._limits.active_interval_seconds
            if in_flight
            else self._limits.waiting_interval_seconds
        )
        return now + timedelta(seconds=seconds)

    async def _flush(self, ctx: _Reconcile) -> None:
        if ctx.events:
            events, ctx.events = ctx.events, []
            await self._goals.append_events(ctx.goal.id, events)

    # -- planning phase ---------------------------------------------------------------

    def _delegate(self, goal: PersistedGoal) -> Principal:
        return delegated_principal(
            owner_subject=goal.owner_subject,
            project_id=goal.project_id,
            goal_id=goal.id,
            expires_at=goal.deadline,
        )

    def _stale(self, since: datetime | None, now: datetime) -> bool:
        return since is None or now - since >= timedelta(
            seconds=self._limits.creating_timeout_seconds
        )

    async def _advance_planning(self, ctx: _Reconcile) -> None:
        goal = ctx.goal
        if goal.plan_run_id is None:
            plan_run_id = uuid4()
            await self._goals.charge_planning(
                goal.id, calls=self._planning_limits.max_llm_calls, usd=PLANNING_RESERVATION_USD
            )
            updated = await self._goals.update_goal(
                goal.id,
                GoalPatch(plan_run_id=plan_run_id, plan_creating_since=ctx.now),
                lease_owner=self._owner,
            )
            if updated is None:
                return
            ctx.goal = updated
            ctx.event(GoalEventKind.PLAN_REQUESTED, run_id=str(plan_run_id))
            self._spawn(self._create_plan_run(updated), name=f"goal-plan-{goal.id}")
            return
        row = await self._runs.get_run(goal.plan_run_id)
        if row is None:
            if self._stale(goal.plan_creating_since, ctx.now):
                updated = await self._goals.update_goal(
                    goal.id, GoalPatch(plan_creating_since=ctx.now), lease_owner=self._owner
                )
                if updated is not None:
                    ctx.goal = updated
                    self._spawn(self._create_plan_run(updated), name=f"goal-plan-{goal.id}")
            return
        ctx.plan_calls = row.llm_calls
        if row.status is _S.APPROVED:
            await self._materialise(ctx, row)
            return
        if row.status in (_S.REJECTED, _S.EXPIRED, _S.CANCELLED, _S.FAILED):
            decided_by, reason, decided_stage = await self._last_decision(row.id)
            code = failure_code_for_run(
                row.status,
                pending_stage=row.pending_stage,
                error_code=row.error.code if row.error else None,
                decided_stage=decided_stage,
            )
            ctx.event(
                GoalEventKind.PLAN_REFUSED,
                run_id=str(row.id),
                code=code,
                decided_by=decided_by,
                reason=reason,
            )
            status = _G.CANCELLED if row.status is _S.CANCELLED else _G.FAILED
            if code == PLAN_INVALID_CODE:
                message = (
                    "la planificación no cumplió las restricciones de la plataforma: "
                    + (row.error.message if row.error else "")
                )[:1000]
            else:
                message = "la planificación no fue aprobada"
            await self._finish(
                ctx,
                status,
                code=code if code.startswith("plan_") else f"plan_{code}",
                message=message,
                reason=reason,
                decided_by=decided_by,
            )
            return
        if row.status in (_S.WAITING_APPROVAL, _S.RESUME_PENDING):
            since = await self._waiting_since(row, ctx.now)
            midway, final = reminders_due(
                since=since,
                now=ctx.now,
                reminder_seconds=goal.limits.decision_reminder_seconds,
                final_notice_seconds=goal.limits.final_notice_seconds,
                deadline=row.approval_deadline,
            )
            deadline_iso = row.approval_deadline.isoformat() if row.approval_deadline else None
            if midway:
                ctx.needs_human(
                    NeedsHumanReason.PLAN_PENDING,
                    run_id=str(row.id),
                    stage="plan",
                    deadline=deadline_iso,
                )
            if final:
                ctx.needs_human(
                    NeedsHumanReason.PLAN_FINAL_NOTICE,
                    run_id=str(row.id),
                    stage="plan",
                    deadline=deadline_iso,
                )

    async def _last_decision(
        self, run_id: UUID
    ) -> tuple[str | None, str | None, DecisionStage | None]:
        """Who took the last recorded decision of a run, the formal reason and its stage."""
        decisions = await self._runs.list_decisions(run_id)
        if not decisions:
            return None, None, None
        last = decisions[-1]
        return last.decided_by, last.reason, last.stage

    async def _waiting_since(self, row: PersistedRun, now: datetime) -> datetime:
        """When the run started waiting: its ``waiting_approval`` event, else its last update."""
        events = await self._runs.list_events(row.id, after_id=None, limit=None)
        waiting = [e for e in events if e.kind.value == "waiting_approval"]
        return waiting[-1].created_at if waiting else row.updated_at

    async def _materialise(self, ctx: _Reconcile, row: PersistedRun) -> None:
        goal = ctx.goal
        try:
            plan = DevelopmentPlan.model_validate(row.plan or {})
            order = topological_order({t.id: t.dependencies for t in plan.tasks})
        except DependencyCycleError as exc:
            ctx.event(GoalEventKind.PLAN_APPROVED, run_id=str(row.id), tasks=0, cycle=exc.cycle)
            await self._finish(ctx, _G.FAILED, code="goal_dependency_cycle", message=str(exc))
            return
        except ValueError as exc:
            await self._finish(ctx, _G.FAILED, code="plan_invalid", message=str(exc))
            return
        if len(plan.tasks) > goal.limits.max_tasks:
            await self._finish(
                ctx,
                _G.FAILED,
                code="goal_limit_reached",
                message=f"el plan trae {len(plan.tasks)} tareas y el límite es "
                f"{goal.limits.max_tasks}",
            )
            return
        by_id = {t.id: t for t in plan.tasks}
        new_tasks: list[NewGoalTask] = []
        seen: set[str] = set()
        for index, key in enumerate(order):
            task = by_id[key]
            text = task_text(task, index=index + 1, total=len(order), objective=goal.objective)
            print_ = fingerprint(goal.repository_id, text)
            status = _T.PENDING
            if print_ in seen:
                status = _T.DUPLICATE
            else:
                duplicate = await self._goals.find_active_duplicate(goal.repository_id, print_)
                if duplicate is not None:
                    status = _T.DUPLICATE
            seen.add(print_)
            new_tasks.append(
                NewGoalTask(
                    task_key=task.id,
                    title=task.title[:200],
                    task_text=text,
                    dependencies=list(task.dependencies),
                    ordinal=index,
                    repository_id=goal.repository_id,
                    fingerprint=print_,
                    status=status,
                )
            )
        persisted = await self._goals.add_tasks(goal.id, new_tasks, now=ctx.now)
        ctx.tasks = {t.id: t for t in persisted}
        ctx.event(GoalEventKind.PLAN_APPROVED, run_id=str(row.id), tasks=len(order))
        ctx.event(GoalEventKind.TASKS_MATERIALIZED, tasks=len(order), order=order)
        for t in persisted:
            if t.status is _T.DUPLICATE:
                ctx.event(GoalEventKind.TASK_DUPLICATE, ordinal=t.ordinal, task_key=t.task_key)
        updated = await self._goals.update_goal(
            goal.id, GoalPatch(status=_G.ACTIVE, blocked_reasons=[]), lease_owner=self._owner
        )
        if updated is not None:
            ctx.goal = updated

    async def _plan_constraints(self, goal: PersistedGoal) -> PlanConstraints | None:
        """Deterministic facts the plan of ``goal`` must respect (H1): the repository policy,
        the goal's task limit and, when a mirror is available, the filtered file listing.
        The listing is a hint for the model and the gate; failing to obtain it never blocks
        planning (the gate then skips the path rule that needs it)."""
        try:
            policy = self._policy(goal)
        except PolicyError:
            return None  # the launcher refuses the run with the real error
        paths: list[str] = []
        complete = False
        if self._listing is not None:
            try:
                listed = await self._listing.list_paths(
                    goal.repository_id, goal.project_id, limit=MAX_CONTEXT_PATHS + 1
                )
            except Exception as exc:  # noqa: BLE001 - a listing is advisory by contract
                logger.warning("Listado del espejo no disponible para %s: %s", goal.id, exc)
                listed = None
            if listed is not None:
                paths = filter_listing(listed, policy)
                complete = len(listed) <= MAX_CONTEXT_PATHS
        return constraints_from_policy(
            policy,
            max_tasks=goal.limits.max_tasks,
            repository_paths=paths,
            listing_complete=complete,
        )

    async def _create_plan_run(self, goal: PersistedGoal) -> None:
        assert goal.plan_run_id is not None
        key = f"goal:{goal.id}:{PLAN_IDEMPOTENCY_SUFFIX}"
        try:
            await self._launcher.create(
                goal.objective,
                self._delegate(goal),
                project_id=goal.project_id,
                run_id=str(goal.plan_run_id),
                idempotency_key=key,
                workflow_kind=WorkflowKind.PLANNING,
                plan_constraints=await self._plan_constraints(goal),
            )
        except RunAlreadyExistsError:
            pass  # created by an earlier attempt: the next tick links it
        except (ShuttingDownError, DatabaseUnavailableError) as exc:
            logger.warning("Planificación de %s pospuesta: %s", goal.id, exc)
        except (DomainError, PermissionDeniedError, LLMError) as exc:
            code = str(getattr(exc, "code", "create_failed"))
            await self._fail_goal_quietly(goal.id, f"plan_create_failed:{code}", str(exc))
        except Exception:
            logger.exception("Creación de la planificación de %s falló", goal.id)
        finally:
            self.kick(goal.id)

    async def _fail_goal_quietly(self, goal_id: UUID, code: str, message: str) -> None:
        try:
            async with self._lock(goal_id):
                goal = await self._goals.claim_goal(
                    goal_id,
                    owner=self._owner,
                    now=self._clock(),
                    lease_seconds=self._limits.lease_seconds,
                )
                if goal is None:
                    return
                ctx = _Reconcile(goal=goal, now=self._clock(), tasks={})
                await self._finish(ctx, _G.FAILED, code=code, message=message)
                await self._flush(ctx)
                await self._goals.release_goal(goal_id, owner=self._owner, next_reconcile_at=None)
        except DatabaseUnavailableError as exc:
            logger.warning("No se pudo cerrar el objetivo %s: %s", goal_id, exc.message)

    # -- tasks <- runs ------------------------------------------------------------------

    async def _patch(
        self, ctx: _Reconcile, task: PersistedGoalTask, patch: TaskPatch
    ) -> PersistedGoalTask:
        updated = await self._goals.update_task(
            task.id, patch, expected_status=task.status, now=ctx.now
        )
        if updated is None:
            # Someone else moved it (another process): read the truth back.
            fresh = next(
                (t for t in await self._goals.list_tasks(ctx.goal.id) if t.id == task.id), task
            )
            ctx.put(fresh)
            return fresh
        ctx.put(updated)
        return updated

    async def _refresh_tasks(self, ctx: _Reconcile) -> None:
        for task in ctx.ordered:
            if task.status not in IN_FLIGHT_TASK_STATUSES or task.run_id is None:
                continue
            row = await self._runs.get_run(task.run_id)
            if row is None:
                if task.status is _T.CREATING and self._stale(task.creating_since, ctx.now):
                    fresh = await self._patch(ctx, task, TaskPatch(creating_since=ctx.now))
                    self._spawn(self._create_child(ctx.goal, fresh), name=f"goal-child-{fresh.id}")
                continue
            await self._apply_run(ctx, task, row)

    def _publication(self, row: PersistedRun) -> dict[str, Any]:
        return row.publication or {}

    async def _apply_run(self, ctx: _Reconcile, task: PersistedGoalTask, row: PersistedRun) -> None:
        new_status = task_status_for_run(row.status)
        publication = self._publication(row)
        patch = TaskPatch(llm_calls=row.llm_calls)
        for name in ("head_sha", "pr_number", "pr_url"):
            value = publication.get(name)
            if value is not None and getattr(task, name) != value:
                setattr(patch, name, value)
        if publication.get("base_sha") and task.base_sha is None:
            patch.base_sha = str(publication["base_sha"])
        if new_status is _T.WAITING_HUMAN:
            stage = row.pending_stage
            stage_name = stage.value if stage is not None else "plan"
            if task.status is not _T.WAITING_HUMAN or task.waiting_stage != stage_name:
                patch.status = _T.WAITING_HUMAN
                patch.waiting_since = ctx.now
                patch.waiting_stage = stage_name
                ctx.event(
                    GoalEventKind.TASK_WAITING_HUMAN,
                    ordinal=task.ordinal * 4 + stage_ordinal(stage),
                    task_key=task.task_key,
                    run_id=str(row.id),
                    stage=stage_name,
                    deadline=row.approval_deadline.isoformat() if row.approval_deadline else None,
                )
                task = await self._patch(ctx, task, patch)
                if stage is DecisionStage.SCOPE:
                    await self._record_scope(ctx, task, row)
                return
            since = task.waiting_since or row.updated_at
            midway, final = reminders_due(
                since=since,
                now=ctx.now,
                reminder_seconds=ctx.goal.limits.decision_reminder_seconds,
                final_notice_seconds=ctx.goal.limits.final_notice_seconds,
                deadline=row.approval_deadline,
            )
            deadline_iso = row.approval_deadline.isoformat() if row.approval_deadline else None
            if midway:
                ctx.needs_human(
                    NeedsHumanReason.DECISION_PENDING,
                    task,
                    stage=stage_name,
                    deadline=deadline_iso,
                )
            if final:
                ctx.needs_human(
                    NeedsHumanReason.DECISION_FINAL_NOTICE,
                    task,
                    stage=stage_name,
                    deadline=deadline_iso,
                )
            await self._patch(ctx, task, patch)
            return
        if new_status is _T.RUNNING:
            if task.status is not _T.RUNNING:
                patch.status = _T.RUNNING
            await self._patch(ctx, task, patch)
            return
        if new_status is _T.PUBLISHED:
            patch.status = _T.PUBLISHED
            patch.published_at = ctx.now
            dependents = dependents_of(ctx.graph, task.task_key)
            ctx.event(
                GoalEventKind.TASK_PUBLISHED,
                ordinal=task.ordinal,
                task_key=task.task_key,
                run_id=str(row.id),
                pr_number=publication.get("pr_number"),
                pr_url=publication.get("pr_url"),
                head_sha=publication.get("head_sha"),
                dependents=len(dependents),
            )
            await self._patch(ctx, task, patch)
            return
        decided_by, reason, decided_stage = (
            await self._last_decision(row.id) if row.status is _S.REJECTED else (None, None, None)
        )
        code = failure_code_for_run(
            row.status,
            pending_stage=row.pending_stage,
            error_code=row.error.code if row.error else None,
            decided_stage=decided_stage,
        )
        patch.status = _T.FAILED
        patch.last_error_code = code
        failure: dict[str, Any] = {
            "task_key": task.task_key,
            "run_id": str(row.id),
            "code": code,
            "attempt": task.attempt,
        }
        if decided_by:
            failure["decided_by"] = decided_by
        if reason:
            failure["reason"] = reason
        if row.error is not None:
            failure["message"] = row.error.message[:300]
        ctx.event(
            GoalEventKind.TASK_FAILED, ordinal=task.ordinal * 4 + min(task.attempt, 3), **failure
        )
        ctx.needs_human(
            NeedsHumanReason.TASK_FAILED,
            task,
            code=code,
            **({"reason": reason} if reason else {}),
        )
        await self._patch(ctx, task, patch)

    async def _record_scope(
        self, ctx: _Reconcile, task: PersistedGoalTask, row: PersistedRun
    ) -> None:
        """At the scope stage: keep the digest and paths shown to the human and verify the
        child's base contains every dependency's merge commit (ADR 0012 §4.2)."""
        try:
            view = await self._launcher.decisions(str(row.id), self._delegate(ctx.goal))
        except (RunNotFoundError, PermissionDeniedError, DomainError):
            return
        pending = view.pending
        if pending is None or pending.stage is not DecisionStage.SCOPE:
            return
        payload = pending.payload
        base = (payload.get("base") or {}).get("sha") if isinstance(payload, dict) else None
        paths = [str(f.get("path")) for f in payload.get("files") or [] if isinstance(f, dict)]
        patch = TaskPatch(scope_digest=pending.scope_digest, scope_paths=paths)
        if isinstance(base, str) and base:
            patch.base_sha = base
        task = await self._patch(ctx, task, patch)
        if base:
            ctx.event(
                GoalEventKind.TASK_BASE_RECORDED,
                ordinal=task.ordinal * 4 + min(task.attempt, 3),
                task_key=task.task_key,
                run_id=str(row.id),
                base_sha=base,
            )
        stale: list[str] = []
        for key in task.dependencies:
            dependency = ctx.by_key(key)
            evidence = dependency.merge_evidence if dependency is not None else None
            if evidence is None:
                continue  # not a merged dependency (cannot happen once READY); nothing to check
            if self._probe is None or not isinstance(base, str) or not base:
                stale.append(key)
                continue
            try:
                contained = await self._probe.contains(
                    ctx.goal.repository_id,
                    ctx.goal.project_id,
                    ancestor=evidence.merge_commit_sha,
                    descendant=base,
                )
            except IntegrationError as exc:
                ctx.event(
                    GoalEventKind.PROBE_FAILED, ordinal=task.ordinal, task_key=key, code=exc.code
                )
                return  # unknown: do not cancel anything on a transient error
            if not contained:
                stale.append(key)
        if not stale:
            return
        ctx.event(
            GoalEventKind.TASK_BASE_STALE,
            ordinal=task.ordinal * 4 + min(task.attempt, 3),
            task_key=task.task_key,
            run_id=str(row.id),
            base_sha=base,
            missing=stale,
        )
        if not await self._cancel_child(ctx, task, reason="base_stale"):
            ctx.needs_human(NeedsHumanReason.DECISION_PENDING, task, code="base_stale")
            return
        if task.attempt >= 2:
            await self._patch(ctx, task, TaskPatch(status=_T.FAILED, last_error_code="base_stale"))
            ctx.needs_human(NeedsHumanReason.TASK_FAILED, task, code="base_stale")
            return
        # Back to ``ready`` with a fresh run id: the same task, a new child, a new base. Not
        # in this tick: the mirror has not moved since, so a retry now would see the same base.
        ctx.hold.add(task.id)
        await self._patch(
            ctx,
            task,
            TaskPatch(
                status=_T.READY,
                run_id=None,
                creating_since=None,
                waiting_since=None,
                waiting_stage=None,
                scope_digest=None,
                scope_paths=None,
                base_sha=None,
                last_error_code="base_stale",
            ),
        )

    async def _cancel_child(self, ctx: _Reconcile, task: PersistedGoalTask, *, reason: str) -> bool:
        """D3: cancel a child of this goal, never a stranger's run; audited either way."""
        if task.run_id is None or task.goal_id != ctx.goal.id:
            return False
        row = await self._runs.get_run(task.run_id)
        if row is None or row.owner_subject != ctx.goal.owner_subject:
            return False
        try:
            await self._launcher.cancel(str(task.run_id), self._delegate(ctx.goal))
        except (RunNotCancellableError, RunNotFoundError, PermissionDeniedError) as exc:
            logger.info(
                "Hija %s de %s no cancelada (%s): %s", task.run_id, ctx.goal.id, reason, exc
            )
            return False
        ctx.event(
            GoalEventKind.CHILD_CANCELLED,
            ordinal=task.ordinal * 4 + min(task.attempt, 3),
            task_key=task.task_key,
            run_id=str(task.run_id),
            reason=reason,
        )
        return True

    # -- dependencies -------------------------------------------------------------------

    async def _check_merges(self, ctx: _Reconcile) -> None:
        graph = ctx.graph
        for task in ctx.ordered:
            if task.status is not _T.PUBLISHED:
                continue
            dependents = dependents_of(graph, task.task_key)
            if not dependents:
                continue
            if task.pr_number is None or self._probe is None:
                ctx.event(
                    GoalEventKind.MERGE_UNVERIFIED,
                    ordinal=task.ordinal,
                    task_key=task.task_key,
                    code="integration_unverifiable",
                )
                ctx.needs_human(NeedsHumanReason.MERGE_UNVERIFIED, task)
                continue
            try:
                result = await self._probe.probe(
                    ctx.goal.repository_id, ctx.goal.project_id, task.pr_number
                )
            except IntegrationError as exc:
                ctx.event(
                    GoalEventKind.PROBE_FAILED,
                    ordinal=task.ordinal,
                    task_key=task.task_key,
                    code=exc.code,
                )
                continue
            if result.state == "merged" and result.evidence is not None:
                if result.evidence.verified_in_mirror:
                    await self._patch(
                        ctx,
                        task,
                        TaskPatch(status=_T.MERGED, merge_evidence=result.evidence),
                    )
                    ctx.event(
                        GoalEventKind.DEPENDENCY_MERGED,
                        ordinal=task.ordinal,
                        task_key=task.task_key,
                        pr_number=task.pr_number,
                        merge_commit_sha=result.evidence.merge_commit_sha,
                        base_after=result.evidence.base_after,
                    )
                else:
                    ctx.event(
                        GoalEventKind.MERGE_UNVERIFIED,
                        ordinal=task.ordinal,
                        task_key=task.task_key,
                        pr_number=task.pr_number,
                        merge_commit_sha=result.evidence.merge_commit_sha,
                    )
                    ctx.needs_human(
                        NeedsHumanReason.MERGE_UNVERIFIED, task, pr_number=task.pr_number
                    )
                continue
            if result.state == "closed":
                await self._patch(
                    ctx, task, TaskPatch(status=_T.FAILED, last_error_code="pr_closed")
                )
                ctx.event(
                    GoalEventKind.PR_CLOSED,
                    ordinal=task.ordinal,
                    task_key=task.task_key,
                    pr_number=task.pr_number,
                )
                ctx.needs_human(NeedsHumanReason.PR_CLOSED, task, pr_number=task.pr_number)
                continue
            since = task.published_at or task.updated_at
            if reminder_due(
                since=since,
                now=ctx.now,
                reminder_seconds=ctx.goal.limits.merge_reminder_seconds,
            ):
                ctx.needs_human(NeedsHumanReason.MERGE_PENDING, task, pr_number=task.pr_number)

    def _propagate_failures(self, ctx: _Reconcile) -> None:
        """Compute which pending/ready tasks lost a dependency (applied by ``_apply_skips``)."""
        self._to_skip: dict[UUID, list[str]] = {}
        graph = ctx.graph
        dead = {t.task_key for t in ctx.tasks.values() if t.status in (_T.FAILED, _T.DUPLICATE)}
        changed = True
        while changed:
            changed = False
            for task in ctx.ordered:
                if task.status not in (_T.PENDING, _T.READY) or task.id in self._to_skip:
                    continue
                lost = [key for key in graph.get(task.task_key, []) if key in dead]
                if lost:
                    self._to_skip[task.id] = lost
                    dead.add(task.task_key)
                    changed = True

    async def _apply_skips(self, ctx: _Reconcile) -> None:
        for task_id, lost in self._to_skip.items():
            task = ctx.tasks[task_id]
            updated = await self._patch(
                ctx, task, TaskPatch(status=_T.SKIPPED, last_error_code=f"dependency:{lost[0]}")
            )
            if updated.status is _T.SKIPPED:
                ctx.event(
                    GoalEventKind.TASK_SKIPPED,
                    ordinal=task.ordinal,
                    task_key=task.task_key,
                    because=lost,
                )
        self._to_skip = {}

    async def _mark_ready(self, ctx: _Reconcile) -> None:
        for task in ctx.ordered:
            if task.status is not _T.PENDING:
                continue
            deps = [ctx.by_key(key) for key in task.dependencies]
            if all(d is not None and d.status is _T.MERGED for d in deps):
                updated = await self._patch(ctx, task, TaskPatch(status=_T.READY))
                if updated.status is _T.READY:
                    ctx.event(
                        GoalEventKind.TASK_READY,
                        ordinal=task.ordinal,
                        task_key=task.task_key,
                        dependencies=len(deps),
                    )

    # -- scheduling ---------------------------------------------------------------------

    def _policy(self, goal: PersistedGoal) -> RepositoryPolicy:
        return self._policies.require(goal.repository_id, goal.project_id)

    async def _schedule(self, ctx: _Reconcile) -> None:
        goal = ctx.goal
        if goal.status in GOAL_TERMINAL_STATUSES:
            return
        try:
            policy = self._policy(goal)
        except PolicyError as exc:
            await self._finish(ctx, _G.FAILED, code=exc.code, message=exc.detail)
            return
        for task in ctx.ordered:
            if task.status is not _T.READY or task.id in ctx.hold:
                continue
            duplicate = await self._goals.find_active_duplicate(
                goal.repository_id, task.fingerprint, exclude_task_id=task.id
            )
            if duplicate is not None:
                await self._patch(
                    ctx,
                    task,
                    TaskPatch(
                        status=_T.DUPLICATE,
                        last_error_code=f"duplicate_of:{str(duplicate.goal_id)[:8]}",
                    ),
                )
                ctx.event(
                    GoalEventKind.TASK_DUPLICATE,
                    ordinal=task.ordinal,
                    task_key=task.task_key,
                    duplicate_goal_id=str(duplicate.goal_id),
                    duplicate_task_key=duplicate.task_key,
                )
                continue
            result = await self._goals.reserve_child(
                goal.id,
                task.id,
                run_id=task.run_id or uuid4(),
                attempt=task.attempt + 1,
                calls=policy.max_model_calls,
                usd=policy.max_reserved_usd,
                max_in_flight_per_goal=goal.limits.max_concurrency,
                max_in_flight_per_repository=self._limits.max_in_flight_per_repository,
                max_in_flight_global=self._limits.max_in_flight_global,
                now=ctx.now,
            )
            if result.outcome is ReserveOutcome.RESERVED and result.task is not None:
                ctx.put(result.task)
                self._spawn(
                    self._create_child(goal, result.task), name=f"goal-child-{result.task.id}"
                )
                continue
            if result.outcome is ReserveOutcome.CAPACITY:
                ctx.event(
                    GoalEventKind.CAPACITY_WAIT,
                    ordinal=task.ordinal,
                    task_key=task.task_key,
                    reason=result.reason,
                )
                break
            if result.outcome is ReserveOutcome.BUDGET:
                await self._patch(
                    ctx, task, TaskPatch(status=_T.FAILED, last_error_code=result.reason)
                )
                ctx.event(
                    GoalEventKind.BUDGET_EXHAUSTED,
                    ordinal=task.ordinal,
                    task_key=task.task_key,
                    reason=result.reason,
                )
                ctx.needs_human(NeedsHumanReason.BUDGET_EXHAUSTED, task, code=result.reason)
                continue
            # STALE: somebody else moved the task; re-read on the next tick.

    async def _create_child(self, goal: PersistedGoal, task: PersistedGoalTask) -> None:
        assert task.run_id is not None
        key = f"goal:{goal.id}:{task.task_key}:{task.attempt}"
        upstream = (
            PlannedUpstream(
                goal_id=str(goal.id), task_key=task.task_key, plan_run_id=str(goal.plan_run_id)
            )
            if goal.plan_run_id is not None
            else None
        )
        route_mode = RouteMode.FAST if upstream is None and goal.route == Route.FAST.value else None
        try:
            await self._launcher.create(
                task.task_text,
                self._delegate(goal),
                project_id=goal.project_id,
                run_id=str(task.run_id),
                idempotency_key=key,
                workflow_kind=WorkflowKind.DEVELOPMENT,
                repository_id=goal.repository_id,
                route_mode=route_mode,
                planned_upstream=upstream,
            )
            await self._goals.append_events(
                goal.id,
                [
                    GoalEvent(
                        kind=GoalEventKind.CHILD_CREATED,
                        ordinal=task.ordinal * 4 + min(task.attempt, 3),
                        payload={
                            "task_key": task.task_key,
                            "run_id": str(task.run_id),
                            "attempt": task.attempt,
                            "planned_upstream": upstream is not None,
                            "delegated_for": goal.owner_subject,
                            "executed_by": SUPERVISOR_ACTOR,
                        },
                    )
                ],
            )
        except RunAlreadyExistsError:
            pass  # an earlier attempt created it: the next tick links it
        except (ShuttingDownError, DatabaseUnavailableError) as exc:
            logger.warning("Creación de la hija %s pospuesta: %s", task.run_id, exc)
        except (DomainError, PermissionDeniedError, LLMError) as exc:
            code = str(getattr(exc, "code", "create_failed"))
            await self._fail_task_quietly(goal, task, f"create_failed:{code}")
        except Exception:
            logger.exception("Creación de la hija %s falló", task.run_id)
            await self._fail_task_quietly(goal, task, "create_failed:internal")
        finally:
            self.kick(goal.id)

    async def _fail_task_quietly(
        self, goal: PersistedGoal, task: PersistedGoalTask, code: str
    ) -> None:
        try:
            now = self._clock()
            await self._goals.update_task(
                task.id,
                TaskPatch(status=_T.FAILED, last_error_code=code),
                expected_status=_T.CREATING,
                now=now,
            )
            await self._goals.append_events(
                goal.id,
                [
                    GoalEvent(
                        kind=GoalEventKind.TASK_FAILED,
                        ordinal=task.ordinal * 4 + min(task.attempt, 3),
                        payload={
                            "task_key": task.task_key,
                            "run_id": str(task.run_id),
                            "code": code,
                            "attempt": task.attempt,
                            "executed_by": SUPERVISOR_ACTOR,
                        },
                    )
                ],
            )
        except DatabaseUnavailableError as exc:
            logger.warning("No se pudo registrar el fallo de %s: %s", task.id, exc.message)

    # -- closing ------------------------------------------------------------------------

    async def _maybe_finish(self, ctx: _Reconcile) -> bool:
        tasks = list(ctx.tasks.values())
        if not tasks or any(t.status in ACTIVE_TASK_STATUSES for t in tasks):
            return False
        succeeded = sum(1 for t in tasks if t.status in SUCCESSFUL_TASK_STATUSES)
        failed = [t for t in tasks if t.status in (_T.FAILED, _T.SKIPPED, _T.DUPLICATE)]
        if not failed:
            await self._finish(ctx, _G.COMPLETED)
        elif succeeded:
            await self._finish(
                ctx,
                _G.PARTIAL,
                code=failed[0].last_error_code or "task_failed",
                message=f"{len(failed)} tareas fallidas u omitidas de {len(tasks)}",
            )
        else:
            await self._finish(
                ctx,
                _G.FAILED,
                code=failed[0].last_error_code or "task_failed",
                message="ninguna tarea llegó a publicarse",
            )
        return True

    async def _finish(
        self,
        ctx: _Reconcile,
        status: GoalStatus,
        *,
        code: str | None = None,
        message: str | None = None,
        actor: str | None = None,
        withdraw: bool = True,
        reason: str | None = None,
        decided_by: str | None = None,
    ) -> None:
        tasks = list(ctx.tasks.values())
        patch = GoalPatch(
            status=status,
            blocked_reasons=[],
            error=_run_error(code, message or code) if code else None,
            llm_calls_used=self._calls_used(ctx),
        )
        updated = await self._goals.update_goal(ctx.goal.id, patch, lease_owner=self._owner)
        if updated is None:
            return
        ctx.goal = updated
        ctx.finished = True
        kind = {
            _G.COMPLETED: GoalEventKind.GOAL_COMPLETED,
            _G.PARTIAL: GoalEventKind.GOAL_PARTIAL,
            _G.FAILED: GoalEventKind.GOAL_FAILED,
            _G.CANCELLED: GoalEventKind.GOAL_CANCELLED,
            _G.EXPIRED: GoalEventKind.GOAL_EXPIRED,
        }[status]
        payload: dict[str, Any] = {
            "tasks": len(tasks),
            "succeeded": sum(1 for t in tasks if t.status in SUCCESSFUL_TASK_STATUSES),
            "failed": sum(1 for t in tasks if t.status in (_T.FAILED, _T.SKIPPED, _T.DUPLICATE)),
        }
        if code:
            payload["code"] = code
        if actor:
            payload["decided_by"] = actor
        elif decided_by:
            payload["decided_by"] = decided_by
        if reason:
            payload["reason"] = reason
        if message:
            payload["message"] = message[:300]
        # Notification material (never persisted): the roster of tasks with their outcome.
        roster = [
            {
                "task_key": t.task_key,
                "title": t.title,
                "status": t.status.value,
                "pr_number": t.pr_number,
                "pr_url": t.pr_url,
                "merged": t.status is _T.MERGED,
                "code": t.last_error_code,
            }
            for t in sorted(tasks, key=lambda t: t.ordinal)
        ]
        context = {"tasks": roster, "usage": ctx.goal.usage.model_dump(mode="json")}
        ctx.events.append(
            GoalEvent(kind=kind, payload=payload, actor_subject=actor, context=context)
        )
        if withdraw and status in (_G.CANCELLED, _G.EXPIRED):
            await self._withdraw_children(ctx, reason=status.value)
        await self._flush(ctx)

    async def _withdraw_children(self, ctx: _Reconcile, *, reason: str) -> None:
        """A cancelled or expired goal stops asking humans for decisions on its children
        (D3) and marks what never ran; children already running finish on their own."""
        for task in ctx.ordered:
            if task.status is _T.WAITING_HUMAN:
                await self._cancel_child(ctx, task, reason=reason)
            if task.status in (_T.PENDING, _T.READY):
                await self._patch(
                    ctx, task, TaskPatch(status=_T.FAILED, last_error_code=f"goal_{reason}")
                )
            elif task.status in (_T.CREATING, _T.RUNNING, _T.WAITING_HUMAN):
                await self._patch(ctx, task, TaskPatch(last_error_code=f"goal_{reason}"))

    @staticmethod
    def _calls_used(ctx: _Reconcile) -> int:
        return ctx.plan_calls + sum(t.llm_calls for t in ctx.tasks.values())

    @staticmethod
    def _standing_reasons(ctx: _Reconcile) -> set[str]:
        """Reasons derived from durable state (not from this tick's transitions), so a goal
        stays ``blocked`` across restarts for as long as the condition holds."""
        reasons: set[str] = set()
        for task in ctx.tasks.values():
            if task.status is not _T.FAILED:
                continue
            code = task.last_error_code or ""
            if code == "pr_closed":
                reasons.add(NeedsHumanReason.PR_CLOSED.value)
            elif code.startswith("budget_"):
                reasons.add(NeedsHumanReason.BUDGET_EXHAUSTED.value)
            elif not code.startswith("goal_"):
                reasons.add(NeedsHumanReason.TASK_FAILED.value)
        return reasons

    async def _update_status(self, ctx: _Reconcile) -> None:
        goal = ctx.goal
        if goal.status in GOAL_TERMINAL_STATUSES:
            return
        reasons = sorted(ctx.blocked | self._standing_reasons(ctx))
        executing = any(t.status in (_T.CREATING, _T.RUNNING) for t in ctx.tasks.values())
        if reasons and not executing:
            # Nothing advances until a human acts (a child may still be waiting for them).
            desired = _G.BLOCKED
        elif goal.status is _G.PLANNING or (not ctx.tasks and goal.plan_run_id is not None):
            desired = _G.PLANNING
        else:
            desired = _G.ACTIVE
        calls = self._calls_used(ctx)
        if (
            desired is goal.status
            and reasons == goal.blocked_reasons
            and calls == goal.usage.llm_calls_used
        ):
            return
        updated = await self._goals.update_goal(
            goal.id,
            GoalPatch(status=desired, blocked_reasons=reasons, llm_calls_used=calls),
            lease_owner=self._owner,
        )
        if updated is not None:
            ctx.goal = updated

    # -- operator actions ---------------------------------------------------------------

    async def cancel(self, goal: PersistedGoal, *, actor: Principal) -> PersistedGoal:
        """Withdraw a goal (``runs:cancel`` checked by the service). Children waiting for a
        decision are cancelled with the *human's* principal; running ones finish alone."""
        async with self._lock(goal.id):
            now = self._clock()
            claimed = await self._goals.claim_goal(
                goal.id, owner=self._owner, now=now, lease_seconds=self._limits.lease_seconds
            )
            if claimed is None:
                current = await self._goals.get_goal(goal.id)
                if current is None:
                    raise GoalNotFoundError(str(goal.id))
                if current.status in GOAL_TERMINAL_STATUSES:
                    raise GoalNotCancellableError(str(goal.id), current.status.value)
                raise GoalReconcileInProgressError(str(goal.id))
            try:
                tasks = {t.id: t for t in await self._goals.list_tasks(goal.id)}
                ctx = _Reconcile(goal=claimed, now=now, tasks=tasks)
                for task in ctx.ordered:
                    if task.status is _T.WAITING_HUMAN and task.run_id is not None:
                        try:
                            await self._launcher.cancel(str(task.run_id), actor)
                            ctx.event(
                                GoalEventKind.CHILD_CANCELLED,
                                ordinal=task.ordinal * 4 + min(task.attempt, 3),
                                task_key=task.task_key,
                                run_id=str(task.run_id),
                                reason="goal_cancelled",
                                cancelled_by=actor.subject,
                            )
                        except (RunNotCancellableError, RunNotFoundError, PermissionDeniedError):
                            pass
                    if task.status in (_T.PENDING, _T.READY):
                        await self._patch(
                            ctx, task, TaskPatch(status=_T.FAILED, last_error_code="goal_cancelled")
                        )
                await self._finish(ctx, _G.CANCELLED, actor=actor.subject, withdraw=False)
                if ctx.goal.plan_run_id is not None and not tasks:
                    try:
                        await self._launcher.cancel(str(ctx.goal.plan_run_id), actor)
                    except (RunNotCancellableError, RunNotFoundError, PermissionDeniedError):
                        pass
            finally:
                await self._goals.release_goal(goal.id, owner=self._owner, next_reconcile_at=None)
            current = await self._goals.get_goal(goal.id)
            assert current is not None
            return current


# -- use cases ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateGoalResult:
    response: GoalResponse
    created: bool


@dataclass(frozen=True)
class GoalListing:
    items: list[PersistedGoal]
    has_more: bool


@dataclass(frozen=True)
class GoalEventListing:
    items: list[PersistedGoalEvent]
    has_more: bool


class GoalService:
    """Use cases over the coordinator; every call carries the principal of a human."""

    def __init__(
        self,
        coordinator: GoalCoordinator,
        *,
        authorization: AuthorizationPolicy,
        policies: ProjectPolicyRegistry,
        runs: RunRepository,
        router: SmartRouter | None = None,
        planning_limits: RunLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._goals = coordinator.goals
        self._authz = authorization
        self._policies = policies
        self._runs = runs
        self._router = router
        self._planning_limits = planning_limits or RunLimits()
        self._clock = clock or _now

    @property
    def coordinator(self) -> GoalCoordinator:
        return self._coordinator

    async def create(
        self, body: CreateGoal, principal: Principal, *, idempotency_key: str | None = None
    ) -> CreateGoalResult:
        project = self._authz.resolve_project_for_create(principal, body.project_id)
        if not self._authz.has(principal, Permission.REPOS_WRITE):
            raise PermissionDeniedError("Falta el permiso repos:write.")
        try:
            policy = self._policies.require(body.repository_id, project)
        except PolicyError as exc:
            raise RepositoryNotAuthorizedError(body.repository_id, project) from exc
        if policy.remote is None and body.decomposition is not GoalDecomposition.SINGLE:
            raise GoalDependenciesUnsupportedError(body.repository_id)  # D2
        limits = resolve_limits(
            body.limits,
            planning_calls=self._planning_limits.max_llm_calls,
            per_task_calls=policy.max_model_calls,
        )
        now = self._clock()
        route: str | None = None
        route_decision: dict[str, Any] | None = None
        if body.decomposition is GoalDecomposition.SINGLE:
            status, single = _G.ACTIVE, True
        elif body.decomposition is GoalDecomposition.PLAN:
            status, single = _G.PLANNING, False
        else:
            decision = await self._route(body.objective, policy)
            route = decision.route.value
            route_decision = decision.model_dump(mode="json")
            single = decision.route is Route.FAST
            status = _G.ACTIVE if single else _G.PLANNING
        goal_id = uuid4()
        tasks: list[NewGoalTask] = []
        if single:
            text = single_task_text(body.objective)
            print_ = fingerprint(body.repository_id, text)
            duplicate = await self._goals.find_active_duplicate(body.repository_id, print_)
            tasks.append(
                NewGoalTask(
                    task_key="single",
                    title=text[:200],
                    task_text=text,
                    ordinal=0,
                    repository_id=body.repository_id,
                    fingerprint=print_,
                    status=_T.DUPLICATE if duplicate is not None else _T.READY,
                )
            )
        outcome = await self._goals.create_goal(
            NewGoal(
                id=goal_id,
                project_id=project,
                repository_id=body.repository_id,
                owner_subject=principal.subject,
                objective=body.objective,
                decomposition=body.decomposition,
                dependency_policy=body.dependency_policy,
                status=status,
                limits=limits,
                deadline=now + timedelta(seconds=limits.deadline_seconds),
                route=route,
                route_decision=route_decision,
                idempotency_key=idempotency_key,
            ),
            tasks,
            now=now,
        )
        if not outcome.created:
            if outcome.goal.objective != body.objective:
                raise GoalIdempotencyKeyReusedError(idempotency_key or "")
            return CreateGoalResult(response=await self._view(outcome.goal), created=False)
        if route_decision is not None:
            await self._goals.append_events(
                goal_id,
                [
                    GoalEvent(
                        kind=GoalEventKind.ROUTE_SELECTED,
                        payload={
                            "route": route,
                            "reason": route_decision.get("reason"),
                            "policy_version": route_decision.get("policy_version"),
                            "classifier_used": route_decision.get("classifier_used"),
                            "executed_by": "agentforge:platform",
                        },
                    )
                ],
            )
        goal = await self._coordinator.reconcile(goal_id) or outcome.goal
        return CreateGoalResult(response=await self._view(goal), created=True)

    async def _route(self, objective: str, policy: RepositoryPolicy) -> Any:
        from app.routing import RouteDecision

        if self._router is None:
            return RouteDecision(
                route=Route.FULL,
                mode=RouteMode.AUTO,
                reason="router_unconfigured",
                decided_at=self._clock(),
            )
        router = self._router
        return await asyncio.to_thread(
            router.decide,
            objective,
            mode=RouteMode.AUTO,
            allow_fast=policy.allow_fast_route,
            context={"repository_id": policy.repository_id, "goal": True},
        )

    async def get(self, goal_id: str, principal: Principal) -> GoalResponse:
        goal = await self._visible(goal_id, principal)
        return await self._view(goal)

    async def list(
        self,
        principal: Principal,
        *,
        status: GoalStatus | None = None,
        project_id: str | None = None,
        owner: str | None = None,
        before: GoalCursor | None = None,
        limit: int,
    ) -> GoalListing:
        scope = self._authz.listing_scope(principal).narrowed_to(project_id)
        rows = await self._goals.list_goals(
            scope=scope, status=status, owner_subject=owner, before=before, limit=limit + 1
        )
        return GoalListing(items=rows[:limit], has_more=len(rows) > limit)

    async def events(
        self, goal_id: str, principal: Principal, *, after_id: int | None = None, limit: int
    ) -> GoalEventListing:
        goal = await self._visible(goal_id, principal)
        decision = self._authz.decide(principal, Permission.RUNS_AUDIT_READ, goal)
        if not decision.allowed:
            self._authz.raise_for(decision, run_id=goal_id)
        rows = await self._goals.list_events(goal.id, after_id=after_id, limit=limit + 1)
        return GoalEventListing(items=rows[:limit], has_more=len(rows) > limit)

    async def cancel(self, goal_id: str, principal: Principal) -> GoalResponse:
        goal = await self._visible(goal_id, principal)
        decision = self._authz.decide(principal, Permission.RUNS_CANCEL, goal)
        if not decision.allowed:
            self._authz.raise_for(decision, run_id=goal_id)
        if goal.status in GOAL_TERMINAL_STATUSES:
            raise GoalNotCancellableError(goal_id, goal.status.value)
        return await self._view(await self._coordinator.cancel(goal, actor=principal))

    async def reconcile(self, goal_id: str, principal: Principal) -> GoalResponse:
        """Operator action (``recovery:sweep``): one synchronous reconciliation now."""
        goal = await self._visible(goal_id, principal)
        if not self._authz.has(principal, Permission.RECOVERY_SWEEP):
            raise PermissionDeniedError("Falta el permiso recovery:sweep.")
        current = await self._coordinator.reconcile(goal.id, wait=False)
        return await self._view(current or goal)

    # -- helpers ------------------------------------------------------------------------

    async def _visible(self, goal_id: str, principal: Principal) -> PersistedGoal:
        try:
            gid = UUID(goal_id)
        except ValueError as exc:
            raise GoalNotFoundError(goal_id) from exc
        goal = await self._goals.get_goal(gid)
        if goal is None or not self._authz.can_see(principal, goal):
            raise GoalNotFoundError(goal_id)
        return goal

    async def _view(self, goal: PersistedGoal) -> GoalResponse:
        tasks = await self._goals.list_tasks(goal.id)
        reads: list[GoalTaskRead] = []
        for task in tasks:
            row = await self._runs.get_run(task.run_id) if task.run_id is not None else None
            reads.append(
                GoalTaskRead(
                    task_key=task.task_key,
                    title=task.title,
                    status=task.status,
                    ordinal=task.ordinal,
                    dependencies=list(task.dependencies),
                    attempt=task.attempt,
                    run_id=str(task.run_id) if task.run_id else None,
                    run_status=row.status if row else None,
                    pending_stage=row.pending_stage if row else None,
                    approval_deadline=row.approval_deadline if row else None,
                    pending_digest=task.scope_digest,
                    base_sha=task.base_sha,
                    head_sha=task.head_sha,
                    pr_number=task.pr_number,
                    pr_url=task.pr_url,
                    merge_evidence=task.merge_evidence,
                    last_error_code=task.last_error_code,
                )
            )
        return GoalResponse(
            goal_id=str(goal.id),
            status=goal.status,
            owner=goal.owner_subject,
            project_id=goal.project_id,
            repository_id=goal.repository_id,
            objective=goal.objective,
            decomposition=goal.decomposition,
            dependency_policy=goal.dependency_policy,
            limits=goal.limits,
            usage=goal.usage,
            plan_run_id=str(goal.plan_run_id) if goal.plan_run_id else None,
            route=goal.route,
            deadline=goal.deadline,
            blocked_reasons=list(goal.blocked_reasons),
            tasks=reads,
            error=goal.error,
            created_at=goal.created_at,
            updated_at=goal.updated_at,
        )


def summary_of(goal: PersistedGoal, tasks: list[PersistedGoalTask]) -> GoalSummary:
    return GoalSummary(
        goal_id=str(goal.id),
        status=goal.status,
        owner=goal.owner_subject,
        project_id=goal.project_id,
        repository_id=goal.repository_id,
        decomposition=goal.decomposition,
        tasks_total=len(tasks),
        tasks_done=sum(1 for t in tasks if t.status in SUCCESSFUL_TASK_STATUSES),
        created_at=goal.created_at,
        updated_at=goal.updated_at,
    )


def event_read(event: PersistedGoalEvent) -> GoalEventRead:
    return GoalEventRead(
        id=event.id,
        kind=event.kind,
        ordinal=event.ordinal,
        payload=event.payload,
        actor=event.actor_subject,
        created_at=event.created_at,
    )


__all__ = [
    "CreateGoalResult",
    "GoalCoordinator",
    "GoalEventListing",
    "GoalListing",
    "GoalService",
    "RepositoryListing",
    "RunLauncher",
    "SupervisorLimits",
    "event_read",
    "summary_of",
]

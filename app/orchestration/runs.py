import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, StateSnapshot
from pydantic import BaseModel, ConfigDict

from app.contracts.execution import RunError, RunResponse
from app.contracts.goals import PlannedUpstream
from app.contracts.lifecycle import (
    TERMINAL_STATUSES,
    DecisionStage,
    RunEventKind,
    RunLifecycleStatus,
    WorkflowKind,
)
from app.contracts.planning import ArchitectureReview, DevelopmentPlan, PlanConstraints
from app.contracts.routing import RouteMode
from app.infrastructure.db.errors import DatabaseUnavailableError
from app.orchestration.events import (
    attribute,
    events_from_history,
    events_from_update,
    revision_from_update,
    transition_events,
)
from app.orchestration.exceptions import (
    DecisionExpiredError,
    DecisionStageMismatchError,
    DecisionStaleError,
    IdempotencyKeyReusedError,
    LeaseLostError,
    RepositoryNotAuthorizedError,
    RepositoryRequiredError,
    RunAlreadyDecidedError,
    RunNotCancellableError,
    RunNotFoundError,
    RunNotResumableError,
    ShuttingDownError,
    WorkflowUnavailableError,
)
from app.orchestration.graph import RunGraph
from app.orchestration.policy import RecoveryPolicy, new_owner_id
from app.orchestration.ports import (
    DecisionRecord,
    LeaseToken,
    PersistedEvent,
    PersistedRun,
    RunCursor,
    RunEvent,
    RunProjection,
    RunRepository,
)
from app.orchestration.projection import build_projection, projection_differs
from app.orchestration.status import (
    PendingDecision,
    checkpoint_id_of,
    derive_status,
    has_pending_interrupt,
    pending_decision,
)
from app.projects import PolicyError, ProjectPolicyRegistry
from app.security import AuthorizationPolicy, Permission, PermissionDeniedError, Principal

logger = logging.getLogger(__name__)

ABANDONED_CODE = "abandoned"
_S = RunLifecycleStatus
_WAITING = frozenset({_S.WAITING_APPROVAL, _S.RESUME_PENDING})


_RESUMABLE_PUBLICATION_STATES = frozenset({"prepared", "approved", "pushed", "opened"})


def _publication_resumable(row: PersistedRun) -> bool:
    """A development run whose publication stopped midway or failed transiently (ADR 0011)."""
    publication = row.publication or {}
    state = publication.get("state")
    if state in _RESUMABLE_PUBLICATION_STATES:
        return True
    return state == "failed" and bool(publication.get("retryable"))


def _config(run_id: UUID) -> RunnableConfig:
    # thread_id == run_id, always: one run is exactly one LangGraph thread.
    return {"configurable": {"thread_id": str(run_id)}}


def _parse(run_id: str) -> UUID:
    try:
        return UUID(run_id)
    except ValueError as exc:
        raise RunNotFoundError(run_id) from exc


def _response(row: PersistedRun) -> RunResponse:
    return RunResponse(
        run_id=str(row.id),
        status=row.status,
        owner=row.owner_subject,
        project_id=row.project_id,
        workflow_kind=row.workflow_kind,
        pending_stage=row.pending_stage,
        approval_deadline=row.approval_deadline,
        publication=row.publication,
        route=row.route,
        route_mode=row.route_mode,
        revision=row.revision,
        llm_calls=row.llm_calls,
        architect_approved=row.architect_approved,
        plan=DevelopmentPlan.model_validate(row.plan) if row.plan else None,
        architecture=ArchitectureReview.model_validate(row.architecture)
        if row.architecture
        else None,
        error=row.error,
    )


class CreateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    response: RunResponse
    created: bool


class RecoveryOutcome(StrEnum):
    REPROJECTED = "reprojected"  # checkpoint had moved on; the row just caught up
    APPROVAL_APPLIED = "approval_applied"  # recorded decision applied to the graph
    INTERRUPTED = "interrupted"  # pending work, left recoverable (auto-resume off)
    RESUMED = "resumed"  # re-driven from the last checkpoint
    ABANDONED = "abandoned"  # attempts exhausted: terminal failure
    SKIPPED = "skipped"  # lease lost or service shutting down meanwhile


class ShutdownReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    drained: bool
    cancelled: list[str]
    leases_released: int


class RunListing(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[PersistedRun]
    has_more: bool


class EventListing(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[PersistedEvent]
    has_more: bool


class DecisionsView(BaseModel):
    """Decisions already taken for a run and the one it waits for, if any."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    status: RunLifecycleStatus
    pending: PendingDecision | None
    decisions: list[DecisionRecord]


TerminalHook = Callable[[PersistedRun], Awaitable[None]]


class RunService:
    """Use cases over a compiled run graph and a ``RunRepository``.

    Two stores, two sources of truth with different jobs: the LangGraph
    checkpoint is authoritative for *where the execution is*; the ``runs``
    row is a projection of it plus what the graph cannot know (the recorded
    human decision, the audit trail, the execution lease). They are never
    written in one transaction, and no transaction is open while the graph
    (and its LLM calls) runs.

    Coordination between processes lives in the database (ADR 0004): a run is
    driven only under a lease that a heartbeat task renews; every write by a
    lease holder is fenced by ``(owner, generation)``; losing the lease
    cancels the drive. The per-run ``asyncio.Lock`` only serialises callers
    inside this process and is not a correctness guarantee.
    """

    def __init__(
        self,
        graph: RunGraph,
        repository: RunRepository,
        policy: RecoveryPolicy | None = None,
        *,
        owner: str | None = None,
        authorization: AuthorizationPolicy | None = None,
        workflow_kind: WorkflowKind = WorkflowKind.PLANNING,
        graphs: Mapping[WorkflowKind, RunGraph] | None = None,
        policies: ProjectPolicyRegistry | None = None,
        on_terminal: TerminalHook | None = None,
        clock: Callable[[], datetime] | None = None,
        default_route_mode: RouteMode = RouteMode.AUTO,
    ) -> None:
        # ``graph`` reads any run's checkpoint (every workflow shares RunState);
        # ``graphs`` says which compiled graph may *drive* each workflow kind.
        self._graph = graph
        self._workflow_kind = WorkflowKind(workflow_kind)
        self._graphs: dict[WorkflowKind, RunGraph] = (
            dict(graphs) if graphs is not None else {self._workflow_kind: graph}
        )
        self._policies = policies
        self._on_terminal = on_terminal
        self._default_route_mode = default_route_mode
        self._clock = clock or (lambda: datetime.now(UTC))
        self._repository = repository
        self._policy = policy or RecoveryPolicy()
        # Deny by default: without an injected policy, separation of duties is
        # on and nothing is granted beyond what the principal's roles map to.
        self._authz = authorization or AuthorizationPolicy()
        self._owner = owner or new_owner_id()
        self._drives: dict[str, asyncio.Task[None]] = {}
        self._idle = asyncio.Event()
        self._idle.set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._accepting = True

    # -- introspection ----------------------------------------------------------

    @property
    def graph(self) -> RunGraph:
        return self._graph

    @property
    def repository(self) -> RunRepository:
        return self._repository

    @property
    def policy(self) -> RecoveryPolicy:
        return self._policy

    @property
    def authorization(self) -> AuthorizationPolicy:
        return self._authz

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def active_run_ids(self) -> frozenset[str]:
        return frozenset(self._drives)

    async def wait_until_idle(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except TimeoutError:
            return False
        return True

    # -- use cases --------------------------------------------------------------

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
    ) -> CreateResult:
        self._ensure_accepting()
        # Authorization first: nothing is written until the principal may
        # create in a project it belongs to (403/400 come from the policy).
        project = self._authz.resolve_project_for_create(principal, project_id)
        kind = WorkflowKind(workflow_kind) if workflow_kind is not None else self._workflow_kind
        graph = self._graph_for(kind)
        graph_input: dict[str, Any] = {"task": task, "workflow_kind": kind.value}
        if plan_constraints is not None:
            # H1: deterministic facts the plan must respect, built by the caller
            # (the goal Supervisor) from policy and limits; never from model text.
            graph_input["plan_constraints"] = plan_constraints.model_dump(mode="json")
        if kind is WorkflowKind.DEVELOPMENT:
            # Writing inside a worktree is a separate human permission (ADR 0009)
            # and the repository must carry an explicit policy for the project.
            if repository_id is None:
                raise RepositoryRequiredError()
            if not self._authz.has(principal, Permission.REPOS_WRITE):
                raise PermissionDeniedError("Falta el permiso repos:write.")
            if self._policies is None:
                raise WorkflowUnavailableError(kind.value)
            try:
                self._policies.require(repository_id, project)
            except PolicyError as exc:
                raise RepositoryNotAuthorizedError(repository_id, project) from exc
            graph_input.update(
                project_id=project,
                repository_id=repository_id,
                route_mode=(route_mode or self._default_route_mode).value,
            )
            if planned_upstream is not None:
                # ADR 0012 (D4): the router may skip Planner/Architect for a task of an
                # approved goal plan; the Scoper and every human approval stay.
                graph_input["planned_upstream"] = planned_upstream.model_dump(mode="json")
        rid = _parse(run_id) if run_id else uuid4()
        # The row (status=running, lease held by us) exists before any LLM work:
        # a crash from here on leaves something to recover, never an orphan.
        outcome = await self._repository.create_run(
            rid,
            task,
            owner=self._owner,
            ttl_seconds=self._policy.lease_ttl_seconds,
            owner_subject=principal.subject,
            project_id=project,
            idempotency_key=idempotency_key,
            workflow_kind=kind,
        )
        if not outcome.created:
            # Same key, same principal, same project: reuse only if it is the
            # same request. The run may still be in flight or finished.
            if outcome.run.task != task:
                raise IdempotencyKeyReusedError(idempotency_key or "")
            return CreateResult(
                response=await self.get(str(outcome.run.id), principal), created=False
            )
        assert outcome.lease is not None
        async with self._lock(rid):
            await self._drive(rid, graph_input, outcome.lease, graph=graph)
        return CreateResult(response=await self.get(str(rid), principal), created=True)

    async def get(self, run_id: str, principal: Principal) -> RunResponse:
        rid = _parse(run_id)
        row = await self._visible_row(rid, principal)
        if row.status not in TERMINAL_STATUSES:
            row = await self._project(rid, row=row)
        return _response(row)

    async def approve(
        self,
        run_id: str,
        approved: bool,
        principal: Principal,
        *,
        stage: DecisionStage | None = None,
        scope_digest: str | None = None,
        publish_digest: str | None = None,
        reason: str | None = None,
    ) -> RunResponse:
        """Record and apply a human decision for the stage the run waits for.

        ``stage`` is optional for the planning flow (the only stage it has);
        a ``scope`` decision additionally requires ``repos:write`` and must
        carry the digest of the exact proposal shown (``409 decision_stale``
        otherwise), and is refused once its window expired.
        """
        self._ensure_accepting()
        rid = _parse(run_id)
        async with self._lock(rid):
            # Authorization before any write: hidden -> 404, not allowed -> 403.
            row = await self._visible_row(rid, principal)
            decision = self._authz.decide(principal, Permission.RUNS_APPROVE, row)
            if not decision.allowed:
                self._authz.raise_for(decision, run_id=run_id)
            pending = await self._pending(rid, row)
            pending_stage = pending.stage if pending is not None else None
            if pending_stage is None and row.workflow_kind is WorkflowKind.PLANNING:
                pending_stage = DecisionStage.PLAN
            if stage is not None and pending_stage is not None and stage is not pending_stage:
                raise DecisionStageMismatchError(run_id, pending_stage.value, stage.value)
            effective = stage or pending_stage
            if effective is None:
                # Nothing pending: the only legitimate call left is an idempotent
                # replay of the last decision taken, so compare against that stage.
                taken = await self._repository.list_decisions(rid)
                effective = taken[-1].stage if taken else DecisionStage.PLAN
            digest: str | None = None
            if effective is DecisionStage.SCOPE:
                if not self._authz.has(principal, Permission.REPOS_WRITE):
                    raise PermissionDeniedError("Falta el permiso repos:write.")
                if pending is not None and pending.deadline is not None:
                    if self._clock() > pending.deadline:
                        await self._expire(row)
                        raise DecisionExpiredError(run_id)
                if pending is not None and (
                    scope_digest is None or scope_digest != pending.scope_digest
                ):
                    raise DecisionStaleError(run_id)
                digest = pending.scope_digest if pending is not None else scope_digest
            elif effective is DecisionStage.PUBLISH:
                # ADR 0011: repos:publish, never the creator, never (in production)
                # the scope approver, and the exact plan digest shown.
                taken = await self._repository.list_decisions(rid)
                scope_by = next(
                    (d.decided_by for d in taken if d.stage is DecisionStage.SCOPE), None
                )
                verdict = self._authz.decide_publish(principal, row, scope_approver=scope_by)
                if not verdict.allowed:
                    self._authz.raise_for(verdict, run_id=run_id)
                if pending is not None and pending.deadline is not None:
                    if self._clock() > pending.deadline:
                        await self._expire(row)
                        raise DecisionExpiredError(run_id)
                if pending is not None and (
                    publish_digest is None or publish_digest != pending.publish_digest
                ):
                    raise DecisionStaleError(run_id)
                digest = pending.publish_digest if pending is not None else publish_digest
            claim = await self._repository.claim_decision(
                rid,
                effective,
                approved,
                owner=self._owner,
                ttl_seconds=self._policy.lease_ttl_seconds,
                decided_by=principal.subject,
                scope_digest=digest,
                reason=(reason or "").strip() or None,
            )
            if claim is None:
                raise RunNotFoundError(run_id)
            if not claim.claimed:
                # Same decision again: idempotent replay. Anything else: 409.
                if (
                    claim.existing is not None
                    and claim.existing.approved == approved
                    and claim.existing.scope_digest == digest
                ):
                    return await self.get(run_id, principal)
                raise RunAlreadyDecidedError(run_id, claim.status)
            assert claim.lease is not None and claim.existing is not None
            # The decision (and who made it) is durable from this point; applying
            # it may still fail and be finished later by apply_pending_approval.
            await self._apply_decision(rid, claim.existing, claim.lease, actor=principal.subject)
        return await self.get(run_id, principal)

    async def cancel(self, run_id: str, principal: Principal) -> RunResponse:
        """Withdraw a run that waits for a decision or sits interrupted (``runs:cancel``).

        Nothing is resumed: the checkpoint stays, the row becomes ``cancelled``
        and the worktree is released. A run being driven cannot be cancelled here.
        """
        self._ensure_accepting()
        rid = _parse(run_id)
        async with self._lock(rid):
            row = await self._visible_row(rid, principal)
            decision = self._authz.decide(principal, Permission.RUNS_CANCEL, row)
            if not decision.allowed:
                self._authz.raise_for(decision, run_id=run_id)
            if row.status not in TERMINAL_STATUSES:
                row = await self._project(rid, row=row)
            cancelled = await self._repository.cancel_run(rid, actor=principal.subject)
            if cancelled is None:
                raise RunNotCancellableError(run_id, row.status.value)
            await self._finished(cancelled)
        return _response(cancelled)

    async def decisions(self, run_id: str, principal: Principal) -> DecisionsView:
        """What was decided and what is pending, for a run the principal may see."""
        rid = _parse(run_id)
        row = await self._visible_row(rid, principal)
        if row.status not in TERMINAL_STATUSES:
            row = await self._project(rid, row=row)
        pending = await self._pending(rid, row) if row.status in _WAITING else None
        return DecisionsView(
            run_id=str(rid),
            status=row.status,
            pending=pending,
            decisions=await self._repository.list_decisions(rid),
        )

    async def expire_waiting(self, *, limit: int = 50) -> list[PersistedRun]:
        """Close runs whose decision window passed (called by the recovery sweep)."""
        expired = await self._repository.expire_waiting_runs(now=self._clock(), limit=limit)
        for row in expired:
            await self._finished(row)
        return expired

    async def _pending(self, rid: UUID, row: PersistedRun) -> PendingDecision | None:
        snapshot = await self._reader_for(row.workflow_kind).aget_state(_config(rid))
        return pending_decision(snapshot)

    async def _expire(self, row: PersistedRun) -> None:
        expired = await self._repository.expire_waiting_runs(now=self._clock(), limit=50)
        for item in expired:
            await self._finished(item)

    async def _finished(self, row: PersistedRun) -> None:
        """Terminal hook (worktree release and the like); never fails the caller."""
        if self._on_terminal is None or row.status not in TERMINAL_STATUSES:
            return
        try:
            await self._on_terminal(row)
        except Exception:  # best effort by contract
            logger.exception("El gancho terminal de %s falló", row.id)

    def _reader_for(self, kind: WorkflowKind) -> RunGraph:
        """Graph used to *read* a run's checkpoint: the one of its workflow when this
        process has it (so its interrupts are seen exactly), else the shared default."""
        if kind is self._workflow_kind:
            return self._graph
        return self._graphs.get(kind, self._graph)

    def _graph_for(self, kind: WorkflowKind) -> RunGraph:
        # The default workflow drives with ``self.graph`` itself, so a test (or
        # an operator) that swaps it observes the swap; other kinds come from
        # the explicit mapping and are refused when not configured.
        if kind is self._workflow_kind:
            return self._graph
        graph = self._graphs.get(kind)
        if graph is None:
            raise WorkflowUnavailableError(kind.value)
        return graph

    async def list_runs(
        self,
        principal: Principal,
        *,
        status: RunLifecycleStatus | None = None,
        project_id: str | None = None,
        owner: str | None = None,
        before: RunCursor | None = None,
        limit: int,
    ) -> RunListing:
        """Runs the principal may see, newest first; the scope is applied on every page."""
        scope = self._authz.listing_scope(principal).narrowed_to(project_id)
        rows = await self._repository.list_runs(
            scope=scope, status=status, owner_subject=owner, before=before, limit=limit + 1
        )
        return RunListing(items=rows[:limit], has_more=len(rows) > limit)

    async def list_events(
        self, run_id: str, principal: Principal, *, after_id: int | None = None, limit: int
    ) -> EventListing:
        rid = _parse(run_id)
        row = await self._visible_row(rid, principal)  # hidden -> 404
        decision = self._authz.decide(principal, Permission.RUNS_AUDIT_READ, row)
        if not decision.allowed:
            self._authz.raise_for(decision, run_id=run_id)
        rows = await self._repository.list_events(rid, after_id=after_id, limit=limit + 1)
        return EventListing(items=rows[:limit], has_more=len(rows) > limit)

    async def resume(self, run_id: str, principal: Principal) -> RunResponse:
        """Manually re-drive an ``interrupted`` run from its last checkpoint.

        Authorization first (hidden -> 404, no ``runs:resume`` -> 403), then the
        same lease the recovery sweep uses: only one process can win it, and
        ``MAX_RUN_ATTEMPTS`` still bounds how often a run may be re-driven.
        """
        self._ensure_accepting()
        rid = _parse(run_id)
        async with self._lock(rid):
            row = await self._visible_row(rid, principal)
            decision = self._authz.decide(principal, Permission.RUNS_RESUME, row)
            if not decision.allowed:
                self._authz.raise_for(decision, run_id=run_id)
            if row.status is not _S.INTERRUPTED:
                raise RunNotResumableError(run_id, f"estado actual '{row.status}'")
            if row.workflow_kind is WorkflowKind.DEVELOPMENT:
                if not _publication_resumable(row):
                    raise RunNotResumableError(
                        run_id, "una ejecución de desarrollo interrumpida no se reconduce"
                    )
                if not self._authz.has(principal, Permission.REPOS_PUBLISH):
                    raise PermissionDeniedError("Falta el permiso repos:publish.")
            if row.attempt >= self._policy.max_attempts:
                raise RunNotResumableError(
                    run_id, f"agotó sus {self._policy.max_attempts} intentos"
                )
            acquired = await self._repository.acquire_lease(
                rid,
                owner=self._owner,
                ttl_seconds=self._policy.lease_ttl_seconds,
                from_statuses=frozenset({_S.INTERRUPTED}),
                count_attempt=True,
            )
            if acquired is None:
                raise RunNotResumableError(run_id, "otro proceso la está reanudando")
            claimed, lease = acquired
            snapshot = await self._reader_for(claimed.workflow_kind).aget_state(_config(rid))
            await self._redrive(rid, claimed, snapshot, lease, actor=principal.subject)
        return await self.get(run_id, principal)

    async def _visible_row(self, rid: UUID, principal: Principal) -> PersistedRun:
        """The row, or RunNotFoundError when it does not exist *or must stay hidden*."""
        row = await self._repository.get_run(rid)
        if row is None or not self._authz.can_see(principal, row):
            raise RunNotFoundError(str(rid))
        return row

    async def apply_pending_approval(self, run_id: str) -> RunResponse:
        """Apply a recorded decision never confirmed as applied (idempotent).

        System path (recovery, operators' tooling): the decision and who made
        it were authorized when recorded, so no principal is re-checked here.
        Takes the run's lease (only if free or expired) so it never races the
        process that recorded the decision or a concurrent recovery sweep.
        """
        rid = _parse(run_id)
        async with self._lock(rid):
            acquired = await self._repository.acquire_lease(
                rid,
                owner=self._owner,
                ttl_seconds=self._policy.lease_ttl_seconds,
                from_statuses=frozenset({_S.RESUME_PENDING}),
            )
            if acquired is None:
                return await self._read_unchecked(rid)  # someone holds it, or it moved on
            _, lease = acquired
            decision = await self._repository.get_pending_decision(rid)
            if decision is None:
                await self._repository.release_lease(rid, lease)
                return await self._read_unchecked(rid)
            await self._apply_decision(rid, decision, lease, actor=decision.decided_by)
        return await self._read_unchecked(rid)

    async def _read_unchecked(self, rid: UUID) -> RunResponse:
        """Row view without authorization: internal/system callers only."""
        row = await self._repository.get_run(rid)
        if row is None:
            raise RunNotFoundError(str(rid))
        if row.status not in TERMINAL_STATUSES:
            row = await self._project(rid, row=row)
        return _response(row)

    async def recover(
        self, row: PersistedRun, lease: LeaseToken, *, actor: str | None = None
    ) -> RecoveryOutcome:
        """Decide what a run whose lease we just took over needs (ADR 0004).

        ``row`` is the row as claimed; ``lease`` is the token the caller
        (normally ``RecoveryService``) obtained for it. Every branch ends with
        the lease released or handed to a drive that releases it. ``actor`` is
        the principal behind a manually triggered sweep, for the audit trail.
        """
        rid = row.id
        async with self._lock(rid):
            snapshot = await self._reader_for(row.workflow_kind).aget_state(_config(rid))
            if row.status is _S.RESUME_PENDING:
                decision = await self._repository.get_pending_decision(rid)
                if decision is not None:
                    await self._apply_decision(rid, decision, lease)
                    return RecoveryOutcome.APPROVAL_APPLIED
                await self._project(rid, lease=lease, release=True)
                return RecoveryOutcome.REPROJECTED

            has_checkpoint = "task" in snapshot.values
            derived = derive_status(snapshot, in_flight=False) if has_checkpoint else _S.INTERRUPTED
            if derived in TERMINAL_STATUSES or derived is _S.WAITING_APPROVAL:
                # The previous owner finished (or paused) but never projected.
                await self._backfill_audit(rid)
                await self._project(rid, lease=lease, release=True)
                return RecoveryOutcome.REPROJECTED

            if row.workflow_kind is WorkflowKind.DEVELOPMENT:
                if _publication_resumable(row):
                    # Publication is idempotent and costs no model call (ADR 0011):
                    # re-drive it, bounded by the same attempt limit.
                    if row.attempt >= self._policy.max_attempts:
                        await self._fail_abandoned(rid, snapshot, lease, row.attempt)
                        return RecoveryOutcome.ABANDONED
                    attempt = await self._repository.bump_attempt(rid, lease)
                    if attempt is None:
                        raise LeaseLostError(str(rid))
                    await self._redrive(rid, row, snapshot, lease, actor=actor, attempt=attempt)
                    return RecoveryOutcome.RESUMED
                # Writes, billable calls and sandbox programs with unknown
                # outcome are never repeated implicitly (ADR 0008).
                await self._fail_development_interrupted(row, lease)
                return RecoveryOutcome.ABANDONED

            # Pending work and nobody driving it: abandoned by its previous owner.
            if row.attempt >= self._policy.max_attempts:
                await self._fail_abandoned(rid, snapshot, lease, row.attempt)
                return RecoveryOutcome.ABANDONED
            if not self._policy.auto_resume:
                await self._mark_interrupted(rid, snapshot, lease, row)
                return RecoveryOutcome.INTERRUPTED
            attempt = await self._repository.bump_attempt(rid, lease)
            if attempt is None:
                raise LeaseLostError(str(rid))
            await self._redrive(rid, row, snapshot, lease, actor=actor, attempt=attempt)
            return RecoveryOutcome.RESUMED

    async def _redrive(
        self,
        rid: UUID,
        row: PersistedRun,
        snapshot: StateSnapshot,
        lease: LeaseToken,
        *,
        actor: str | None,
        attempt: int | None = None,
    ) -> None:
        """Continue a run from its last checkpoint under ``lease`` (shared by sweep and resume)."""
        if row.workflow_kind is WorkflowKind.DEVELOPMENT:
            if not _publication_resumable(row):
                await self._fail_development_interrupted(row, lease)
                return
            graph = self._graph_for(WorkflowKind.DEVELOPMENT)
            if snapshot.values.get("publication_interrupted") is True:
                # Re-enter the ``publish`` node: the plan and the decision stay as they
                # are; only the transient marker is cleared, as if ``approve_publish``
                # had just produced its verdict again.
                await graph.aupdate_state(
                    _config(rid), {"publication_interrupted": False}, as_node="approve_publish"
                )
                snapshot = await graph.aget_state(_config(rid))
            await self._project_status(rid, snapshot, _S.RUNNING, lease)
            await self._record(
                rid,
                [RunEvent(kind=RunEventKind.RUN_RESUMED, ordinal=attempt or row.attempt)],
                actor=actor,
            )
            await self._drive(rid, None, lease, actor=actor, graph=graph)
            return
        has_checkpoint = "task" in snapshot.values
        await self._project_status(rid, snapshot, _S.RUNNING, lease)
        await self._record(
            rid,
            [RunEvent(kind=RunEventKind.RUN_RESUMED, ordinal=attempt or row.attempt)],
            actor=actor,
        )
        graph_input: Any = None if has_checkpoint else {"task": row.task}
        await self._drive(rid, graph_input, lease, graph=self._graph_for(row.workflow_kind))

    async def _fail_development_interrupted(self, row: PersistedRun, lease: LeaseToken) -> None:
        error = RunError(
            code="development_interrupted",
            message=(
                "El intento de desarrollo se interrumpió; requiere una nueva ejecución autorizada."
            ),
        )
        await self._repository.project(
            row.id, RunProjection(status=_S.FAILED, error=error), lease=lease, release=True
        )
        await self._record(
            row.id, [RunEvent(kind=RunEventKind.RUN_FAILED, payload={"code": error.code})]
        )

    # -- shutdown ---------------------------------------------------------------

    def stop_accepting(self) -> None:
        self._accepting = False

    async def shutdown(self, grace_seconds: float) -> ShutdownReport:
        """Stop, drain, cancel what did not finish, release only our own leases.

        Cancelled drives are *not* marked terminal: their rows keep their last
        projected status, their lease is released, and recovery decides later
        from the checkpoint. A sync LLM call already running in a worker
        thread will finish on its own; its result is never checkpointed.
        """
        self.stop_accepting()
        drained = await self.wait_until_idle(grace_seconds)
        cancelled: list[str] = []
        if not drained:
            for run_id, task in list(self._drives.items()):
                task.cancel()
                cancelled.append(run_id)
            await self.wait_until_idle(5.0)
        try:
            released = await self._repository.release_leases_owned_by(self._owner)
        except Exception:
            # Shutdown must always reach the pool close below; an unreleased
            # lease simply expires and recovery takes it from there.
            logger.exception("No se pudieron liberar los leases de %s", self._owner)
            released = 0
        return ShutdownReport(drained=drained, cancelled=cancelled, leases_released=released)

    # -- internals: driving ------------------------------------------------------

    def _ensure_accepting(self) -> None:
        if not self._accepting:
            raise ShuttingDownError()

    def _lock(self, rid: UUID) -> asyncio.Lock:
        return self._locks.setdefault(str(rid), asyncio.Lock())

    @asynccontextmanager
    async def _driving(self, rid: UUID, task: asyncio.Task[None]) -> AsyncIterator[None]:
        key = str(rid)
        self._drives[key] = task
        self._idle.clear()
        try:
            yield
        finally:
            self._drives.pop(key, None)
            if not self._drives:
                self._idle.set()

    async def _drive(
        self,
        rid: UUID,
        graph_input: Any,
        lease: LeaseToken,
        *,
        actor: str | None = None,
        graph: RunGraph | None = None,
    ) -> None:
        """Run the graph under ``lease`` until it stops, then project and release.

        The heartbeat renews the lease off the event loop's critical path (the
        LLM runs in LangGraph's executor). Losing the lease cancels the drive:
        this process must not keep advancing a run someone else now owns.
        """
        lost = asyncio.Event()
        stream = asyncio.create_task(self._stream(rid, graph_input, actor, graph or self._graph))
        heartbeat = asyncio.create_task(self._heartbeat(rid, lease, lost))
        lost_waiter = asyncio.create_task(lost.wait())
        async with self._driving(rid, stream):
            try:
                await asyncio.wait({stream, lost_waiter}, return_when=asyncio.FIRST_COMPLETED)
                if not stream.done():
                    stream.cancel()
                    await asyncio.gather(stream, return_exceptions=True)
                    raise LeaseLostError(str(rid))
                try:
                    await stream
                except asyncio.CancelledError:
                    # Cancelled by shutdown: leave the row to recovery, untouched.
                    raise ShuttingDownError() from None
                except Exception:
                    # A node raised something that is not an LLMError. The checkpoint
                    # keeps the failed task; make the row say "failed" if we still can.
                    await self._project_quietly(rid, lease)
                    raise
                await self._project(rid, lease=lease, release=True)
            except asyncio.CancelledError:
                stream.cancel()
                await asyncio.gather(stream, return_exceptions=True)
                raise
            finally:
                for task in (heartbeat, lost_waiter):
                    task.cancel()
                await asyncio.gather(heartbeat, lost_waiter, return_exceptions=True)

    async def _stream(
        self, rid: UUID, graph_input: Any, actor: str | None, graph: RunGraph
    ) -> None:
        revision = 0
        async for update in graph.astream(graph_input, _config(rid), stream_mode="updates"):
            revision = revision_from_update(update, revision)
            # ``actor`` is the human whose decision caused this drive. Node events
            # belong to the agents (``executed_by`` in the payload, no actor);
            # only the events that decision causes directly carry the human.
            events = events_from_update(update, revision=revision, approved_by=actor)
            await self._record(rid, attribute(events, actor=actor))

    async def _heartbeat(self, rid: UUID, lease: LeaseToken, lost: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        ttl = self._policy.lease_ttl_seconds
        last_renewed = loop.time()
        while True:
            await asyncio.sleep(self._policy.heartbeat_interval)
            try:
                renewed = await self._repository.renew_lease(rid, lease, ttl)
            except DatabaseUnavailableError as exc:
                # Transient: keep driving while the lease is still valid.
                if loop.time() - last_renewed < ttl:
                    logger.warning("Heartbeat de %s sin base de datos: %s", rid, exc.message)
                    continue
                logger.error("Lease de %s no renovado durante todo el TTL; se asume perdido", rid)
                lost.set()
                return
            if not renewed:
                logger.warning("Lease de %s perdido (generación %d)", rid, lease.generation)
                lost.set()
                return
            last_renewed = loop.time()

    # -- internals: projecting ---------------------------------------------------

    async def _apply_decision(
        self, rid: UUID, decision: DecisionRecord, lease: LeaseToken, *, actor: str | None = None
    ) -> None:
        """Resume the graph with a recorded decision and confirm it as applied.

        The resume payload names the stage and, for ``scope``, the digest the
        human saw: the graph refuses anything that does not match its pending
        interrupt. Applied is confirmed once the run left the waiting states.
        """
        row = await self._repository.get_run(rid)
        if row is None:
            raise RunNotFoundError(str(rid))
        snapshot = await self._reader_for(row.workflow_kind).aget_state(_config(rid))
        if has_pending_interrupt(snapshot):
            resume: dict[str, Any] = {
                "approved": decision.approved,
                "stage": decision.stage.value,
                "approved_by": decision.decided_by,
            }
            if decision.scope_digest is not None:
                key = (
                    "publish_digest" if decision.stage is DecisionStage.PUBLISH else "scope_digest"
                )
                resume[key] = decision.scope_digest
            await self._drive(
                rid,
                Command(resume=resume),
                lease,
                actor=actor,
                graph=self._graph_for(row.workflow_kind),
            )
        else:
            # The resume already happened (crash after it): only the projection is missing.
            await self._project(rid, lease=lease, release=True)
        updated = await self._repository.get_run(rid)
        moved_on = updated is not None and (
            updated.status not in _WAITING or updated.pending_stage is not decision.stage
        )
        if updated is not None and moved_on:
            if await self._repository.mark_decision_applied(rid, decision.stage):
                await self._record(
                    rid,
                    [
                        RunEvent(
                            kind=RunEventKind.APPROVAL_APPLIED,
                            ordinal=decision.stage.event_ordinal,
                            payload={
                                "approved": decision.approved,
                                "stage": decision.stage.value,
                                ("approved_by" if decision.approved else "rejected_by"): actor,
                            },
                        )
                    ],
                    actor=actor,
                )

    async def _project(
        self,
        rid: UUID,
        *,
        row: PersistedRun | None = None,
        lease: LeaseToken | None = None,
        release: bool = False,
    ) -> PersistedRun:
        """Re-derive the row from the latest checkpoint; conditional write, no regressions.

        ``in_flight`` comes from the lease as the database sees it, not from
        this process: a run with a live lease is being driven by *someone*.
        """
        current = row or await self._repository.get_run(rid)
        if current is None:
            raise RunNotFoundError(str(rid))
        if current.status in TERMINAL_STATUSES:
            if release and lease is not None:
                await self._repository.release_lease(rid, lease)
            return current
        snapshot = await self._reader_for(current.workflow_kind).aget_state(_config(rid))
        if "task" not in snapshot.values:
            if release and lease is not None:
                await self._repository.release_lease(rid, lease)
            return current  # no checkpoint written yet
        in_flight = current.lease_active and not release
        projection = build_projection(
            snapshot,
            current=current.status,
            in_flight=in_flight,
            current_stage=current.pending_stage,
        )
        if not projection_differs(current, projection) and not release:
            return current
        updated = await self._repository.project(rid, projection, lease=lease, release=release)
        if updated is None:
            # Refused: someone else moved the row first, or we no longer hold the
            # lease. The checkpoint is intact; read back the truth.
            logger.info(
                "Proyección descartada para run %s: %s -> %s (checkpoint %s, lease %s)",
                rid,
                current.status,
                projection.status,
                checkpoint_id_of(snapshot),
                lease.generation if lease else "-",
            )
            if lease is not None:
                raise LeaseLostError(str(rid))
            return await self._repository.get_run(rid) or current
        await self._record(rid, transition_events(current.status, updated))
        if updated.status in TERMINAL_STATUSES and current.status not in TERMINAL_STATUSES:
            await self._finished(updated)
        return updated

    async def _project_status(
        self, rid: UUID, snapshot: StateSnapshot, status: RunLifecycleStatus, lease: LeaseToken
    ) -> PersistedRun | None:
        base = (
            build_projection(snapshot, current=status, in_flight=True)
            if "task" in snapshot.values
            else RunProjection(status=status)
        )
        return await self._repository.project(
            rid, base.model_copy(update={"status": status}), lease=lease
        )

    async def _mark_interrupted(
        self, rid: UUID, snapshot: StateSnapshot, lease: LeaseToken, row: PersistedRun
    ) -> None:
        base = (
            build_projection(snapshot, current=row.status, in_flight=False)
            if "task" in snapshot.values
            else RunProjection(status=_S.INTERRUPTED)
        )
        projection = base.model_copy(update={"status": _S.INTERRUPTED, "error": None})
        updated = await self._repository.project(rid, projection, lease=lease, release=True)
        if updated is None:
            raise LeaseLostError(str(rid))
        if row.status is not _S.INTERRUPTED:
            await self._record(
                rid, [RunEvent(kind=RunEventKind.RUN_INTERRUPTED, ordinal=row.attempt)]
            )

    async def _fail_abandoned(
        self, rid: UUID, snapshot: StateSnapshot, lease: LeaseToken, attempts: int
    ) -> None:
        error = RunError(
            code=ABANDONED_CODE,
            message=f"Ejecución abandonada tras {attempts} intentos sin completarse.",
        )
        base = (
            build_projection(snapshot, current=_S.RUNNING, in_flight=False)
            if "task" in snapshot.values
            else RunProjection(status=_S.FAILED)
        )
        projection = base.model_copy(update={"status": _S.FAILED, "error": error})
        updated = await self._repository.project(rid, projection, lease=lease, release=True)
        if updated is None:
            raise LeaseLostError(str(rid))
        await self._record(
            rid, [RunEvent(kind=RunEventKind.RUN_FAILED, payload={"code": ABANDONED_CODE})]
        )

    async def _backfill_audit(self, rid: UUID) -> None:
        """Add node events the previous owner never wrote, from checkpoint evidence."""
        row = await self._repository.get_run(rid)
        reader = self._reader_for(row.workflow_kind) if row is not None else self._graph
        history = [snapshot async for snapshot in reader.aget_state_history(_config(rid))]
        history.reverse()  # oldest first
        await self._record(rid, events_from_history(history))

    async def _project_quietly(self, rid: UUID, lease: LeaseToken) -> None:
        try:
            await self._project(rid, lease=lease, release=True)
        except (DatabaseUnavailableError, LeaseLostError) as exc:
            logger.error(
                "Proyección de run %s pendiente (%s); la recuperación la completará", rid, exc
            )

    async def _record(self, rid: UUID, events: list[RunEvent], *, actor: str | None = None) -> None:
        if not events:
            return
        if actor is not None:
            events = [event.model_copy(update={"actor_subject": actor}) for event in events]
        try:
            await self._repository.append_events(rid, events)
        except DatabaseUnavailableError as exc:
            # Audit is best effort while the graph runs: the checkpoint is the
            # source of truth and recovery can rebuild node events from it.
            logger.error(
                "Auditoría no persistida para run %s (%s): %s",
                rid,
                ", ".join(f"{e.kind}:{e.ordinal}" for e in events),
                exc.message,
            )

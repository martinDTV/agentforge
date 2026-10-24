import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, StateSnapshot
from pydantic import BaseModel, ConfigDict

from app.contracts.execution import RunError, RunResponse
from app.contracts.lifecycle import TERMINAL_STATUSES, RunEventKind, RunLifecycleStatus
from app.contracts.planning import ArchitectureReview, DevelopmentPlan
from app.infrastructure.db.errors import DatabaseUnavailableError
from app.orchestration.events import (
    events_from_history,
    events_from_update,
    revision_from_update,
    transition_events,
)
from app.orchestration.exceptions import (
    IdempotencyKeyReusedError,
    LeaseLostError,
    RunAlreadyDecidedError,
    RunNotFoundError,
    ShuttingDownError,
)
from app.orchestration.graph import RunGraph
from app.orchestration.policy import RecoveryPolicy, new_owner_id
from app.orchestration.ports import LeaseToken, PersistedRun, RunEvent, RunProjection, RunRepository
from app.orchestration.projection import build_projection, projection_differs
from app.orchestration.status import checkpoint_id_of, derive_status, has_pending_interrupt

logger = logging.getLogger(__name__)

ABANDONED_CODE = "abandoned"
_S = RunLifecycleStatus


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
    ) -> None:
        self._graph = graph
        self._repository = repository
        self._policy = policy or RecoveryPolicy()
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
        self, task: str, run_id: str | None = None, *, idempotency_key: str | None = None
    ) -> CreateResult:
        self._ensure_accepting()
        rid = _parse(run_id) if run_id else uuid4()
        # The row (status=running, lease held by us) exists before any LLM work:
        # a crash from here on leaves something to recover, never an orphan.
        outcome = await self._repository.create_run(
            rid,
            task,
            owner=self._owner,
            ttl_seconds=self._policy.lease_ttl_seconds,
            idempotency_key=idempotency_key,
        )
        if not outcome.created:
            # Same key: reuse only if it is the same request. The run may still
            # be in flight (concurrent call) or finished; either way one run.
            if outcome.run.task != task:
                raise IdempotencyKeyReusedError(idempotency_key or "")
            return CreateResult(response=await self.get(str(outcome.run.id)), created=False)
        assert outcome.lease is not None
        async with self._lock(rid):
            await self._drive(rid, {"task": task}, outcome.lease)
        return CreateResult(response=await self.get(str(rid)), created=True)

    async def get(self, run_id: str) -> RunResponse:
        rid = _parse(run_id)
        row = await self._repository.get_run(rid)
        if row is None:
            raise RunNotFoundError(run_id)
        if row.status not in TERMINAL_STATUSES:
            row = await self._project(rid, row=row)
        return _response(row)

    async def approve(self, run_id: str, approved: bool) -> RunResponse:
        self._ensure_accepting()
        rid = _parse(run_id)
        async with self._lock(rid):
            claim = await self._repository.claim_approval(
                rid, approved, owner=self._owner, ttl_seconds=self._policy.lease_ttl_seconds
            )
            if claim is None:
                raise RunNotFoundError(run_id)
            if not claim.claimed:
                # Same decision again: idempotent replay. Anything else: 409.
                if claim.existing is not None and claim.existing.approved == approved:
                    return await self.get(run_id)
                raise RunAlreadyDecidedError(run_id, claim.status)
            assert claim.lease is not None
            # The decision is durable from this point; applying it may still fail
            # and be finished later by apply_pending_approval / recovery.
            await self._apply_approval(rid, approved, claim.lease)
        return await self.get(run_id)

    async def apply_pending_approval(self, run_id: str) -> RunResponse:
        """Apply a recorded decision never confirmed as applied (idempotent).

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
                if await self._repository.get_run(rid) is None:
                    raise RunNotFoundError(run_id)
                return await self.get(run_id)  # someone holds it, or it moved on
            _, lease = acquired
            decision = await self._repository.get_approval(rid)
            if decision is None or decision.applied_at is not None:
                await self._repository.release_lease(rid, lease)
                return await self.get(run_id)
            await self._apply_approval(rid, decision.approved, lease)
        return await self.get(run_id)

    async def recover(self, row: PersistedRun, lease: LeaseToken) -> RecoveryOutcome:
        """Decide what a run whose lease we just took over needs (ADR 0004).

        ``row`` is the row as claimed; ``lease`` is the token the caller
        (normally ``RecoveryService``) obtained for it. Every branch ends with
        the lease released or handed to a drive that releases it.
        """
        rid = row.id
        async with self._lock(rid):
            snapshot = await self._graph.aget_state(_config(rid))
            if row.status is _S.RESUME_PENDING:
                decision = await self._repository.get_approval(rid)
                if decision is not None and decision.applied_at is None:
                    await self._apply_approval(rid, decision.approved, lease)
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
            await self._project_status(rid, snapshot, _S.RUNNING, lease)
            await self._record(rid, [RunEvent(kind=RunEventKind.RUN_RESUMED, ordinal=attempt)])
            graph_input: Any = None if has_checkpoint else {"task": row.task}
            await self._drive(rid, graph_input, lease)
            return RecoveryOutcome.RESUMED

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

    async def _drive(self, rid: UUID, graph_input: Any, lease: LeaseToken) -> None:
        """Run the graph under ``lease`` until it stops, then project and release.

        The heartbeat renews the lease off the event loop's critical path (the
        LLM runs in LangGraph's executor). Losing the lease cancels the drive:
        this process must not keep advancing a run someone else now owns.
        """
        lost = asyncio.Event()
        stream = asyncio.create_task(self._stream(rid, graph_input))
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
            finally:
                for task in (heartbeat, lost_waiter):
                    task.cancel()
                await asyncio.gather(heartbeat, lost_waiter, return_exceptions=True)

    async def _stream(self, rid: UUID, graph_input: Any) -> None:
        revision = 0
        async for update in self._graph.astream(graph_input, _config(rid), stream_mode="updates"):
            revision = revision_from_update(update, revision)
            await self._record(rid, events_from_update(update, revision=revision))

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

    async def _apply_approval(self, rid: UUID, approved: bool, lease: LeaseToken) -> None:
        snapshot = await self._graph.aget_state(_config(rid))
        if has_pending_interrupt(snapshot):
            await self._drive(rid, Command(resume={"approved": approved}), lease)
        else:
            # The resume already happened (crash after it): only the projection is missing.
            await self._project(rid, lease=lease, release=True)
        row = await self._repository.get_run(rid)
        if row is not None and row.status in (_S.APPROVED, _S.REJECTED):
            if await self._repository.mark_approval_applied(rid):
                await self._record(
                    rid,
                    [RunEvent(kind=RunEventKind.APPROVAL_APPLIED, payload={"approved": approved})],
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
        snapshot = await self._graph.aget_state(_config(rid))
        if "task" not in snapshot.values:
            if release and lease is not None:
                await self._repository.release_lease(rid, lease)
            return current  # no checkpoint written yet
        in_flight = current.lease_active and not release
        projection = build_projection(snapshot, current=current.status, in_flight=in_flight)
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
        history = [snapshot async for snapshot in self._graph.aget_state_history(_config(rid))]
        history.reverse()  # oldest first
        await self._record(rid, events_from_history(history))

    async def _project_quietly(self, rid: UUID, lease: LeaseToken) -> None:
        try:
            await self._project(rid, lease=lease, release=True)
        except (DatabaseUnavailableError, LeaseLostError) as exc:
            logger.error(
                "Proyección de run %s pendiente (%s); la recuperación la completará", rid, exc
            )

    async def _record(self, rid: UUID, events: list[RunEvent]) -> None:
        if not events:
            return
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

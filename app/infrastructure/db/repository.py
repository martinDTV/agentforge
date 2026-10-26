"""PostgreSQL implementation of ``RunRepository`` (SQLAlchemy 2.0 async).

Every public method is exactly one short transaction. Connection-level
failures become ``DatabaseUnavailableError``; programming errors and
integrity violations propagate unchanged, because they are bugs, not outages.

Coordination lives in the database, never in process memory:

- leases are taken with conditional UPDATEs (``lease free or expired``),
- every write by a lease holder is fenced by ``(lease_owner, lease_generation)``,
- every projection is also fenced by a monotonic ``last_checkpoint_id``,
- recovery claims rows with ``FOR UPDATE SKIP LOCKED``.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.execution import RunError
from app.contracts.lifecycle import RunEventKind, RunLifecycleStatus, WorkflowKind, sources_for
from app.orchestration.exceptions import RunAlreadyExistsError
from app.orchestration.ports import (
    ApprovalClaim,
    ApprovalDecision,
    CreateOutcome,
    LeaseToken,
    PersistedEvent,
    PersistedRun,
    RunCursor,
    RunEvent,
    RunProjection,
)
from app.security import AccessScope

from .errors import as_unavailable
from .models import RunApprovalRecord, RunEventRecord, RunRecord

_LEASE_FREE = or_(RunRecord.lease_owner.is_(None), RunRecord.lease_expires_at < func.now())
_LEASE_ACTIVE = and_(RunRecord.lease_owner.is_not(None), RunRecord.lease_expires_at > func.now())


def _expiry(ttl_seconds: float) -> Any:
    return func.now() + timedelta(seconds=ttl_seconds)


def _to_run(record: RunRecord, lease_active: bool) -> PersistedRun:
    error = (
        RunError(code=record.error_code, message=record.error_message or "")
        if record.error_code is not None
        else None
    )
    return PersistedRun(
        id=record.id,
        task=record.task,
        status=record.status,
        owner_subject=record.owner_subject,
        project_id=record.project_id,
        workflow_kind=record.workflow_kind,
        revision=record.revision,
        llm_calls=record.llm_calls,
        architect_approved=record.architect_approved,
        plan=record.plan,
        architecture=record.architecture,
        error=error,
        last_checkpoint_id=record.last_checkpoint_id,
        idempotency_key=record.idempotency_key,
        attempt=record.attempt,
        lease_owner=record.lease_owner,
        lease_expires_at=record.lease_expires_at,
        lease_generation=record.lease_generation,
        lease_active=lease_active,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _to_decision(record: RunApprovalRecord | None) -> ApprovalDecision | None:
    if record is None:
        return None
    return ApprovalDecision(
        approved=record.approved,
        decided_at=record.decided_at,
        applied_at=record.applied_at,
        decided_by=record.decided_by,
    )


def _fenced(lease: LeaseToken) -> Any:
    return and_(
        RunRecord.lease_owner == lease.owner, RunRecord.lease_generation == lease.generation
    )


class SqlAlchemyRunRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

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

    async def _load(self, session: AsyncSession, run_id: UUID) -> PersistedRun | None:
        row = (
            await session.execute(
                select(RunRecord, _LEASE_ACTIVE.label("lease_active")).where(RunRecord.id == run_id)
            )
        ).first()
        if row is None:
            return None
        return _to_run(row[0], bool(row[1]))

    # -- rows -----------------------------------------------------------------

    async def create_run(
        self,
        run_id: UUID,
        task: str,
        *,
        owner: str,
        ttl_seconds: float,
        owner_subject: str,
        project_id: str,
        idempotency_key: str | None = None,
        workflow_kind: WorkflowKind = WorkflowKind.PLANNING,
    ) -> CreateOutcome:
        async with self._tx() as session:
            stmt = pg_insert(RunRecord).values(
                id=run_id,
                task=task,
                status=RunLifecycleStatus.RUNNING,
                owner_subject=owner_subject,
                project_id=project_id,
                workflow_kind=workflow_kind,
                idempotency_key=idempotency_key,
                attempt=1,
                lease_owner=owner,
                lease_expires_at=_expiry(ttl_seconds),
                lease_generation=1,
            )
            if idempotency_key is not None:
                # Atomic against the partial unique index over (project, principal,
                # key): no check-then-insert race, and no cross-principal reuse.
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["project_id", "owner_subject", "idempotency_key"],
                    index_where=text("idempotency_key IS NOT NULL"),
                )
            try:
                inserted = (
                    await session.execute(stmt.returning(RunRecord.id))
                ).scalar_one_or_none()
            except IntegrityError as exc:
                raise RunAlreadyExistsError(str(run_id)) from exc
            if inserted is None:
                existing = (
                    await session.execute(
                        select(RunRecord, _LEASE_ACTIVE).where(
                            RunRecord.project_id == project_id,
                            RunRecord.owner_subject == owner_subject,
                            RunRecord.idempotency_key == idempotency_key,
                        )
                    )
                ).first()
                if existing is None:  # pragma: no cover - only if the row vanished meanwhile
                    raise RunAlreadyExistsError(str(run_id))
                return CreateOutcome(run=_to_run(existing[0], bool(existing[1])), created=False)
            session.add(
                RunEventRecord(
                    run_id=run_id,
                    kind=RunEventKind.RUN_CREATED,
                    payload={},
                    actor_subject=owner_subject,
                )
            )
            await session.flush()
            run = await self._load(session, run_id)
            assert run is not None
            return CreateOutcome(run=run, created=True, lease=LeaseToken(owner=owner, generation=1))

    async def get_run(self, run_id: UUID) -> PersistedRun | None:
        async with self._tx() as session:
            return await self._load(session, run_id)

    async def list_runs(
        self,
        *,
        scope: AccessScope,
        status: RunLifecycleStatus | None = None,
        owner_subject: str | None = None,
        before: RunCursor | None = None,
        limit: int,
    ) -> list[PersistedRun]:
        if scope.is_empty:
            return []
        conditions: list[Any] = []
        if scope.projects is not None:
            conditions.append(RunRecord.project_id.in_(sorted(scope.projects)))
        if scope.owner_only is not None:
            conditions.append(RunRecord.owner_subject == scope.owner_only)
        if owner_subject is not None:
            conditions.append(RunRecord.owner_subject == owner_subject)
        if status is not None:
            conditions.append(RunRecord.status == status)
        if before is not None:
            # Keyset on (created_at, id) descending: strictly older than the cursor.
            conditions.append(
                tuple_(RunRecord.created_at, RunRecord.id) < tuple_(before.created_at, before.id)
            )
        stmt = (
            select(RunRecord, _LEASE_ACTIVE.label("lease_active"))
            .where(*conditions)
            .order_by(RunRecord.created_at.desc(), RunRecord.id.desc())
            .limit(limit)
        )
        async with self._tx() as session:
            rows = (await session.execute(stmt)).all()
            return [_to_run(row[0], bool(row[1])) for row in rows]

    async def project(
        self,
        run_id: UUID,
        projection: RunProjection,
        *,
        lease: LeaseToken | None = None,
        release: bool = False,
    ) -> PersistedRun | None:
        conditions: list[Any] = [
            RunRecord.id == run_id,
            RunRecord.status.in_(list(sources_for(projection.status))),
        ]
        if projection.checkpoint_id is not None:
            # Checkpoint ids are time-ordered (uuid6): never write an older one.
            conditions.append(
                or_(
                    RunRecord.last_checkpoint_id.is_(None),
                    RunRecord.last_checkpoint_id <= projection.checkpoint_id,
                )
            )
        if lease is not None:
            conditions.append(_fenced(lease))
        values: dict[str, Any] = {
            "status": projection.status,
            "revision": projection.revision,
            "llm_calls": projection.llm_calls,
            "architect_approved": projection.architect_approved,
            "plan": projection.plan,
            "architecture": projection.architecture,
            "error_code": projection.error.code if projection.error else None,
            "error_message": projection.error.message if projection.error else None,
            "last_checkpoint_id": projection.checkpoint_id,
            "updated_at": func.now(),
        }
        if release:
            values.update(lease_owner=None, lease_expires_at=None)
        async with self._tx() as session:
            updated = (
                await session.execute(
                    update(RunRecord).where(*conditions).values(**values).returning(RunRecord.id)
                )
            ).scalar_one_or_none()
            if updated is None:
                return None
            return await self._load(session, run_id)

    # -- leases ---------------------------------------------------------------

    async def acquire_lease(
        self,
        run_id: UUID,
        *,
        owner: str,
        ttl_seconds: float,
        from_statuses: frozenset[RunLifecycleStatus],
        count_attempt: bool = False,
    ) -> tuple[PersistedRun, LeaseToken] | None:
        async with self._tx() as session:
            generation = (
                await session.execute(
                    update(RunRecord)
                    .where(
                        RunRecord.id == run_id,
                        RunRecord.status.in_(list(from_statuses)),
                        _LEASE_FREE,
                    )
                    .values(
                        lease_owner=owner,
                        lease_expires_at=_expiry(ttl_seconds),
                        lease_generation=RunRecord.lease_generation + 1,
                        attempt=RunRecord.attempt + (1 if count_attempt else 0),
                        updated_at=func.now(),
                    )
                    .returning(RunRecord.lease_generation)
                )
            ).scalar_one_or_none()
            if generation is None:
                return None
            run = await self._load(session, run_id)
            assert run is not None
            return run, LeaseToken(owner=owner, generation=int(generation))

    async def bump_attempt(self, run_id: UUID, lease: LeaseToken) -> int | None:
        async with self._tx() as session:
            attempt = (
                await session.execute(
                    update(RunRecord)
                    .where(RunRecord.id == run_id, _fenced(lease))
                    .values(attempt=RunRecord.attempt + 1, updated_at=func.now())
                    .returning(RunRecord.attempt)
                )
            ).scalar_one_or_none()
            return int(attempt) if attempt is not None else None

    async def renew_lease(self, run_id: UUID, lease: LeaseToken, ttl_seconds: float) -> bool:
        async with self._tx() as session:
            result = await session.execute(
                update(RunRecord)
                .where(RunRecord.id == run_id, _fenced(lease))
                .values(lease_expires_at=_expiry(ttl_seconds))
                .returning(RunRecord.id)
            )
            return result.scalar_one_or_none() is not None

    async def release_lease(self, run_id: UUID, lease: LeaseToken) -> bool:
        async with self._tx() as session:
            result = await session.execute(
                update(RunRecord)
                .where(RunRecord.id == run_id, _fenced(lease))
                .values(lease_owner=None, lease_expires_at=None, updated_at=func.now())
                .returning(RunRecord.id)
            )
            return result.scalar_one_or_none() is not None

    async def release_leases_owned_by(self, owner: str) -> int:
        async with self._tx() as session:
            rows = (
                await session.execute(
                    update(RunRecord)
                    .where(RunRecord.lease_owner == owner)
                    .values(lease_owner=None, lease_expires_at=None, updated_at=func.now())
                    .returning(RunRecord.id)
                )
            ).all()
            return len(rows)

    async def claim_expired_leases(
        self,
        *,
        owner: str,
        ttl_seconds: float,
        statuses: frozenset[RunLifecycleStatus],
        limit: int,
        projects: frozenset[str] | None = None,
    ) -> list[tuple[PersistedRun, LeaseToken]]:
        if projects is not None and not projects:
            return []
        scope_conditions: list[Any] = [RunRecord.status.in_(list(statuses)), _LEASE_FREE]
        if projects is not None:
            scope_conditions.append(RunRecord.project_id.in_(sorted(projects)))
        candidates = (
            select(RunRecord.id)
            .where(*scope_conditions)
            .order_by(RunRecord.updated_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("candidates")
        )
        async with self._tx() as session:
            rows = (
                await session.execute(
                    update(RunRecord)
                    .where(RunRecord.id == candidates.c.id)
                    .values(
                        lease_owner=owner,
                        lease_expires_at=_expiry(ttl_seconds),
                        lease_generation=RunRecord.lease_generation + 1,
                        updated_at=func.now(),
                    )
                    .returning(RunRecord.id, RunRecord.lease_generation)
                )
            ).all()
            claimed: list[tuple[PersistedRun, LeaseToken]] = []
            for run_id, generation in rows:
                run = await self._load(session, run_id)
                if run is not None:
                    claimed.append((run, LeaseToken(owner=owner, generation=int(generation))))
            return claimed

    # -- audit ----------------------------------------------------------------

    async def append_events(self, run_id: UUID, events: Sequence[RunEvent]) -> int:
        if not events:
            return 0
        async with self._tx() as session:
            stmt = (
                pg_insert(RunEventRecord)
                .values(
                    [
                        {
                            "run_id": run_id,
                            "kind": event.kind,
                            "ordinal": event.ordinal,
                            "payload": event.payload,
                            "checkpoint_id": event.checkpoint_id,
                            "actor_subject": event.actor_subject,
                        }
                        for event in events
                    ]
                )
                .on_conflict_do_nothing(constraint="uq_run_events_run_id_kind_ordinal")
                # RETURNING only yields the rows actually inserted, which is the
                # reliable count (rowcount is -1 for batched inserts on psycopg).
                .returning(RunEventRecord.id)
            )
            inserted = (await session.execute(stmt)).scalars().all()
            return len(inserted)

    async def list_events(
        self, run_id: UUID, *, after_id: int | None = None, limit: int | None = None
    ) -> list[PersistedEvent]:
        stmt = select(RunEventRecord).where(RunEventRecord.run_id == run_id)
        if after_id is not None:
            stmt = stmt.where(RunEventRecord.id > after_id)
        stmt = stmt.order_by(RunEventRecord.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._tx() as session:
            records = await session.scalars(stmt)
            return [
                PersistedEvent(
                    id=r.id,
                    kind=r.kind,
                    ordinal=r.ordinal,
                    payload=r.payload,
                    checkpoint_id=r.checkpoint_id,
                    actor_subject=r.actor_subject,
                    created_at=r.created_at,
                )
                for r in records
            ]

    # -- approvals ------------------------------------------------------------

    async def claim_approval(
        self,
        run_id: UUID,
        approved: bool,
        *,
        owner: str,
        ttl_seconds: float,
        decided_by: str | None,
    ) -> ApprovalClaim | None:
        async with self._tx() as session:
            generation = (
                await session.execute(
                    update(RunRecord)
                    .where(
                        RunRecord.id == run_id,
                        RunRecord.status == RunLifecycleStatus.WAITING_APPROVAL,
                        _LEASE_FREE,
                    )
                    .values(
                        status=RunLifecycleStatus.RESUME_PENDING,
                        lease_owner=owner,
                        lease_expires_at=_expiry(ttl_seconds),
                        lease_generation=RunRecord.lease_generation + 1,
                        updated_at=func.now(),
                    )
                    .returning(RunRecord.lease_generation)
                )
            ).scalar_one_or_none()
            if generation is None:
                record = await session.get(RunRecord, run_id)
                if record is None:
                    return None
                existing = await session.scalar(
                    select(RunApprovalRecord).where(RunApprovalRecord.run_id == run_id)
                )
                return ApprovalClaim(
                    claimed=False, status=record.status, existing=_to_decision(existing)
                )
            approval = RunApprovalRecord(run_id=run_id, approved=approved, decided_by=decided_by)
            session.add(approval)
            session.add(
                RunEventRecord(
                    run_id=run_id,
                    kind=RunEventKind.APPROVAL_RECORDED,
                    payload={"approved": approved},
                    actor_subject=decided_by,
                )
            )
            await session.flush()
            await session.refresh(approval)
            return ApprovalClaim(
                claimed=True,
                status=RunLifecycleStatus.RESUME_PENDING,
                existing=_to_decision(approval),
                lease=LeaseToken(owner=owner, generation=int(generation)),
            )

    async def get_approval(self, run_id: UUID) -> ApprovalDecision | None:
        async with self._tx() as session:
            record = await session.scalar(
                select(RunApprovalRecord).where(RunApprovalRecord.run_id == run_id)
            )
            return _to_decision(record)

    async def mark_approval_applied(self, run_id: UUID) -> bool:
        async with self._tx() as session:
            result = await session.execute(
                update(RunApprovalRecord)
                .where(
                    RunApprovalRecord.run_id == run_id,
                    RunApprovalRecord.applied_at.is_(None),
                )
                .values(applied_at=func.now())
                .returning(RunApprovalRecord.id)
            )
            return result.scalar_one_or_none() is not None

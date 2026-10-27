"""PostgreSQL implementation of ``PublicationAuditRepository`` (append-only)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.publishing import PublicationRecord, PublicationState

from .errors import as_unavailable
from .models import PublicationAuditRecord


def _to_record(row: PublicationAuditRecord) -> PublicationRecord:
    return PublicationRecord(
        run_id=row.run_id,
        repository_id=row.repository_id,
        project_id=row.project_id,
        state=PublicationState(row.state),
        code=row.code,
        retryable=row.retryable,
        remote_branch=row.remote_branch,
        base_branch=row.base_branch,
        base_sha=row.base_sha,
        base_branch_head=row.base_branch_head,
        head_sha=row.head_sha,
        patch_digest=row.patch_digest,
        publish_digest=row.publish_digest,
        pr_number=row.pr_number,
        pr_url=row.pr_url,
        approved_by=row.approved_by,
        actor_subject=row.actor_subject,
        attempt=row.attempt,
        token_expires_at=row.token_expires_at,
        recorded_at=row.recorded_at,
    )


class SqlAlchemyPublicationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

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

    async def record(self, record: PublicationRecord) -> int:
        async with self._tx() as session:
            row = PublicationAuditRecord(
                run_id=record.run_id,
                repository_id=record.repository_id,
                project_id=record.project_id,
                state=record.state.value,
                code=record.code,
                retryable=record.retryable,
                remote_branch=record.remote_branch,
                base_branch=record.base_branch,
                base_sha=record.base_sha,
                base_branch_head=record.base_branch_head,
                head_sha=record.head_sha,
                patch_digest=record.patch_digest,
                publish_digest=record.publish_digest,
                pr_number=record.pr_number,
                pr_url=record.pr_url,
                approved_by=record.approved_by,
                actor_subject=record.actor_subject,
                attempt=record.attempt,
                token_expires_at=record.token_expires_at,
                recorded_at=record.recorded_at,
            )
            session.add(row)
            await session.flush()
            return int(row.id)

    async def list_for_run(self, run_id: UUID) -> list[PublicationRecord]:
        async with self._tx() as session:
            rows = await session.scalars(
                select(PublicationAuditRecord)
                .where(PublicationAuditRecord.run_id == run_id)
                .order_by(PublicationAuditRecord.id)
            )
            return [_to_record(row) for row in rows]

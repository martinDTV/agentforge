"""PostgreSQL implementation of ``MirrorSyncRepository`` (append-only audit)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.git.mirrors import MirrorSync

from .errors import as_unavailable
from .models import MirrorSyncRecord


def _to_sync(record: MirrorSyncRecord) -> MirrorSync:
    return MirrorSync(
        repository_id=record.repository_id,
        project_id=record.project_id,
        operation=record.operation,
        result=record.result,
        code=record.code,
        base_sha=record.base_sha,
        previous_sha=record.previous_sha,
        changed=record.changed,
        github_repository_id=record.github_repository_id,
        installation_id=record.installation_id,
        token_expires_at=record.token_expires_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        actor_subject=record.actor_subject,
    )


class SqlAlchemyMirrorSyncRepository:
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

    async def record(self, sync: MirrorSync) -> int:
        async with self._tx() as session:
            row = MirrorSyncRecord(
                repository_id=sync.repository_id,
                project_id=sync.project_id,
                operation=sync.operation,
                result=sync.result,
                code=sync.code,
                base_sha=sync.base_sha,
                previous_sha=sync.previous_sha,
                changed=sync.changed,
                github_repository_id=sync.github_repository_id,
                installation_id=sync.installation_id,
                token_expires_at=sync.token_expires_at,
                started_at=sync.started_at,
                finished_at=sync.finished_at,
                actor_subject=sync.actor_subject,
            )
            session.add(row)
            await session.flush()
            return int(row.id)

    async def list_for_repository(self, repository_id: str, *, limit: int = 50) -> list[MirrorSync]:
        async with self._tx() as session:
            rows = await session.scalars(
                select(MirrorSyncRecord)
                .where(MirrorSyncRecord.repository_id == repository_id)
                .order_by(MirrorSyncRecord.id.desc())
                .limit(limit)
            )
            return [_to_sync(row) for row in rows]

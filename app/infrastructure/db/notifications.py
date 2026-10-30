"""PostgreSQL notification outbox and route-decision audit (SQLAlchemy 2.0 async)."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.notifications import NotificationKind, OutboxDraft, OutboxItem, OutboxStatus
from app.routing import Route, RouteDecision, RouteMode

from .errors import as_unavailable
from .models import NotificationOutboxRecord, RouteDecisionRecord

DUE = (OutboxStatus.PENDING.value, OutboxStatus.FAILED.value)


def _to_item(row: NotificationOutboxRecord) -> OutboxItem:
    return OutboxItem(
        id=row.id,
        run_id=row.run_id,
        goal_id=row.goal_id,
        event_id=row.event_id,
        kind=NotificationKind(row.kind),
        recipient=row.recipient,
        subject=row.subject,
        text_body=row.text_body,
        html_body=row.html_body,
        dedup_key=row.dedup_key,
        message_id=row.message_id,
        status=OutboxStatus(row.status),
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        created_at=row.created_at,
        sent_at=row.sent_at,
        lease_until=row.lease_until,
    )


def outbox_rows(drafts: Sequence[OutboxDraft]) -> list[dict[str, Any]]:
    return [
        {
            "run_id": d.run_id,
            "goal_id": d.goal_id,
            "event_id": d.event_id,
            "kind": d.kind.value,
            "recipient": d.recipient,
            "subject": d.subject,
            "text_body": d.text_body,
            "html_body": d.html_body,
            "dedup_key": d.dedup_key,
            "message_id": d.message_id,
        }
        for d in drafts
    ]


async def insert_outbox(session: AsyncSession, drafts: Sequence[OutboxDraft]) -> int:
    """Insert drafts in the caller's transaction; duplicates (same dedup key) are ignored."""
    if not drafts:
        return 0
    stmt = (
        pg_insert(NotificationOutboxRecord)
        .values(outbox_rows(drafts))
        .on_conflict_do_nothing(constraint="uq_notification_outbox_dedup_key")
        .returning(NotificationOutboxRecord.id)
    )
    return len((await session.execute(stmt)).scalars().all())


class SqlAlchemyNotificationOutbox:
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

    async def claim(self, *, now: datetime, limit: int, lease_seconds: float) -> list[OutboxItem]:
        candidates = (
            select(NotificationOutboxRecord.id)
            .where(
                or_(
                    and_(
                        NotificationOutboxRecord.status.in_(DUE),
                        NotificationOutboxRecord.next_attempt_at <= now,
                    ),
                    and_(
                        NotificationOutboxRecord.status == OutboxStatus.SENDING.value,
                        NotificationOutboxRecord.lease_until < now,
                    ),
                )
            )
            .order_by(NotificationOutboxRecord.next_attempt_at, NotificationOutboxRecord.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("due")
        )
        async with self._tx() as session:
            rows = (
                (
                    await session.execute(
                        update(NotificationOutboxRecord)
                        .where(NotificationOutboxRecord.id == candidates.c.id)
                        .values(
                            status=OutboxStatus.SENDING.value,
                            attempts=NotificationOutboxRecord.attempts + 1,
                            lease_until=now + timedelta(seconds=lease_seconds),
                        )
                        .returning(NotificationOutboxRecord)
                    )
                )
                .scalars()
                .all()
            )
            return [_to_item(row) for row in rows]

    async def mark_sent(self, item_id: int, *, now: datetime) -> bool:
        async with self._tx() as session:
            updated = (
                await session.execute(
                    update(NotificationOutboxRecord)
                    .where(
                        NotificationOutboxRecord.id == item_id,
                        NotificationOutboxRecord.status == OutboxStatus.SENDING.value,
                    )
                    .values(
                        status=OutboxStatus.SENT.value,
                        sent_at=now,
                        lease_until=None,
                        last_error=None,
                    )
                    .returning(NotificationOutboxRecord.id)
                )
            ).scalar_one_or_none()
            return updated is not None

    async def mark_failed(
        self,
        item_id: int,
        *,
        now: datetime,
        error: str,
        next_attempt_at: datetime | None,
        dead: bool,
    ) -> bool:
        async with self._tx() as session:
            updated = (
                await session.execute(
                    update(NotificationOutboxRecord)
                    .where(
                        NotificationOutboxRecord.id == item_id,
                        NotificationOutboxRecord.status == OutboxStatus.SENDING.value,
                    )
                    .values(
                        status=OutboxStatus.DEAD.value if dead else OutboxStatus.FAILED.value,
                        next_attempt_at=next_attempt_at or now,
                        lease_until=None,
                        last_error=error[:512],
                    )
                    .returning(NotificationOutboxRecord.id)
                )
            ).scalar_one_or_none()
            return updated is not None

    async def list_items(
        self, *, statuses: Sequence[OutboxStatus] | None = None, limit: int = 100
    ) -> list[OutboxItem]:
        stmt = select(NotificationOutboxRecord).order_by(NotificationOutboxRecord.id.desc())
        if statuses:
            stmt = stmt.where(NotificationOutboxRecord.status.in_([s.value for s in statuses]))
        async with self._tx() as session:
            rows = (await session.scalars(stmt.limit(limit))).all()
            return [_to_item(row) for row in rows]

    async def requeue(self, item_id: int, *, now: datetime) -> bool:
        async with self._tx() as session:
            updated = (
                await session.execute(
                    update(NotificationOutboxRecord)
                    .where(
                        NotificationOutboxRecord.id == item_id,
                        NotificationOutboxRecord.status.in_(
                            [OutboxStatus.DEAD.value, OutboxStatus.FAILED.value]
                        ),
                    )
                    .values(status=OutboxStatus.PENDING.value, next_attempt_at=now, last_error=None)
                    .returning(NotificationOutboxRecord.id)
                )
            ).scalar_one_or_none()
            return updated is not None

    async def counts(self) -> dict[str, int]:
        async with self._tx() as session:
            rows = (
                await session.execute(
                    select(NotificationOutboxRecord.status, func.count()).group_by(
                        NotificationOutboxRecord.status
                    )
                )
            ).all()
            return {str(status): int(count) for status, count in rows}


class SqlAlchemyRouteDecisionRepository:
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

    async def record(self, run_id: UUID, decision: RouteDecision) -> int:
        async with self._tx() as session:
            row = RouteDecisionRecord(
                run_id=run_id,
                route=decision.route.value,
                mode=decision.mode.value,
                reason=decision.reason,
                policy_version=decision.policy_version,
                confidence=decision.confidence,
                classifier_used=decision.classifier_used,
                mandatory_rules=list(decision.mandatory_rules),
                usage=dict(decision.usage),
                escalated_from=decision.escalated_from.value if decision.escalated_from else None,
                decided_at=decision.decided_at,
            )
            session.add(row)
            await session.flush()
            return int(row.id)

    async def list_for_run(self, run_id: UUID) -> list[RouteDecision]:
        async with self._tx() as session:
            rows = await session.scalars(
                select(RouteDecisionRecord)
                .where(RouteDecisionRecord.run_id == run_id)
                .order_by(RouteDecisionRecord.id)
            )
            return [
                RouteDecision(
                    route=Route(r.route),
                    mode=RouteMode(r.mode),
                    reason=r.reason,
                    policy_version=r.policy_version,
                    confidence=r.confidence,
                    classifier_used=r.classifier_used,
                    mandatory_rules=list(r.mandatory_rules or []),
                    usage=dict(r.usage or {}),
                    escalated_from=Route(r.escalated_from) if r.escalated_from else None,
                    decided_at=r.decided_at,
                )
                for r in rows
            ]

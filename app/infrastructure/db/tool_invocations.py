"""PostgreSQL implementation of ``ToolInvocationRepository`` (SQLAlchemy 2.0 async).

Same rules as the run repository: one short transaction per method,
connection failures become ``DatabaseUnavailableError``, integrity violations
are handled where they are part of the contract (the start claim) and
propagate otherwise.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.tools import AgentKind, InvocationStatus, RiskClass
from app.tools.ports import InvocationCompletion, ToolInvocation

from .errors import as_unavailable
from .models import ToolInvocationRecord

_UNIQUE_STEP = "uq_tool_invocations_run_id_step_attempt"


def _to_invocation(record: ToolInvocationRecord) -> ToolInvocation:
    return ToolInvocation(
        id=record.id,
        run_id=record.run_id,
        step=record.step,
        attempt=record.attempt,
        tool_name=record.tool_name,
        tool_version=record.tool_version,
        risk=RiskClass(record.risk) if record.risk is not None else None,
        agent=AgentKind(record.agent),
        actor_subject=record.actor_subject,
        project_id=record.project_id,
        status=record.status,
        error_code=record.error_code,
        input_digest=record.input_digest,
        output_digest=record.output_digest,
        output_bytes=record.output_bytes,
        duration_ms=record.duration_ms,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


class SqlAlchemyToolInvocationRepository:
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

    async def find_step(self, run_id: UUID, step: str) -> list[ToolInvocation]:
        async with self._tx() as session:
            rows = (
                await session.execute(
                    select(ToolInvocationRecord)
                    .where(ToolInvocationRecord.run_id == run_id, ToolInvocationRecord.step == step)
                    .order_by(ToolInvocationRecord.attempt)
                )
            ).scalars()
            return [_to_invocation(row) for row in rows]

    async def start(self, invocation: ToolInvocation) -> bool:
        values = {
            "id": invocation.id,
            "run_id": invocation.run_id,
            "step": invocation.step,
            "attempt": invocation.attempt,
            "tool_name": invocation.tool_name,
            "tool_version": invocation.tool_version,
            "risk": invocation.risk.value if invocation.risk is not None else None,
            "agent": invocation.agent.value,
            "actor_subject": invocation.actor_subject,
            "project_id": invocation.project_id,
            "status": invocation.status,
            "error_code": invocation.error_code,
            "input_digest": invocation.input_digest,
            "output_digest": invocation.output_digest,
            "output_bytes": invocation.output_bytes,
            "duration_ms": invocation.duration_ms,
            "finished_at": invocation.finished_at,
        }
        if invocation.started_at is not None:
            values["started_at"] = invocation.started_at
        async with self._tx() as session:
            stmt = (
                pg_insert(ToolInvocationRecord)
                .values(**values)
                .on_conflict_do_nothing(constraint=_UNIQUE_STEP)
                .returning(ToolInvocationRecord.id)
            )
            inserted = (await session.execute(stmt)).scalar_one_or_none()
            return inserted is not None

    async def transition(
        self,
        invocation_id: UUID,
        *,
        expected: InvocationStatus,
        status: InvocationStatus,
        completion: InvocationCompletion | None = None,
    ) -> bool:
        values: dict[str, object] = {"status": status}
        if completion is not None:
            values.update(
                error_code=completion.error_code,
                output_digest=completion.output_digest,
                output_bytes=completion.output_bytes,
                duration_ms=completion.duration_ms,
                finished_at=completion.finished_at,
            )
        async with self._tx() as session:
            result = await session.execute(
                update(ToolInvocationRecord)
                .where(
                    ToolInvocationRecord.id == invocation_id,
                    ToolInvocationRecord.status == expected,
                )
                .values(**values)
                .returning(ToolInvocationRecord.id)
            )
            return result.scalar_one_or_none() is not None

    async def list_for_run(self, run_id: UUID, *, limit: int = 100) -> list[ToolInvocation]:
        async with self._tx() as session:
            rows = (
                await session.execute(
                    select(ToolInvocationRecord)
                    .where(ToolInvocationRecord.run_id == run_id)
                    .order_by(
                        ToolInvocationRecord.attempt,
                        ToolInvocationRecord.started_at,
                        ToolInvocationRecord.step,
                    )
                    .limit(limit)
                )
            ).scalars()
            return [_to_invocation(row) for row in rows]

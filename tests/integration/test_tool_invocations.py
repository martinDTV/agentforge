"""Tool invocation audit on PostgreSQL: migration 0004, the repository and the gateway."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.contracts.tools import AgentKind, InvocationStatus, ToolCall
from app.infrastructure.db import (
    Database,
    SqlAlchemyRunRepository,
    SqlAlchemyToolInvocationRepository,
)
from app.security import AuthorizationPolicy, AutomationIdentity, ToolAuthorizationPolicy
from app.tools import ToolContext, ToolGateway, ToolRegistry, invocation_id
from app.tools.fake import EchoTool
from app.tools.gateway import digest_arguments
from app.tools.ports import InvocationCompletion, ToolInvocation
from tests.integration.conftest import downgrade, migrate, sqlalchemy_url
from tests.unit.tool_fixtures import TEST_GRANTS

pytestmark = pytest.mark.anyio


def test_migration_0004_creates_the_audit_table_with_its_constraints(empty_database_url: str):
    migrate(empty_database_url)
    engine = create_engine(sqlalchemy_url(empty_database_url))
    inspector = inspect(engine)
    assert "tool_invocations" in inspector.get_table_names()
    uniques = {
        u["name"]: u["column_names"] for u in inspector.get_unique_constraints("tool_invocations")
    }
    assert uniques["uq_tool_invocations_run_id_step_attempt"] == ["run_id", "step", "attempt"]
    checks = {c["name"] for c in inspector.get_check_constraints("tool_invocations")}
    assert {
        "ck_tool_invocations_finished_when_closed",
        "ck_tool_invocations_attempt_non_negative",
        "ck_tool_invocations_step_not_blank",
    } <= checks
    fks = inspector.get_foreign_keys("tool_invocations")
    assert fks[0]["referred_table"] == "runs" and fks[0]["options"].get("ondelete") == "CASCADE"
    downgrade(empty_database_url, "0003")
    assert "tool_invocations" not in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0003"
    engine.dispose()


async def _run(repository: SqlAlchemyRunRepository, project: str = "alpha") -> uuid.UUID:
    run_id = uuid.uuid4()
    await repository.create_run(
        run_id, "tarea", owner="proc", ttl_seconds=60, owner_subject="alice", project_id=project
    )
    return run_id


def _row(
    run_id: uuid.UUID, step: str, attempt: int, status: InvocationStatus = InvocationStatus.PENDING
) -> ToolInvocation:
    now = datetime.now(UTC)
    closed = status not in (InvocationStatus.PENDING, InvocationStatus.RUNNING)
    return ToolInvocation(
        id=invocation_id(run_id, step, attempt),
        run_id=run_id,
        step=step,
        attempt=attempt,
        tool_name="fake.echo",
        tool_version="1",
        agent=AgentKind.DEVELOPER,
        actor_subject="agentforge:developer",
        project_id="alpha",
        status=status,
        error_code="tool_not_granted" if status is InvocationStatus.DENIED else None,
        input_digest=digest_arguments({"message": "hola"}),
        started_at=now,
        finished_at=now if closed else None,
    )


async def test_repository_claims_steps_and_moves_rows_conditionally(
    fresh_migrated_database_url: str,
):
    database = Database(fresh_migrated_database_url)
    try:
        runs = SqlAlchemyRunRepository(database.session_factory)
        audit = SqlAlchemyToolInvocationRepository(database.session_factory)
        run_id = await _run(runs)
        first = _row(run_id, "s", 0)
        assert await audit.start(first) is True
        assert await audit.start(first) is False  # same (run, step, attempt)
        assert await audit.start(_row(run_id, "s", 1, InvocationStatus.DENIED)) is True
        done = InvocationCompletion(
            finished_at=datetime.now(UTC), output_digest="f" * 64, output_bytes=12, duration_ms=3
        )
        assert (
            await audit.transition(
                first.id,
                expected=InvocationStatus.RUNNING,
                status=InvocationStatus.OK,
                completion=done,
            )
            is False
        )  # still pending
        assert await audit.transition(
            first.id, expected=InvocationStatus.PENDING, status=InvocationStatus.RUNNING
        )
        assert await audit.transition(
            first.id, expected=InvocationStatus.RUNNING, status=InvocationStatus.OK, completion=done
        )
        assert (
            await audit.transition(
                first.id,
                expected=InvocationStatus.RUNNING,
                status=InvocationStatus.OK,
                completion=done,
            )
            is False
        )  # already closed
        rows = await audit.find_step(run_id, "s")
        assert [(r.attempt, r.status) for r in rows] == [
            (0, InvocationStatus.OK),
            (1, InvocationStatus.DENIED),
        ]
        assert rows[0].output_digest == "f" * 64 and rows[0].finished_at is not None
        assert await audit.find_step(run_id, "other") == []
        # A closed row must carry a finish time: the database enforces it.
        with pytest.raises(IntegrityError):
            await audit.start(
                _row(run_id, "s", 2, InvocationStatus.DENIED).model_copy(
                    update={"finished_at": None}
                )
            )
        # Deleting the run cascades to its audit rows.
        async with database.session_factory() as session, session.begin():
            await session.execute(text("DELETE FROM runs WHERE id = :id"), {"id": run_id})
        assert await audit.list_for_run(run_id) == []
    finally:
        await database.dispose()


async def test_gateway_audits_on_postgres_end_to_end(fresh_migrated_database_url: str):
    database = Database(fresh_migrated_database_url)
    try:
        runs = SqlAlchemyRunRepository(database.session_factory)
        audit = SqlAlchemyToolInvocationRepository(database.session_factory)
        run_id = await _run(runs)
        gateway = ToolGateway(
            ToolRegistry([EchoTool()]),
            ToolAuthorizationPolicy(AuthorizationPolicy(), TEST_GRANTS),
            audit,
        )
        identity = AutomationIdentity()
        context = ToolContext(
            run_id=run_id,
            attempt=1,
            project_id="alpha",
            agent=AgentKind.DEVELOPER,
            principal=identity.principal_for(AgentKind.DEVELOPER, "alpha"),
        )
        # A previous attempt died while running this very step.
        crashed = _row(run_id, "read", 0)
        assert await audit.start(crashed)
        assert await audit.transition(
            crashed.id, expected=InvocationStatus.PENDING, status=InvocationStatus.RUNNING
        )
        ok = await gateway.invoke(
            context, ToolCall(name="fake.echo", arguments={"message": "hola"}), step="read"
        )
        denied = await gateway.invoke(context, ToolCall(name="nope.tool"), step="unknown")
        planner = context.model_copy(
            update={
                "agent": AgentKind.PLANNER,
                "principal": identity.principal_for(AgentKind.PLANNER, "alpha"),
            }
        )
        refused = await gateway.invoke(
            planner, ToolCall(name="fake.echo", arguments={"message": "x"}), step="plan"
        )
        assert ok.ok and not denied.ok and not refused.ok
        rows = await audit.list_for_run(run_id)
        assert [(r.attempt, r.step, r.status, r.error_code) for r in rows] == [
            (0, "read", InvocationStatus.UNCERTAIN, "process_lost"),
            (1, "read", InvocationStatus.OK, None),
            (1, "unknown", InvocationStatus.DENIED, "tool_not_registered"),
            (1, "plan", InvocationStatus.DENIED, "tool_not_granted"),
        ]
        assert rows[1].actor_subject == "agentforge:developer"
        assert rows[3].actor_subject == "agentforge:planner"
        assert all(r.finished_at is not None for r in rows)
        async with database.session_factory() as session:
            stored = (
                await session.execute(
                    text(
                        "SELECT tool_name, risk FROM tool_invocations ORDER BY attempt, started_at"
                    )
                )
            ).all()
        assert stored == [
            ("fake.echo", None),
            ("fake.echo", "read"),
            ("nope.tool", None),
            ("fake.echo", "read"),
        ]
    finally:
        await database.dispose()

"""Development workflow (Fase 6) on PostgreSQL: run row, events, tool audit,
checkpoints and the conservative recovery of an interrupted development run."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.agents.development import DeveloperChanges, FileEdit, QAReview
from app.contracts.lifecycle import RunEventKind, RunLifecycleStatus, WorkflowKind
from app.contracts.tools import AgentKind, InvocationStatus, RiskClass
from app.infrastructure.checkpoints import PostgresCheckpointer
from app.infrastructure.db import (
    Database,
    SqlAlchemyRunRepository,
    SqlAlchemyToolInvocationRepository,
)
from app.infrastructure.llm.budget import BudgetedLLM
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.infrastructure.sandbox import FakeSandbox
from app.orchestration.development import DevelopmentDeps, build_development_graph
from app.orchestration.graph import build_graph
from app.orchestration.policy import RecoveryPolicy
from app.orchestration.runs import RecoveryOutcome, RunService
from app.projects import ProjectPolicyRegistry, RepositoryPolicy
from app.security import AuthorizationPolicy, Principal
from app.security.development import DevelopmentScope, DevelopmentToolPolicy
from app.tools import ExecRunTool, GitTools, ToolGateway, ToolLimits, default_registry
from app.tools.write_file import WriteFileTool
from tests.integration.conftest import libpq_dsn
from tests.unit.git_fixtures import make_sandbox

pytestmark = pytest.mark.anyio
PROJECT = "sandbox-it"


def operator_principal(project_id: str) -> Principal:
    """A local operator who may create and audit runs in one sandbox project."""
    return Principal(
        subject="operator:development-mvp",
        kind="user",
        issuer="agentforge:operator",
        roles=frozenset({"project_operator"}),
        projects=frozenset({project_id}),
    )


REPOSITORY = "sandbox-it-repo"
S = RunLifecycleStatus


def components(tmp_path: Path, saver, audit):
    box = make_sandbox(tmp_path, project=PROJECT, repository=REPOSITORY)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, PROJECT, REPOSITORY)
    (record.path / "tests").mkdir()
    (record.path / "tests/test_app.py").write_text("def test_ok():\n    assert True\n")
    scope = DevelopmentScope(
        run_id,
        PROJECT,
        REPOSITORY,
        record.path,
        datetime.now(UTC) + timedelta(minutes=5),
        frozenset({"src/app.py"}),
    )
    sandbox = FakeSandbox(runner_id="fake-runner")
    limits = ToolLimits()
    registry = default_registry(
        limits, GitTools(box.adapter, box.manager, limits), ExecRunTool(sandbox, box.manager)
    )
    registry.register(WriteFileTool(box.manager, scope))
    gateway = ToolGateway(registry, DevelopmentToolPolicy(scope), audit)
    model = FakeLLMAdapter()
    model.register(
        DeveloperChanges,
        DeveloperChanges(
            summary="implementado", edits=[FileEdit(path="src/app.py", content="VALUE = 1\n")]
        ),
    )
    model.register(QAReview, QAReview(approved=True, summary="correcto", issues=[]))
    deps = DevelopmentDeps(scope, gateway, BudgetedLLM(model), ("src/app.py", "tests/test_app.py"))
    return run_id, record, build_development_graph(deps, checkpointer=saver), model, sandbox


async def test_a_development_run_is_persisted_end_to_end(
    tmp_path: Path, checkpoint_database_url: str
):
    url = checkpoint_database_url
    database = Database(url)
    checkpointer = PostgresCheckpointer(libpq_dsn(url))
    saver = await checkpointer.open(wait=True)
    try:
        repository = SqlAlchemyRunRepository(database.session_factory)
        audit = SqlAlchemyToolInvocationRepository(database.session_factory)
        run_id, record, graph, model, sandbox = components(tmp_path, saver, audit)
        service = RunService(
            graph,
            repository,
            RecoveryPolicy(),
            authorization=AuthorizationPolicy(separation_of_duties=False),
            workflow_kind=WorkflowKind.DEVELOPMENT,
            policies=ProjectPolicyRegistry(
                [
                    RepositoryPolicy(
                        repository_id=REPOSITORY, project_id=PROJECT, writable_globs=["src/**"]
                    )
                ]
            ),
        )
        result = await service.create(
            "implement value",
            operator_principal(PROJECT),
            project_id=PROJECT,
            run_id=str(run_id),
            repository_id=REPOSITORY,
        )
        await service.shutdown(1.0)
        assert result.created and result.response.status is S.COMPLETED
        assert result.response.workflow_kind is WorkflowKind.DEVELOPMENT
        row = await repository.get_run(run_id)
        assert row is not None and row.status is S.COMPLETED and row.lease_owner is None
        assert row.workflow_kind is WorkflowKind.DEVELOPMENT and row.llm_calls == 2
        assert row.idempotency_key is None
        assert row.owner_subject == "operator:development-mvp" and row.project_id == PROJECT
        kinds = [e.kind for e in await repository.list_events(run_id, after_id=None, limit=100)]
        assert kinds[0] is RunEventKind.RUN_CREATED
        for kind in (
            RunEventKind.DEVELOPER_COMPLETED,
            RunEventKind.DEVELOPER_APPLIED,
            RunEventKind.QA_REVIEWED,
            RunEventKind.RUN_COMPLETED,
        ):
            assert kind in kinds, kind
        rows = await audit.list_for_run(run_id)
        assert rows and all(r.status is InvocationStatus.OK for r in rows)
        assert {r.risk for r in rows} == {RiskClass.READ, RiskClass.WRITE, RiskClass.EXEC}
        assert {r.agent for r in rows} == {AgentKind.DEVELOPER, AgentKind.QA}
        assert len(sandbox.requests) == 5 and len(model.calls) == 2
        snapshot = await graph.aget_state({"configurable": {"thread_id": str(run_id)}})
        assert snapshot.values["completed"] is True and "approved" not in snapshot.values
        assert snapshot.values["changed_files"] == ["src/app.py"]
        assert snapshot.values["workflow_kind"] == WorkflowKind.DEVELOPMENT.value
        assert (record.path / "src/app.py").read_text() == "VALUE = 1\n"
    finally:
        await checkpointer.close()
        await database.dispose()


async def test_an_abandoned_development_run_is_failed_never_redriven(
    tmp_path: Path, checkpoint_database_url: str, fake_llm: FakeLLMAdapter
):
    """Even a sweeper with auto-resume and a planning graph must not re-drive it."""
    url = checkpoint_database_url
    database = Database(url)
    checkpointer = PostgresCheckpointer(libpq_dsn(url))
    saver = await checkpointer.open(wait=True)
    try:
        repository = SqlAlchemyRunRepository(database.session_factory)
        run_id = uuid.uuid4()
        created = await repository.create_run(
            run_id,
            "implement value",
            owner="dead-process",
            ttl_seconds=0.2,
            owner_subject="operator:development-mvp",
            project_id=PROJECT,
            workflow_kind=WorkflowKind.DEVELOPMENT,
        )
        assert created.created
        await asyncio.sleep(0.5)  # the lease expires without a heartbeat
        service = RunService(
            build_graph(fake_llm, checkpointer=saver),  # the API's planning graph
            repository,
            RecoveryPolicy(auto_resume=True, max_attempts=3),
            owner="sweeper",
            authorization=AuthorizationPolicy(separation_of_duties=False),
        )
        acquired = await repository.acquire_lease(
            run_id, owner="sweeper", ttl_seconds=30, from_statuses=frozenset({S.RUNNING})
        )
        assert acquired is not None
        claimed, lease = acquired
        assert await service.recover(claimed, lease) is RecoveryOutcome.ABANDONED
        row = await repository.get_run(run_id)
        assert row is not None and row.status is S.FAILED and row.lease_owner is None
        assert row.error is not None and row.error.code == "development_interrupted"
        assert fake_llm.calls == []  # no planner call, no billable work
        kinds = [e.kind for e in await repository.list_events(run_id, after_id=None, limit=50)]
        assert RunEventKind.RUN_FAILED in kinds and RunEventKind.RUN_RESUMED not in kinds
    finally:
        await checkpointer.close()
        await database.dispose()


async def test_recovery_reads_a_development_checkpoint_with_the_planning_graph(
    tmp_path: Path, checkpoint_database_url: str, fake_llm: FakeLLMAdapter
):
    """A finished development thread must project correctly under the API's graph,
    and an unfinished one must be closed, never re-driven."""
    url = checkpoint_database_url
    database = Database(url)
    checkpointer = PostgresCheckpointer(libpq_dsn(url))
    saver = await checkpointer.open(wait=True)
    try:
        repository = SqlAlchemyRunRepository(database.session_factory)
        audit = SqlAlchemyToolInvocationRepository(database.session_factory)
        sweeper = RunService(
            build_graph(fake_llm, checkpointer=saver),
            repository,
            RecoveryPolicy(auto_resume=True, max_attempts=3),
            owner="sweeper",
            authorization=AuthorizationPolicy(separation_of_duties=False),
        )

        async def abandoned_row(run_id: uuid.UUID) -> None:
            created = await repository.create_run(
                run_id,
                "implement value",
                owner="dead-process",
                ttl_seconds=0.2,
                owner_subject="operator:development-mvp",
                project_id=PROJECT,
                workflow_kind=WorkflowKind.DEVELOPMENT,
            )
            assert created.created

        # Finished thread: the dead process completed the graph but never projected.
        (tmp_path / "done").mkdir()
        run_id, record, graph, model, sandbox = components(tmp_path / "done", saver, audit)
        await abandoned_row(run_id)
        await graph.ainvoke(
            {"task": "implement value", "workflow_kind": "development"},
            {"configurable": {"thread_id": str(run_id)}},
        )
        await asyncio.sleep(0.5)
        acquired = await repository.acquire_lease(
            run_id, owner="sweeper", ttl_seconds=30, from_statuses=frozenset({S.RUNNING})
        )
        assert acquired is not None
        assert await sweeper.recover(*acquired) is RecoveryOutcome.REPROJECTED
        row = await repository.get_run(run_id)
        assert row is not None and row.status is S.COMPLETED and row.lease_owner is None
        kinds = [e.kind for e in await repository.list_events(run_id, after_id=None, limit=50)]
        assert RunEventKind.RUN_COMPLETED in kinds and RunEventKind.QA_REVIEWED in kinds

        # Unfinished thread: a checkpoint after the Developer node, then the process died.
        (tmp_path / "half").mkdir()
        run_id, record, graph, model, sandbox = components(tmp_path / "half", saver, audit)
        sandbox.delay_seconds = 5.0
        await abandoned_row(run_id)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                graph.ainvoke(
                    {"task": "implement value", "workflow_kind": "development"},
                    {"configurable": {"thread_id": str(run_id)}},
                ),
                timeout=0.5,
            )
        snapshot = await graph.aget_state({"configurable": {"thread_id": str(run_id)}})
        assert "changes" in snapshot.values and "completed" not in snapshot.values
        acquired = await repository.acquire_lease(
            run_id, owner="sweeper", ttl_seconds=30, from_statuses=frozenset({S.RUNNING})
        )
        assert acquired is not None
        assert await sweeper.recover(*acquired) is RecoveryOutcome.ABANDONED
        row = await repository.get_run(run_id)
        assert row is not None and row.status is S.FAILED
        assert row.error is not None and row.error.code == "development_interrupted"
        assert fake_llm.calls == [] and len(model.calls) == 1
    finally:
        await checkpointer.close()
        await database.dispose()

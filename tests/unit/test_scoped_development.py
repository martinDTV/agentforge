"""Governed development workflow (Fase 7, Paso 1) with fakes: a development run
blocks until an authorised human approves an exact scope, and Developer cannot
write outside it. No runner, no PostgreSQL, no model."""

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.agents.development import DeveloperChanges, FileEdit, QAReview
from app.contracts.lifecycle import DecisionStage, RunEventKind, RunLifecycleStatus, WorkflowKind
from app.contracts.scope import ScopeAction, ScopeFile, ScopeProposal
from app.contracts.tools import AgentKind, RiskClass, ToolCall
from app.infrastructure.db import InMemoryRunRepository, InMemoryToolInvocationRepository
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.infrastructure.sandbox import FakeSandbox
from app.orchestration.exceptions import (
    DecisionExpiredError,
    DecisionStageMismatchError,
    DecisionStaleError,
    RepositoryNotAuthorizedError,
    RepositoryRequiredError,
    RunAlreadyDecidedError,
    RunNotCancellableError,
    WorkflowUnavailableError,
)
from app.orchestration.graph import build_graph
from app.orchestration.ports import PersistedRun
from app.orchestration.runs import RunService
from app.orchestration.scoped_development import DevelopmentRuntime, build_scoped_development_graph
from app.projects import ProjectPolicyRegistry, RepositoryPolicy
from app.sandbox import ProgramId, SandboxResult, SandboxStatus
from app.security import AuthorizationPolicy, AutomationIdentity, PermissionDeniedError, Principal
from app.security.development import DevelopmentScope
from app.tools import ToolContext
from tests.unit.git_fixtures import make_sandbox

pytestmark = pytest.mark.anyio

PROJECT = "sandbox-gov"
REPO = "sandbox-gov"
TASK = "Añade la función greet(name) en src/app.py y pruebas en tests/test_greet.py"
S = RunLifecycleStatus


def who(subject: str, roles: set[str], projects: set[str] | None = None) -> Principal:
    return Principal(
        subject=subject,
        kind="user",
        issuer="https://issuer.test",
        roles=frozenset(roles),
        projects=frozenset(projects or {PROJECT}),
    )


OPERATOR = who("olivia", {"project_operator"})  # may create and approve scope
APPROVER = who("alice", {"approver"})  # may approve runs but not scopes
VIEWER = who("victor", {"viewer"})
PLAIN_OPERATOR = who("oscar", {"operator"})  # creates planning runs, no repos:write
AGENT = AutomationIdentity().principal_for(AgentKind.DEVELOPER, PROJECT)


class World:
    def __init__(self, tmp_path: Path, *, clock=None, policy_overrides=None) -> None:
        self.box = make_sandbox(tmp_path, project=PROJECT, repository=REPO)
        self.sandbox = FakeSandbox(runner_id="fake-runner")
        self.audit = InMemoryToolInvocationRepository()
        self.repository = InMemoryRunRepository()
        self.model = FakeLLMAdapter()
        self.model.register(
            ScopeProposal,
            ScopeProposal(
                summary="editar src/app.py y crear tests/test_greet.py",
                files=[
                    ScopeFile(path="src/app.py", action=ScopeAction.EDIT, reason="implementar"),
                    ScopeFile(
                        path="tests/test_greet.py", action=ScopeAction.CREATE, reason="probar"
                    ),
                    ScopeFile(path="README.md", action=ScopeAction.EDIT, reason="documentar"),
                ],
                tests=["tests"],
            ),
        )
        self.model.register(
            DeveloperChanges,
            DeveloperChanges(
                summary="implementado",
                edits=[
                    FileEdit(path="src/app.py", content="def greet(n):\n    return f'hi {n}'\n"),
                    FileEdit(path="tests/test_greet.py", content="def test_ok():\n    assert 1\n"),
                ],
            ),
        )
        self.model.register(QAReview, QAReview(approved=True, summary="ok", issues=[]))
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        self.policy = RepositoryPolicy(
            repository_id=REPO,
            project_id=PROJECT,
            writable_globs=["src/**", "tests/**"],
            scope_approval_ttl_seconds=3600,
            **(policy_overrides or {}),
        )
        self.policies = ProjectPolicyRegistry([self.policy])
        self.runtime = DevelopmentRuntime(
            policies=self.policies,
            workspaces=self.box.manager,
            git=self.box.adapter,
            sandbox=self.sandbox,
            audit=self.audit,
            llm=self.model,
            clock=clock or (lambda: self.now),
        )
        self.dev_graph = build_scoped_development_graph(self.runtime)
        self.released: list[tuple[uuid.UUID, S]] = []

    def service(self, *, owner: str = "api-1", separation_of_duties: bool = False) -> RunService:
        async def on_terminal(row: PersistedRun) -> None:
            self.released.append((row.id, row.status))
            await self.runtime.release(row.id, row.project_id, row.status)

        return RunService(
            build_graph(self.model),
            self.repository,
            owner=owner,
            authorization=AuthorizationPolicy(separation_of_duties=separation_of_duties),
            graphs={
                WorkflowKind.PLANNING: build_graph(self.model),
                WorkflowKind.DEVELOPMENT: self.dev_graph,
            },
            policies=self.policies,
            on_terminal=on_terminal,
            clock=lambda: self.now,
        )


async def start(world: World, service: RunService, principal: Principal = OPERATOR) -> str:
    result = await service.create(
        TASK,
        principal,
        project_id=PROJECT,
        workflow_kind=WorkflowKind.DEVELOPMENT,
        repository_id=REPO,
    )
    return result.response.run_id


async def test_a_development_run_blocks_on_scope_approval_without_writing_anything(tmp_path):
    world = World(tmp_path)
    service = world.service()
    run_id = await start(world, service)
    response = await service.get(run_id, OPERATOR)
    assert response.status is S.WAITING_APPROVAL
    assert response.pending_stage is DecisionStage.SCOPE
    assert response.approval_deadline == world.now + timedelta(hours=1)
    assert response.workflow_kind is WorkflowKind.DEVELOPMENT and response.llm_calls == 1
    view = await service.decisions(run_id, OPERATOR)
    assert view.pending is not None and view.pending.stage is DecisionStage.SCOPE
    files = {f["path"]: f["action"] for f in view.pending.payload["files"]}
    assert files == {"src/app.py": "edit", "tests/test_greet.py": "create"}
    assert [r["path"] for r in view.pending.payload["rejected"]] == ["README.md"]
    assert view.decisions == []
    # Only the Scoper acted, only reading, and the worktree is untouched.
    rows = await world.audit.list_for_run(uuid.UUID(run_id))
    assert {r.agent for r in rows} == {AgentKind.SCOPER}
    assert {r.risk for r in rows} == {RiskClass.READ}
    worktree = world.box.manager.get(uuid.UUID(run_id), PROJECT)
    assert worktree is not None and (worktree.path / "src/app.py").read_text() == "print('hi')\n"
    assert not (worktree.path / "tests/test_greet.py").exists()
    kinds = [
        e.kind
        for e in await world.repository.list_events(uuid.UUID(run_id), after_id=None, limit=50)
    ]
    assert RunEventKind.SCOPE_PROPOSED in kinds and RunEventKind.WAITING_APPROVAL in kinds
    assert world.model.calls[0][2] is ScopeProposal


async def test_approval_must_name_the_exact_scope_and_needs_repos_write(tmp_path):
    world = World(tmp_path)
    service = world.service()
    run_id = await start(world, service)
    digest = (await service.decisions(run_id, OPERATOR)).pending.scope_digest  # type: ignore[union-attr]
    with pytest.raises(DecisionStaleError):
        await service.approve(run_id, True, OPERATOR)  # no digest at all
    with pytest.raises(DecisionStaleError):
        await service.approve(run_id, True, OPERATOR, scope_digest="0" * 64)  # obsolete
    with pytest.raises(DecisionStageMismatchError):
        await service.approve(
            run_id, True, OPERATOR, stage=DecisionStage.PUBLISH, scope_digest=digest
        )
    with pytest.raises(PermissionDeniedError):  # approver without repos:write
        await service.approve(run_id, True, APPROVER, scope_digest=digest)
    with pytest.raises(PermissionDeniedError):
        await service.approve(run_id, True, VIEWER, scope_digest=digest)
    assert (await service.get(run_id, OPERATOR)).status is S.WAITING_APPROVAL
    assert (await service.decisions(run_id, OPERATOR)).decisions == []
    # Agents are not visible principals at all.
    from app.orchestration.exceptions import RunNotFoundError

    with pytest.raises(RunNotFoundError):
        await service.approve(run_id, True, AGENT, scope_digest=digest)


async def test_approved_scope_becomes_developers_only_write_authority(tmp_path):
    world = World(tmp_path)
    service = world.service()
    run_id = await start(world, service)
    digest = (await service.decisions(run_id, OPERATOR)).pending.scope_digest  # type: ignore[union-attr]
    response = await service.approve(
        run_id, True, OPERATOR, stage=DecisionStage.SCOPE, scope_digest=digest
    )
    assert response.status is S.COMPLETED and response.pending_stage is None
    rid = uuid.UUID(run_id)
    worktree = world.box.manager.get(rid, PROJECT)
    assert worktree is not None
    assert (worktree.path / "src/app.py").read_text().startswith("def greet")
    assert (worktree.path / "tests/test_greet.py").is_file()
    assert (worktree.path / "README.md").read_text() == "# Demo\n"  # never in scope
    rows = await world.audit.list_for_run(rid)
    writes = [r for r in rows if r.risk is RiskClass.WRITE]
    assert {r.agent for r in writes} == {AgentKind.DEVELOPER} and len(writes) == 2
    assert all(r.status.value == "ok" for r in writes)
    decisions = (await service.decisions(run_id, OPERATOR)).decisions
    assert [(d.stage, d.approved, d.decided_by, d.scope_digest) for d in decisions] == [
        (DecisionStage.SCOPE, True, "olivia", digest)
    ]
    assert decisions[0].applied_at is not None
    events = await world.repository.list_events(rid, after_id=None, limit=100)
    recorded = next(e for e in events if e.kind is RunEventKind.APPROVAL_RECORDED)
    assert recorded.actor_subject == "olivia" and recorded.payload["stage"] == "scope"
    assert recorded.payload["scope_digest"] == digest
    applied = next(e for e in events if e.kind is RunEventKind.APPROVAL_APPLIED)
    assert applied.payload == {"approved": True, "stage": "scope", "approved_by": "olivia"}
    assert world.released and world.released[-1] == (rid, S.COMPLETED)
    # Second, identical decision replays; a contradictory one is refused.
    assert (
        await service.approve(run_id, True, OPERATOR, scope_digest=digest)
    ).status is S.COMPLETED
    with pytest.raises(RunAlreadyDecidedError):
        await service.approve(run_id, False, OPERATOR, scope_digest=digest)


async def test_developer_cannot_write_outside_the_approved_scope(tmp_path):
    world = World(tmp_path)
    # The model later tries to touch a file that is writable by policy but was
    # not part of the approved scope.
    world.model.register(
        DeveloperChanges,
        DeveloperChanges(
            summary="sneaky",
            edits=[
                FileEdit(path="src/app.py", content="x = 1\n"),
                FileEdit(path="src/other.py", content="print('no')\n"),
            ],
        ),
    )
    service = world.service()
    run_id = await start(world, service)
    digest = (await service.decisions(run_id, OPERATOR)).pending.scope_digest  # type: ignore[union-attr]
    response = await service.approve(run_id, True, OPERATOR, scope_digest=digest)
    assert response.status is S.FAILED and response.error is not None
    assert response.error.code == "path_denied"
    worktree = world.box.manager.get(uuid.UUID(run_id), PROJECT)
    assert worktree is not None and not (worktree.path / "src/other.py").exists()
    assert (worktree.path / "src/app.py").read_text() == "print('hi')\n"  # nothing written at all
    # And the gateway itself refuses a direct write outside the scope, even for
    # a path the policy would allow.
    scope = DevelopmentScope(
        uuid.UUID(run_id),
        PROJECT,
        REPO,
        worktree.path,
        world.now + timedelta(hours=1),
        frozenset({"src/app.py"}),
        clock=lambda: world.now,
    )
    gateway = world.runtime.write_gateway(scope)
    context = ToolContext(
        run_id=uuid.UUID(run_id),
        project_id=PROJECT,
        agent=AgentKind.DEVELOPER,
        principal=AutomationIdentity().principal_for(AgentKind.DEVELOPER, PROJECT),
        workspace_root=worktree.path,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.security.development.datetime", datetime)
        result = await gateway.invoke(
            context,
            ToolCall(
                name="workspace.write_file",
                arguments={"path": "src/other.py", "content": "x\n", "expected_sha256": None},
            ),
            step="direct",
        )
    assert result.error is not None and result.error.code == "path_denied"
    assert not (worktree.path / "src/other.py").exists()


async def test_rejection_cancellation_and_expiry_are_terminal_and_audited(tmp_path):
    world = World(tmp_path)
    service = world.service()
    # Rejection
    run_id = await start(world, service)
    digest = (await service.decisions(run_id, OPERATOR)).pending.scope_digest  # type: ignore[union-attr]
    rejected = await service.approve(run_id, False, OPERATOR, scope_digest=digest)
    assert rejected.status is S.REJECTED and len(world.model.calls) == 1  # no Developer call
    # Cancellation of a waiting run by someone with runs:cancel
    run_id = await start(world, service)
    cancelled = await service.cancel(run_id, OPERATOR)
    assert cancelled.status is S.CANCELLED and cancelled.pending_stage is None
    events = await world.repository.list_events(uuid.UUID(run_id), after_id=None, limit=50)
    cancel = next(e for e in events if e.kind is RunEventKind.RUN_CANCELLED)
    assert cancel.actor_subject == "olivia"
    with pytest.raises(RunNotCancellableError):
        await service.cancel(run_id, OPERATOR)  # already terminal
    with pytest.raises(PermissionDeniedError):
        await service.cancel(await start(world, service), VIEWER)
    assert (uuid.UUID(run_id), S.CANCELLED) in world.released
    # Expiry: the clock passes the deadline before anyone decides.
    run_id = await start(world, service)
    digest = (await service.decisions(run_id, OPERATOR)).pending.scope_digest  # type: ignore[union-attr]
    world.now += timedelta(hours=2)
    with pytest.raises(DecisionExpiredError):
        await service.approve(run_id, True, OPERATOR, scope_digest=digest)
    expired = await service.get(run_id, OPERATOR)
    assert expired.status is S.EXPIRED
    kinds = [
        e.kind
        for e in await world.repository.list_events(uuid.UUID(run_id), after_id=None, limit=50)
    ]
    assert RunEventKind.RUN_EXPIRED in kinds
    # The sweep path expires too.
    run_id = await start(world, service)
    world.now += timedelta(hours=2)
    assert [str(r.id) for r in await service.expire_waiting()] == [run_id]


async def test_creation_is_denied_without_permission_policy_or_repository(tmp_path):
    world = World(tmp_path)
    service = world.service()
    with pytest.raises(PermissionDeniedError):
        await start(world, service, PLAIN_OPERATOR)  # runs:create but no repos:write
    with pytest.raises(RepositoryRequiredError):
        await service.create(
            TASK, OPERATOR, project_id=PROJECT, workflow_kind=WorkflowKind.DEVELOPMENT
        )
    with pytest.raises(RepositoryNotAuthorizedError):
        await service.create(
            TASK,
            OPERATOR,
            project_id=PROJECT,
            workflow_kind=WorkflowKind.DEVELOPMENT,
            repository_id="other",
        )
    # A principal of another project never reaches this project's repository.
    with pytest.raises(PermissionDeniedError):
        await service.create(
            TASK,
            who("eve", {"project_operator"}, {"other-project"}),
            project_id=PROJECT,
            workflow_kind=WorkflowKind.DEVELOPMENT,
            repository_id=REPO,
        )
    # Without a development graph the workflow does not exist at all.
    bare = RunService(
        build_graph(world.model),
        world.repository,
        authorization=AuthorizationPolicy(separation_of_duties=False),
    )
    with pytest.raises(WorkflowUnavailableError):
        await bare.create(
            TASK,
            OPERATOR,
            project_id=PROJECT,
            workflow_kind=WorkflowKind.DEVELOPMENT,
            repository_id=REPO,
        )
    assert world.model.calls == []  # nothing ever reached the model


async def test_concurrent_decisions_apply_exactly_one(tmp_path):
    world = World(tmp_path)
    service = world.service()
    run_id = await start(world, service)
    digest = (await service.decisions(run_id, OPERATOR)).pending.scope_digest  # type: ignore[union-attr]
    outcomes = await asyncio.gather(
        service.approve(run_id, True, OPERATOR, scope_digest=digest),
        service.approve(run_id, False, OPERATOR, scope_digest=digest),
        service.approve(run_id, True, OPERATOR, scope_digest=digest),
        return_exceptions=True,
    )
    errors = [o for o in outcomes if isinstance(o, BaseException)]
    responses = [o for o in outcomes if not isinstance(o, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], RunAlreadyDecidedError)
    assert {r.status for r in responses} == {S.COMPLETED}
    assert len((await service.decisions(run_id, OPERATOR)).decisions) == 1


async def test_separation_of_duties_applies_to_scope_approval(tmp_path):
    world = World(tmp_path)
    service = world.service(separation_of_duties=True)
    run_id = await start(world, service)
    digest = (await service.decisions(run_id, OPERATOR)).pending.scope_digest  # type: ignore[union-attr]
    with pytest.raises(PermissionDeniedError):  # creator may not approve their own scope
        await service.approve(run_id, True, OPERATOR, scope_digest=digest)
    other = who("omar", {"project_operator"})
    assert (await service.approve(run_id, True, other, scope_digest=digest)).status is S.COMPLETED


async def test_a_decision_in_another_process_resumes_the_same_run(tmp_path):
    world = World(tmp_path)
    first = world.service(owner="api-1")
    run_id = await start(world, first)
    digest = (await first.decisions(run_id, OPERATOR)).pending.scope_digest  # type: ignore[union-attr]
    second = world.service(owner="api-2")  # same stores and checkpointer, new process
    response = await second.approve(run_id, True, OPERATOR, scope_digest=digest)
    assert response.status is S.COMPLETED
    assert (await first.get(run_id, OPERATOR)).status is S.COMPLETED


async def test_policy_limits_bound_the_run(tmp_path):
    # Change size: the Developer's content exceeds max_file_bytes.
    world = World(tmp_path, policy_overrides={"max_file_bytes": 256})
    world.model.register(
        DeveloperChanges,
        DeveloperChanges(
            summary="big", edits=[FileEdit(path="src/app.py", content="x" * 300 + "\n")]
        ),
    )
    service = world.service()
    run_id = await start(world, service)
    digest = (await service.decisions(run_id, OPERATOR)).pending.scope_digest  # type: ignore[union-attr]
    response = await service.approve(run_id, True, OPERATOR, scope_digest=digest)
    assert response.status is S.FAILED and response.error.code == "change_too_large"  # type: ignore[union-attr]
    # Model calls: scoper + developer + QA exceed a budget of 2.
    (tmp_path / "b").mkdir()
    world2 = World(tmp_path / "b", policy_overrides={"max_model_calls": 2})
    service2 = world2.service()
    run_id = await start(world2, service2)
    digest = (await service2.decisions(run_id, OPERATOR)).pending.scope_digest  # type: ignore[union-attr]
    response = await service2.approve(run_id, True, OPERATOR, scope_digest=digest)
    assert response.status is S.FAILED and response.error.code == "development_budget_exceeded"  # type: ignore[union-attr]
    # Correction limit with a failing pytest, bounded by the policy.
    (tmp_path / "c").mkdir()
    world3 = World(tmp_path / "c", policy_overrides={"max_corrections": 1})
    world3.sandbox.script(
        ProgramId.PYTEST,
        lambda request: SandboxResult(
            execution_id=request.execution_id,
            status=SandboxStatus.COMPLETED,
            exit_code=1,
            stdout="1 failed",
            duration_ms=1,
            runner_id="fake-runner",
        ),
    )
    service3 = world3.service()
    run_id = await start(world3, service3)
    digest = (await service3.decisions(run_id, OPERATOR)).pending.scope_digest  # type: ignore[union-attr]
    response = await service3.approve(run_id, True, OPERATOR, scope_digest=digest)
    assert response.status is S.FAILED and response.error.code == "correction_limit"  # type: ignore[union-attr]
    assert response.revision == 1


async def test_scoper_proposals_outside_policy_end_as_scope_empty(tmp_path):
    world = World(tmp_path)
    world.model.register(
        ScopeProposal,
        ScopeProposal(
            summary="fuera",
            files=[
                ScopeFile(path=".github/workflows/ci.yml", action=ScopeAction.EDIT, reason="x"),
                ScopeFile(path="../etc/passwd", action=ScopeAction.EDIT, reason="y"),
            ],
        ),
    )
    service = world.service()
    run_id = await start(world, service)
    response = await service.get(run_id, OPERATOR)
    assert response.status is S.FAILED and response.error.code == "scope_empty"  # type: ignore[union-attr]
    assert not await world.audit.list_for_run(uuid.UUID(run_id)) or all(
        r.risk is RiskClass.READ for r in await world.audit.list_for_run(uuid.UUID(run_id))
    )
    replace  # keep the import used for future variants


async def test_policy_tightened_after_approval_blocks_the_writes(tmp_path):
    world = World(tmp_path)
    service = world.service()
    run_id = await start(world, service)
    digest = (await service.decisions(run_id, OPERATOR)).pending.scope_digest  # type: ignore[union-attr]
    # The operator removes tests/** from the policy after the human saw the proposal.
    world.policies._policies[REPO] = world.policy.model_copy(update={"writable_globs": ["src/**"]})
    response = await service.approve(run_id, True, OPERATOR, scope_digest=digest)
    assert response.status is S.FAILED and response.error is not None
    assert response.error.code == "scope_outside_policy"
    worktree = world.box.manager.get(uuid.UUID(run_id), PROJECT)
    assert worktree is not None and (worktree.path / "src/app.py").read_text() == "print('hi')\n"
    assert not (worktree.path / "tests/test_greet.py").exists()
    rows = await world.audit.list_for_run(uuid.UUID(run_id))
    assert all(r.risk is RiskClass.READ for r in rows)  # not a single write or exec


async def test_a_path_rejected_in_the_proposal_never_becomes_writable(tmp_path):
    world = World(tmp_path)
    # Developer later tries the README the proposal asked for and the policy rejected.
    world.model.register(
        DeveloperChanges,
        DeveloperChanges(
            summary="docs too",
            edits=[
                FileEdit(path="src/app.py", content="x = 1\n"),
                FileEdit(path="README.md", content="# pwned\n"),
            ],
        ),
    )
    service = world.service()
    run_id = await start(world, service)
    view = await service.decisions(run_id, OPERATOR)
    assert [r["path"] for r in view.pending.payload["rejected"]] == ["README.md"]  # type: ignore[union-attr]
    response = await service.approve(run_id, True, OPERATOR, scope_digest=view.pending.scope_digest)  # type: ignore[union-attr]
    assert response.status is S.FAILED and response.error.code == "path_denied"  # type: ignore[union-attr]
    worktree = world.box.manager.get(uuid.UUID(run_id), PROJECT)
    assert worktree is not None and (worktree.path / "README.md").read_text() == "# Demo\n"


async def test_a_digest_of_a_different_file_set_is_stale(tmp_path):
    from app.contracts.scope import scope_digest

    world = World(tmp_path)
    service = world.service()
    run_id = await start(world, service)
    pending = (await service.decisions(run_id, OPERATOR)).pending
    assert pending is not None
    shown = [ScopeFile(**f) for f in pending.payload["files"]]
    assert scope_digest(REPO, shown, pending.payload["tests"]) == pending.scope_digest
    narrower = scope_digest(REPO, shown[:1], pending.payload["tests"])  # one file fewer
    wider = scope_digest(
        REPO,
        [*shown, ScopeFile(path="src/x.py", action=ScopeAction.CREATE, reason="r")],
        pending.payload["tests"],
    )
    for digest in (narrower, wider):
        with pytest.raises(DecisionStaleError):
            await service.approve(run_id, True, OPERATOR, scope_digest=digest)
    assert (await service.get(run_id, OPERATOR)).status is S.WAITING_APPROVAL


async def test_events_distinguish_who_initiated_approved_and_executed(tmp_path):
    world = World(tmp_path)
    service = world.service()
    run_id = await start(world, service)
    digest = (await service.decisions(run_id, OPERATOR)).pending.scope_digest  # type: ignore[union-attr]
    omar = who("omar", {"project_operator"})
    assert (await service.approve(run_id, True, omar, scope_digest=digest)).status is S.COMPLETED
    events = await world.repository.list_events(uuid.UUID(run_id), after_id=None, limit=100)
    by_kind: dict[RunEventKind, list] = {}
    for event in events:
        by_kind.setdefault(event.kind, []).append(event)
    created = by_kind[RunEventKind.RUN_CREATED][0]
    assert created.actor_subject == "olivia" and created.payload["initiated_by"] == "olivia"
    recorded = by_kind[RunEventKind.APPROVAL_RECORDED][0]
    assert recorded.actor_subject == "omar" and recorded.payload["approved_by"] == "omar"
    applied = by_kind[RunEventKind.APPROVAL_APPLIED][0]
    assert applied.actor_subject == "omar" and applied.payload["approved_by"] == "omar"
    # Node events: executed by the automation identity, attributed to nobody.
    for kind, agent in (
        (RunEventKind.SCOPE_PROPOSED, "agentforge:scoper"),
        (RunEventKind.DEVELOPER_COMPLETED, "agentforge:developer"),
        (RunEventKind.DEVELOPER_APPLIED, "agentforge:developer"),
        (RunEventKind.QA_REVIEWED, "agentforge:qa"),
    ):
        for event in by_kind[kind]:
            assert event.actor_subject is None, kind
            assert event.payload["executed_by"] == agent, kind
    completed = by_kind[RunEventKind.RUN_COMPLETED][0]
    assert completed.actor_subject is None and completed.payload["approved_by"] == "omar"
    cancelled_id = await start(world, service)
    await service.cancel(cancelled_id, OPERATOR)
    cancel = next(
        e
        for e in await world.repository.list_events(
            uuid.UUID(cancelled_id), after_id=None, limit=50
        )
        if e.kind is RunEventKind.RUN_CANCELLED
    )
    assert cancel.actor_subject == "olivia" and cancel.payload["cancelled_by"] == "olivia"

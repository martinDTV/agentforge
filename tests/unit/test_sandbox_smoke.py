"""Guards and behaviour of the operator smoke script, with fakes (no runner, no PostgreSQL)."""

import uuid
from pathlib import Path

import pytest

from app.contracts.lifecycle import RunLifecycleStatus
from app.contracts.tools import AgentKind, RiskClass
from app.infrastructure.db import InMemoryRunRepository, InMemoryToolInvocationRepository
from app.infrastructure.sandbox import FakeSandbox, UnconfiguredSandbox
from app.tools import ExecutionPolicy, ToolLimits
from app.tools.fake import StubTool
from scripts.sandbox_smoke import (
    SmokeDeps,
    SmokeRefused,
    main,
    require_local_environment,
    require_sandbox_project,
    run_smoke,
    seed_demo_repository,
    smoke_policy,
    succeeded,
)
from tests.unit.git_fixtures import GitSandbox, make_sandbox
from tests.unit.tool_fixtures import make_context

pytestmark = pytest.mark.anyio


def deps_for(
    box: GitSandbox, sandbox: FakeSandbox | UnconfiguredSandbox, *, confirm: bool = True
) -> SmokeDeps:
    return SmokeDeps(
        sandbox=sandbox,
        git=box.adapter,
        registry=box.registry,
        workspaces=box.manager,
        audit=InMemoryToolInvocationRepository(),
        runs=InMemoryRunRepository(),
        limits=ToolLimits(),
        execution_policy=ExecutionPolicy(),
        confirm=lambda _summary: confirm,
        out=lambda _line: None,
    )


def test_the_temporary_policy_grants_exactly_exec_run_to_one_test_agent():
    policy = smoke_policy()
    exec_tool = StubTool("exec.run", RiskClass.EXEC)
    writer = StubTool("git.commit", RiskClass.WRITE)
    pusher = StubTool("git.push", RiskClass.NETWORK)
    reader = StubTool("workspace.read_file", RiskClass.READ)
    developer = make_context(AgentKind.DEVELOPER, project="sandbox-demo")
    assert policy.decide(developer.principal, developer, exec_tool.spec).allowed
    for blocked in (writer, pusher, reader):
        assert not policy.decide(developer.principal, developer, blocked.spec).allowed, (
            blocked.spec.name
        )
    for agent in (AgentKind.PLANNER, AgentKind.ARCHITECT, AgentKind.QA, AgentKind.SECURITY):
        context = make_context(agent, project="sandbox-demo")
        assert policy.decide(context.principal, context, exec_tool.spec).code == "tool_not_granted"
    assert policy.enabled_risks == {RiskClass.READ, RiskClass.EXEC}
    assert (
        RiskClass.WRITE not in policy.enabled_risks
        and RiskClass.NETWORK not in policy.enabled_risks
    )
    # The application's default policy is untouched by building the smoke policy.
    from app.security import ENABLED_RISK_CLASSES

    assert ENABLED_RISK_CLASSES == {RiskClass.READ}


def test_guards_refuse_other_environments_and_projects():
    require_local_environment("local")
    for environment in ("test", "staging", "production"):
        with pytest.raises(SmokeRefused) as exc:
            require_local_environment(environment)
        assert exc.value.code == "environment"
    require_sandbox_project("sandbox-demo")
    for project in ("alpha", "default", "legacy", "production"):
        with pytest.raises(SmokeRefused) as exc:
            require_sandbox_project(project)
        assert exc.value.code == "project"


async def test_the_smoke_runs_once_audits_and_refuses_the_duplicate(tmp_path: Path):
    box = make_sandbox(tmp_path, project="sandbox-demo", repository="sandbox-demo")
    sandbox = FakeSandbox(runner_id="runner-01")
    deps = deps_for(box, sandbox)
    report = await run_smoke(deps, repository_id="sandbox-demo", project_id="sandbox-demo")
    assert succeeded(report)
    assert report.outcome == "ok" and report.status == "completed" and report.exit_code == 0
    assert report.duplicate_outcome == "denied" and report.duplicate_code == "duplicate_invocation"
    assert report.runner_id == "runner-01" and report.step == "smoke:ruff_check"
    [(request, _archive)] = sandbox.requests  # exactly one execution reached the runner
    assert request.program.value == "ruff_check" and request.argv[:2] == ["ruff", "check"]
    assert request.project_id == "sandbox-demo" and request.actor_subject == "agentforge:developer"
    assert report.audit_rows == 1  # the duplicate is refused without a new row
    run_id = uuid.UUID(report.run_id)
    assert box.manager.get(run_id, "sandbox-demo") is None  # worktree removed
    stored = await deps.runs.get_run(run_id)
    assert stored is not None and stored.status is RunLifecycleStatus.FAILED
    assert stored.error is not None and stored.error.code == "sandbox_smoke"
    assert stored.lease_owner is None  # lease released: nothing for a sweeper to recover


async def test_without_confirmation_or_a_verified_runner_nothing_is_sent(tmp_path: Path):
    box = make_sandbox(tmp_path, project="sandbox-demo", repository="sandbox-demo")
    sandbox = FakeSandbox()
    with pytest.raises(SmokeRefused) as exc:
        await run_smoke(
            deps_for(box, sandbox, confirm=False),
            repository_id="sandbox-demo",
            project_id="sandbox-demo",
        )
    assert exc.value.code == "not_confirmed" and sandbox.requests == []
    assert not list((box.worktrees_root / "sandbox-demo").iterdir())  # cleaned up
    unverified = FakeSandbox(verified=False)
    with pytest.raises(SmokeRefused) as exc:
        await run_smoke(
            deps_for(box, unverified), repository_id="sandbox-demo", project_id="sandbox-demo"
        )
    assert exc.value.code == "runner_unverified" and unverified.requests == []
    with pytest.raises(SmokeRefused) as exc:
        await run_smoke(
            deps_for(box, UnconfiguredSandbox()),
            repository_id="sandbox-demo",
            project_id="sandbox-demo",
        )
    assert exc.value.code == "runner_unverified"
    with pytest.raises(SmokeRefused) as exc:
        await run_smoke(deps_for(box, sandbox), repository_id="sandbox-demo", project_id="alpha")
    assert exc.value.code == "project"


async def test_repositories_outside_the_sandbox_project_are_refused(tmp_path: Path):
    box = make_sandbox(tmp_path, project="alpha", repository="demo")  # a "real" project
    from app.git import GitOperationError

    with pytest.raises(GitOperationError) as exc:  # the registry itself refuses the mismatch
        await run_smoke(
            deps_for(box, FakeSandbox()), repository_id="demo", project_id="sandbox-demo"
        )
    assert exc.value.code == "repository_mismatch"


def test_seed_demo_creates_a_bare_repository_with_one_ruff_finding(tmp_path: Path):
    root = tmp_path / "repos"
    path = seed_demo_repository(root)
    assert path == root / "sandbox-demo.git" and (path / "HEAD").is_file()
    assert seed_demo_repository(root) == path  # idempotent
    assert not (root / ".sandbox-demo-seed").exists()
    box_root = tmp_path / "wt"
    from app.git import RepositoryConfig, RepositoryRegistry
    from app.infrastructure.git import LocalGitAdapter
    from app.workspaces import WorktreeManager

    registry = RepositoryRegistry(
        root, [RepositoryConfig(id="sandbox-demo", project_id="sandbox-demo", path=path)]
    )
    manager = WorktreeManager(box_root, LocalGitAdapter(scratch_home=tmp_path / "h"), registry)
    record = manager.ensure(uuid.uuid4(), "sandbox-demo", "sandbox-demo")
    assert (record.path / "src" / "app.py").read_text().startswith("import os\n")


def test_main_refuses_before_touching_anything_outside_local(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import scripts.sandbox_smoke as smoke

    class Env:
        environment = "production"

    monkeypatch.setattr(smoke, "get_environment", lambda: Env())
    assert main([]) == 2
    assert '"refused": "environment"' in capsys.readouterr().err
    monkeypatch.setattr(smoke, "get_environment", lambda: type("E", (), {"environment": "local"})())
    assert main(["--project", "alpha"]) == 2
    assert '"refused": "project"' in capsys.readouterr().err

    class Unconfigured:
        sandbox_runner_url = None
        sandbox_runner_token = None
        sandbox_runner_id = None

    monkeypatch.setattr(smoke, "get_settings", lambda: Unconfigured())
    assert main([]) == 2
    assert "runner_unconfigured" in capsys.readouterr().err

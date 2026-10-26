"""Guards and behaviour of the operator development script, with fakes
(no runner, no PostgreSQL, no model)."""

import json
import uuid
from pathlib import Path

import pytest

from app.contracts.lifecycle import RunEventKind, RunLifecycleStatus, WorkflowKind
from app.contracts.tools import AgentKind
from app.git import GitOperationError, RepositoryConfig, RepositoryRegistry
from app.infrastructure.db import InMemoryRunRepository, InMemoryToolInvocationRepository
from app.infrastructure.git import LocalGitAdapter
from app.infrastructure.sandbox import FakeSandbox, UnconfiguredSandbox
from app.sandbox import ProgramId, SandboxResult, SandboxStatus
from app.security import AccessScope, AuthorizationPolicy, Permission
from app.tools import ExecutionPolicy, ToolLimits
from app.workspaces import WorktreeManager
from scripts.development_mvp import (
    DEFAULT_WRITABLE,
    DEMO_FILES,
    FAKE_VALIDATOR,
    DevelopmentBudget,
    DevelopmentDemoDeps,
    DevelopmentRefused,
    fake_model,
    main,
    operator_principal,
    register_in_file,
    require_local_environment,
    require_sandbox_project,
    run_development,
    seed_demo_repository,
    succeeded,
)

pytestmark = pytest.mark.anyio
PROJECT = "sandbox-dev"


def demo_box(tmp_path: Path, *, project: str = PROJECT, repository: str = PROJECT):
    root = tmp_path / "repos"
    path = seed_demo_repository(root, repository)
    registry = RepositoryRegistry(
        root, [RepositoryConfig(id=repository, project_id=project, path=path)]
    )
    adapter = LocalGitAdapter(scratch_home=tmp_path / "git-home")
    manager = WorktreeManager(tmp_path / "worktrees", adapter, registry)
    return adapter, registry, manager


def deps_for(
    tmp_path: Path,
    sandbox: FakeSandbox | UnconfiguredSandbox,
    *,
    confirm: bool = True,
    project: str = PROJECT,
    repository: str = PROJECT,
) -> DevelopmentDemoDeps:
    adapter, registry, manager = demo_box(tmp_path, project=project, repository=repository)
    return DevelopmentDemoDeps(
        sandbox=sandbox,
        git=adapter,
        registry=registry,
        workspaces=manager,
        audit=InMemoryToolInvocationRepository(),
        runs=InMemoryRunRepository(),
        llm=fake_model(),
        model_name="fake",
        limits=ToolLimits(),
        execution_policy=ExecutionPolicy(),
        confirm=lambda _summary: confirm,
        out=lambda _line: None,
    )


async def test_the_fake_run_completes_through_the_gateway_and_reports_everything(tmp_path):
    sandbox = FakeSandbox(runner_id="runner-01")
    deps = deps_for(tmp_path, sandbox)
    report = await run_development(deps, repository_id=PROJECT, project_id=PROJECT)
    assert succeeded(report) and report.status == "completed" and report.model == "fake"
    assert report.runner_id == "runner-01" and report.error_code is None
    assert report.changed_files == sorted(DEFAULT_WRITABLE) and report.revision == 0
    assert report.llm_calls == 2 and report.qa_approved
    worktree = Path(report.worktree or "")
    assert worktree.is_dir() and not report.worktree_removed
    assert (worktree / "src/mailcheck/validator.py").read_text() == FAKE_VALIDATOR
    assert (worktree / "tests/test_validator.py").is_file()
    assert (worktree / "tests/test_acceptance.py").read_text() == DEMO_FILES[
        "tests/test_acceptance.py"
    ]
    assert "validate_email" in report.patch and not report.patch_truncated
    kinds = [event["kind"] for event in report.events]
    for kind in (
        RunEventKind.RUN_CREATED,
        RunEventKind.DEVELOPER_COMPLETED,
        RunEventKind.DEVELOPER_APPLIED,
        RunEventKind.QA_REVIEWED,
        RunEventKind.RUN_COMPLETED,
    ):
        assert kind.value in kinds, kind
    # The only failed row is the Developer's audited read of the not-yet-existing
    # test file (QA reads it after the write); everything else succeeded.
    failed = [row for row in report.tool_invocations if row["status"] != "ok"]
    assert {(row["tool"], row["error_code"]) for row in failed} == {
        ("workspace.read_file", "path_not_found")
    }
    assert len(failed) == 1 and failed[0]["agent"] == "developer"
    assert report.uncertain_invocations == 0
    total = len(report.tool_invocations)
    assert report.tool_invocations_by_status == {"ok": total - 1, "failed": 1}
    agents = {row["agent"] for row in report.tool_invocations}
    assert agents == {AgentKind.DEVELOPER.value, AgentKind.QA.value}
    risks = {row["risk"] for row in report.tool_invocations}
    assert risks == {"read", "write", "exec"}
    programs = [(r.agent.value, r.program.value, r.argv[-1]) for r, _ in sandbox.requests]
    assert programs == [
        ("developer", "ruff_check", "tests"),
        ("developer", "ruff_format_check", "tests"),
        ("qa", "ruff_check", "tests"),
        ("qa", "ruff_format_check", "tests"),
        ("qa", "pytest", "tests"),
    ]
    assert all(r.project_id == PROJECT and r.network == "none" for r, _ in sandbox.requests)
    row = await deps.runs.get_run(uuid.UUID(report.run_id))
    assert row is not None and row.status is RunLifecycleStatus.COMPLETED
    assert row.workflow_kind is WorkflowKind.DEVELOPMENT and row.lease_owner is None
    assert row.idempotency_key is None
    assert row.owner_subject == "operator:development-mvp"
    assert report.estimated_usd == 0.0  # the fake reports no provider usage


async def test_remove_worktree_leaves_nothing_behind(tmp_path):
    deps = deps_for(tmp_path, FakeSandbox())
    report = await run_development(
        deps, repository_id=PROJECT, project_id=PROJECT, remove_worktree=True
    )
    assert report.worktree_removed and report.worktree is None
    assert deps.workspaces.get(uuid.UUID(report.run_id), PROJECT) is None
    assert "validate_email" in report.patch  # taken before removal


async def test_failed_pytest_loops_then_ends_with_correction_limit(tmp_path):
    sandbox = FakeSandbox()
    sandbox.script(
        ProgramId.PYTEST,
        lambda request: SandboxResult(
            execution_id=request.execution_id,
            status=SandboxStatus.COMPLETED,
            exit_code=1,
            stdout="FAILED tests/test_acceptance.py::x",
            duration_ms=3,
            runner_id="fake-runner",
        ),
    )
    deps = deps_for(tmp_path, sandbox)
    report = await run_development(
        deps,
        repository_id=PROJECT,
        project_id=PROJECT,
        budget=DevelopmentBudget(max_corrections=1),
    )
    assert not succeeded(report)
    assert report.status == "failed" and report.error_code == "correction_limit"
    assert report.revision == 1 and report.llm_calls == 4
    assert report.qa_issues and report.qa_issues[0]["code"] == "technical_checks_failed"
    assert report.worktree is not None  # kept for inspection
    assert [e["kind"] for e in report.events].count(RunEventKind.QA_REVIEWED.value) == 2


async def test_without_confirmation_or_a_verified_runner_nothing_is_sent(tmp_path):
    sandbox = FakeSandbox()
    deps = deps_for(tmp_path, sandbox, confirm=False)
    with pytest.raises(DevelopmentRefused) as exc:
        await run_development(deps, repository_id=PROJECT, project_id=PROJECT)
    assert exc.value.code == "not_confirmed" and sandbox.requests == []
    assert not list((deps.workspaces.root / PROJECT).iterdir())  # worktree cleaned up
    assert await deps.runs.list_runs(scope=AccessScope(projects=None), limit=5) == []
    unverified = FakeSandbox(verified=False)
    with pytest.raises(DevelopmentRefused) as exc:
        await run_development(
            deps_for(tmp_path / "b", unverified), repository_id=PROJECT, project_id=PROJECT
        )
    assert exc.value.code == "runner_unverified" and unverified.requests == []
    with pytest.raises(DevelopmentRefused) as exc:
        await run_development(
            deps_for(tmp_path / "c", UnconfiguredSandbox()),
            repository_id=PROJECT,
            project_id=PROJECT,
        )
    assert exc.value.code == "runner_unverified"
    with pytest.raises(DevelopmentRefused) as exc:
        await run_development(
            deps_for(tmp_path / "d", FakeSandbox()), repository_id=PROJECT, project_id="alpha"
        )
    assert exc.value.code == "project"


async def test_repositories_outside_the_sandbox_project_are_refused(tmp_path):
    deps = deps_for(tmp_path, FakeSandbox(), project="alpha", repository="demo")
    with pytest.raises(GitOperationError) as exc:
        await run_development(deps, repository_id="demo", project_id="sandbox-dev")
    assert exc.value.code == "repository_mismatch"


def test_the_operator_principal_can_create_and_audit_but_not_approve_or_use_tools():
    principal = operator_principal(PROJECT)
    policy = AuthorizationPolicy()
    assert principal.kind == "user" and principal.projects == {PROJECT}
    assert policy.has(principal, Permission.RUNS_CREATE)
    assert policy.has(principal, Permission.RUNS_AUDIT_READ)
    assert policy.has(principal, Permission.REPOS_WRITE)  # development runs need it
    assert not policy.has(principal, Permission.TOOLS_EXECUTE)
    assert not policy.has(principal, Permission.REPOS_PUBLISH)
    assert not policy.has(principal, Permission.PROJECTS_ALL)
    assert not policy.has(principal, Permission.RECOVERY_SWEEP)
    assert policy.resolve_project_for_create(principal, PROJECT) == PROJECT


def test_guards_refuse_other_environments_and_projects():
    require_local_environment("local")
    for environment in ("test", "staging", "production"):
        with pytest.raises(DevelopmentRefused) as exc:
            require_local_environment(environment)
        assert exc.value.code == "environment"
    require_sandbox_project(PROJECT)
    for project in ("alpha", "default", "legacy", "clivana", "production"):
        with pytest.raises(DevelopmentRefused) as exc:
            require_sandbox_project(project)
        assert exc.value.code == "project"


def test_seed_is_idempotent_and_registration_file_is_rewritten_atomically(tmp_path):
    root = tmp_path / "repos"
    path = seed_demo_repository(root)
    assert path == root / "sandbox-dev.git" and (path / "HEAD").is_file()
    assert seed_demo_repository(root) == path
    assert not (root / ".sandbox-dev-seed").exists()
    file = tmp_path / "repositories.json"
    file.write_text(
        json.dumps(
            {"repositories": [{"id": "other", "project_id": "sandbox-x", "path": str(path)}]}
        )
    )
    config = RepositoryConfig(id="sandbox-dev", project_id="sandbox-dev", path=path)
    register_in_file(file, config)
    register_in_file(file, config)  # no duplicates
    loaded = json.loads(file.read_text())
    assert [e["id"] for e in loaded["repositories"]] == ["other", "sandbox-dev"]
    assert loaded["repositories"][1]["authorized_test_paths"] == ["tests"]
    assert not file.with_suffix(".json.tmp").exists()
    registry = RepositoryRegistry.from_file(root, file)
    assert registry.get("sandbox-dev") is not None


def test_main_refuses_before_touching_anything_outside_local(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import scripts.development_mvp as script

    monkeypatch.setattr(
        script, "get_environment", lambda: type("E", (), {"environment": "production"})()
    )
    assert main([]) == 2
    assert '"refused": "environment"' in capsys.readouterr().err
    monkeypatch.setattr(
        script, "get_environment", lambda: type("E", (), {"environment": "local"})()
    )
    assert main(["--project", "clivana"]) == 2
    assert '"refused": "project"' in capsys.readouterr().err

    class Unconfigured:
        sandbox_runner_url = None
        sandbox_runner_token = None
        sandbox_runner_id = None

    monkeypatch.setattr(script, "get_settings", lambda: Unconfigured())
    assert main([]) == 2
    assert "runner_unconfigured" in capsys.readouterr().err

    class FakeConfigured:
        sandbox_runner_url = "http://100.64.0.1:8787"
        sandbox_runner_token = "x"
        sandbox_runner_id = "r"
        git_repositories_root = Path("/nonexistent")
        worktrees_root = Path("/nonexistent")
        use_fake_model = True

    monkeypatch.setattr(script, "get_settings", lambda: FakeConfigured())
    assert main([]) == 2  # a fake model configured in Settings is not a real run
    assert "fake_model_configured" in capsys.readouterr().err


async def test_lint_profile_dry_run_exercises_the_diff_path_end_to_end(tmp_path):
    from scripts.development_mvp import (
        LINT_DEMO_FILES,
        LINT_READ,
        LINT_REPOSITORY,
        LINT_WRITABLE,
        PROFILES,
    )

    root = tmp_path / "repos"
    path = seed_demo_repository(root, LINT_REPOSITORY, LINT_DEMO_FILES)
    registry = RepositoryRegistry(
        root, [RepositoryConfig(id=LINT_REPOSITORY, project_id=LINT_REPOSITORY, path=path)]
    )
    adapter = LocalGitAdapter(scratch_home=tmp_path / "git-home")
    manager = WorktreeManager(tmp_path / "worktrees", adapter, registry)
    sandbox = FakeSandbox(runner_id="runner-01")
    ruff_calls = {"n": 0}

    def ruff(request):
        ruff_calls["n"] += 1
        failing = ruff_calls["n"] <= 2
        return SandboxResult(
            execution_id=request.execution_id,
            status=SandboxStatus.COMPLETED,
            exit_code=1 if failing else 0,
            stdout="src/textkit/slug.py:3:1: I001 [*] Import block is un-sorted or un-formatted\n"
            if failing
            else "All checks passed!\n",
            duration_ms=2,
            runner_id="runner-01",
        )

    sandbox.script(ProgramId.RUFF_CHECK, ruff)
    sandbox.script(
        ProgramId.RUFF_CHECK_DIFF,
        lambda request: SandboxResult(
            execution_id=request.execution_id,
            status=SandboxStatus.COMPLETED,
            exit_code=1,
            stdout="-from collections.abc import Iterable, Callable\n"
            "+from collections.abc import Callable, Iterable\n",
            duration_ms=2,
            runner_id="runner-01",
        ),
    )
    deps = DevelopmentDemoDeps(
        sandbox=sandbox,
        git=adapter,
        registry=registry,
        workspaces=manager,
        audit=InMemoryToolInvocationRepository(),
        runs=InMemoryRunRepository(),
        llm=fake_model("lint"),
        model_name="fake",
        limits=ToolLimits(),
        execution_policy=ExecutionPolicy(),
        confirm=lambda _s: True,
        out=lambda _l: None,
    )
    profile = PROFILES["lint"]
    report = await run_development(
        deps,
        repository_id=profile.repository,
        project_id=profile.project,
        task=profile.task,
        writable_paths=LINT_WRITABLE,
        read_paths=LINT_READ,
        budget=DevelopmentBudget(ruff_diff=True),
    )
    assert succeeded(report) and report.revision == 1 and report.llm_calls == 4
    steps = [row["step"] for row in report.tool_invocations]
    assert "qa:0:ruff_check_diff" in steps and "qa:1:ruff_check_diff" not in steps
    assert report.tool_invocations_by_status.get("uncertain", 0) == 0
    slug = (Path(report.worktree or "") / "src/textkit/slug.py").read_text()
    assert "from collections.abc import Callable, Iterable" in slug
    assert [e["kind"] for e in report.events].count("qa_reviewed") == 2


def test_profiles_define_disposable_projects_only():
    from scripts.development_mvp import PROFILES

    for name, profile in PROFILES.items():
        assert profile.repository.startswith("sandbox-") and profile.project == profile.repository
        assert set(profile.writable).isdisjoint(
            p for p in profile.files if p.startswith("tests/test_") and "acceptance" in p
        ), name
        assert all(path in profile.files or path in profile.writable for path in profile.read), name

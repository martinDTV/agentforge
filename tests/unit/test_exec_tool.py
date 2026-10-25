"""``exec.run`` through the gateway with a scripted sandbox: nothing runs on this host."""

import subprocess
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.contracts.tools import AgentKind, InvocationStatus, RiskClass, ToolCall, ToolOutcome
from app.infrastructure.db import InMemoryToolInvocationRepository
from app.infrastructure.sandbox import FakeSandbox, UnconfiguredSandbox
from app.sandbox import (
    ProgramId,
    SandboxError,
    SandboxLimits,
    SandboxPort,
    SandboxResult,
    SandboxStatus,
    pack_worktree,
)
from app.security import DEFAULT_AGENT_GRANTS, AgentGrant
from app.tools import (
    ExecRunTool,
    ExecutionPolicy,
    GitTools,
    ToolLimits,
    default_registry,
    invocation_id,
)
from app.tools.gateway import digest_arguments
from app.tools.ports import ToolInvocation
from tests.unit.git_fixtures import GitSandbox, make_sandbox
from tests.unit.tool_fixtures import make_context, make_gateway

pytestmark = pytest.mark.anyio
READ_EXEC = frozenset({RiskClass.READ, RiskClass.EXEC})
EXEC_GRANTS: Mapping[AgentKind, AgentGrant] = {
    AgentKind.DEVELOPER: AgentGrant(
        tools=frozenset({"exec.*", "git.*", "workspace.*"}), risks=READ_EXEC
    ),
    AgentKind.QA: AgentGrant(tools=frozenset({"exec.run"}), risks=READ_EXEC),
}


def gateway_for(  # type: ignore[no-untyped-def]
    box: GitSandbox,
    sandbox: SandboxPort,
    *,
    enabled_risks: frozenset[RiskClass] | None = None,
    grants: Mapping[AgentKind, AgentGrant] = DEFAULT_AGENT_GRANTS,
    limits: ToolLimits | None = None,
    policy: ExecutionPolicy | None = None,
    audit: InMemoryToolInvocationRepository | None = None,
):
    bounds = limits or ToolLimits()
    exec_tool = ExecRunTool(sandbox, box.manager, git=box.adapter, policy=policy)
    registry = default_registry(bounds, GitTools(box.adapter, box.manager, bounds), exec_tool)
    return make_gateway(
        list(registry), grants=grants, enabled_risks=enabled_risks, limits=limits, audit=audit
    )


def run_call(program: str = "ruff_check", **extra: object) -> ToolCall:
    return ToolCall(name="exec.run", arguments={"program": program, **extra})


async def test_exec_run_is_registered_but_granted_to_nobody_and_gated_for_everybody(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    sandbox = FakeSandbox()
    gateway, audit = gateway_for(box, sandbox)
    spec = gateway.registry.get("exec.run")
    assert spec is not None and spec.spec.risk is RiskClass.EXEC
    assert spec.spec.timeout_seconds == pytest.approx(150.0)  # wall 120 + cancel grace
    for agent in AgentKind:
        context = make_context(agent, run_id=run_id, workspace=record.path)
        assert "exec.run" not in {d.name for d in gateway.available_for(context)}
        result = await gateway.invoke(context, run_call(), step=f"lint-{agent}")
        assert result.outcome is ToolOutcome.DENIED and result.error is not None
        assert result.error.code == "tool_not_granted", agent
    # Granted but the global risk gate stays at read: still denied, still audited.
    gated, _ = gateway_for(box, sandbox, grants=EXEC_GRANTS)
    developer = make_context(AgentKind.DEVELOPER, run_id=run_id, workspace=record.path)
    denied = await gated.invoke(developer, run_call(), step="lint")
    assert denied.error is not None and denied.error.code == "risk_not_enabled"
    assert sandbox.requests == [] and len(await audit.list_for_run(run_id)) == len(AgentKind)


async def test_a_verified_runner_receives_a_closed_plan_and_the_result_is_audited(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    sandbox = FakeSandbox()
    gateway, audit = gateway_for(box, sandbox, enabled_risks=READ_EXEC, grants=EXEC_GRANTS)
    developer = make_context(AgentKind.DEVELOPER, run_id=run_id, attempt=2, workspace=record.path)
    result = await gateway.invoke(developer, run_call("ruff_check", paths=["src"]), step="lint")
    assert result.ok and result.output is not None, result
    assert result.output["status"] == "completed" and result.output["exit_code"] == 0
    assert result.output["stdout"] == "ruff_check: ok\n"
    assert result.output["argv"] == [
        "ruff",
        "check",
        "--no-cache",
        "--output-format",
        "concise",
        "--",
        "src",
    ]
    assert result.output["execution_id"] == str(invocation_id(run_id, "lint", 2))
    [(request, archive)] = sandbox.requests
    assert request.execution_id == result.invocation_id and request.untrusted is True
    assert request.network == "none" and request.limits == SandboxLimits()
    assert request.env == {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "CI": "1",
        "LANG": "C.UTF-8",
    }
    assert (request.run_id, request.attempt, request.step, request.project_id) == (
        run_id,
        2,
        "lint",
        "alpha",
    )
    assert request.actor_subject == "agentforge:developer" and request.agent is AgentKind.DEVELOPER
    assert request.repository_id == "demo" and request.branch == record.branch
    assert request.head_sha == box.adapter.head(record.path).sha
    expected = pack_worktree(record.path, max_bytes=50 * 1024 * 1024)
    assert (request.workspace_sha256, request.workspace_bytes) == (
        expected.sha256,
        expected.size_bytes,
    )
    assert archive.content == expected.content and archive.file_count == 2
    [row] = await audit.list_for_run(run_id)
    assert (
        row.status is InvocationStatus.OK
        and row.risk is RiskClass.EXEC
        and row.tool_name == "exec.run"
    )
    assert row.output_digest and "ruff_check: ok" not in str(row.model_dump())


async def test_pytest_is_shipped_to_the_sandbox_never_run_here(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    (record.path / "tests").mkdir()
    (record.path / "tests" / "test_demo.py").write_text("def test_ok(): pass\n")
    sandbox = FakeSandbox()
    gateway, _ = gateway_for(box, sandbox, enabled_risks=READ_EXEC, grants=EXEC_GRANTS)
    qa = make_context(AgentKind.QA, run_id=run_id, workspace=record.path)
    ok = await gateway.invoke(
        qa, run_call("pytest", paths=["tests"], keyword="ok", fail_fast=True), step="t1"
    )
    assert ok.ok and ok.output is not None
    assert ok.output["argv"] == [
        "python",
        "-I",
        "-P",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-k",
        "ok",
        "-x",
        "--",
        "tests",
    ]
    assert sandbox.requests[-1][0].program is ProgramId.PYTEST
    # The guard in tests/unit/conftest.py proves nothing was spawned locally:
    with pytest.raises(RuntimeError, match="Ejecución local bloqueada"):
        subprocess.run(["python", "-c", "print(1)"], check=False)
    unauthorized = await gateway.invoke(qa, run_call("pytest", paths=["src"]), step="t2")
    assert unauthorized.outcome is ToolOutcome.FAILED and unauthorized.error is not None
    assert unauthorized.error.code == "path_not_authorized"
    escaped = await gateway.invoke(qa, run_call("ruff_check", paths=["../../etc"]), step="t3")
    assert escaped.error is not None and escaped.error.code == "path_invalid"
    bad_keyword = await gateway.invoke(
        qa, run_call("pytest", keyword="x --collect-only"), step="t4"
    )
    assert bad_keyword.error is not None and bad_keyword.error.code == "keyword_invalid"
    unknown = await gateway.invoke(qa, run_call("bash"), step="t5")
    assert unknown.outcome is ToolOutcome.INVALID_INPUT
    raw = await gateway.invoke(
        qa, ToolCall(name="exec.run", arguments={"command": "ls"}), step="t6"
    )
    assert raw.outcome is ToolOutcome.INVALID_INPUT
    assert len(sandbox.requests) == 1  # only the valid plan reached the sandbox


async def test_without_a_configured_runner_nothing_executes(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    gateway, audit = gateway_for(
        box, UnconfiguredSandbox(), enabled_risks=READ_EXEC, grants=EXEC_GRANTS
    )
    developer = make_context(AgentKind.DEVELOPER, run_id=run_id, workspace=record.path)
    result = await gateway.invoke(developer, run_call(), step="lint")
    assert result.outcome is ToolOutcome.FAILED and result.error is not None
    assert result.error.code == "sandbox_unconfigured"
    [row] = await audit.list_for_run(run_id)
    assert row.status is InvocationStatus.FAILED and row.error_code == "sandbox_unconfigured"
    down = FakeSandbox()
    down.fail_with = SandboxError("runner_unreachable", "sin ruta", retryable=True)
    gateway2, _ = gateway_for(box, down, enabled_risks=READ_EXEC, grants=EXEC_GRANTS)
    unreachable = await gateway2.invoke(developer, run_call(), step="lint2")
    assert unreachable.error is not None and unreachable.error.code == "runner_unreachable"
    unverified = FakeSandbox(verified=False)
    gateway3, _ = gateway_for(box, unverified, enabled_risks=READ_EXEC, grants=EXEC_GRANTS)
    refused = await gateway3.invoke(developer, run_call(), step="lint3")
    assert refused.error is not None and refused.error.code == "runner_unverified"


async def test_timeouts_cancel_the_remote_execution(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    slow = FakeSandbox()
    slow.delay_seconds = 0.5
    gateway, _ = gateway_for(
        box,
        slow,
        enabled_risks=READ_EXEC,
        grants=EXEC_GRANTS,
        limits=ToolLimits(timeout_seconds=0.05, max_timeout_seconds=0.1),
    )
    developer = make_context(AgentKind.DEVELOPER, run_id=run_id, workspace=record.path)
    result = await gateway.invoke(developer, run_call(), step="lint")
    assert result.outcome is ToolOutcome.FAILED and result.error is not None
    assert result.error.code == "timeout"
    assert slow.cancelled == [result.invocation_id]
    tool = ExecRunTool(slow, box.manager)
    assert tool.cancel(developer) is False  # no invocation stamped: nothing to cancel


async def test_project_and_workspace_isolation_hold_for_exec(tmp_path: Path):
    box = make_sandbox(tmp_path)
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    record = box.manager.ensure(mine, "alpha", "demo")
    sandbox = FakeSandbox()
    gateway, _ = gateway_for(box, sandbox, enabled_risks=READ_EXEC, grants=EXEC_GRANTS)
    other_project = make_context(
        AgentKind.DEVELOPER, run_id=mine, project="beta", workspace=record.path
    )
    other_run = make_context(AgentKind.DEVELOPER, run_id=theirs, workspace=record.path)
    plain = tmp_path / "plain"
    plain.mkdir()
    unmanaged = make_context(AgentKind.DEVELOPER, run_id=mine, workspace=plain)
    none = make_context(AgentKind.DEVELOPER, run_id=mine)
    cases = (
        (other_project, "workspace_mismatch"),
        (other_run, "workspace_mismatch"),
        (unmanaged, "workspace_not_managed"),
        (none, "workspace_unavailable"),
    )
    for index, (context, code) in enumerate(cases):
        result = await gateway.invoke(context, run_call(), step=f"lint-{index}")
        assert result.error is not None and result.error.code == code, code
    assert sandbox.requests == []


async def test_uncertain_or_duplicate_exec_steps_never_run_twice(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    sandbox = FakeSandbox()
    audit = InMemoryToolInvocationRepository()
    gateway, _ = gateway_for(box, sandbox, enabled_risks=READ_EXEC, grants=EXEC_GRANTS, audit=audit)
    dead = make_context(AgentKind.DEVELOPER, run_id=run_id, attempt=1, workspace=record.path)
    arguments = {"program": "ruff_check"}
    crashed = ToolInvocation(
        id=invocation_id(run_id, "lint", 1),
        run_id=run_id,
        step="lint",
        attempt=1,
        tool_name="exec.run",
        risk=RiskClass.EXEC,
        agent=dead.agent,
        actor_subject=dead.principal.subject,
        project_id="alpha",
        status=InvocationStatus.PENDING,
        input_digest=digest_arguments(arguments),
        started_at=datetime.now(UTC),
    )
    assert await audit.start(crashed)
    assert await audit.transition(
        crashed.id, expected=InvocationStatus.PENDING, status=InvocationStatus.RUNNING
    )
    alive = make_context(AgentKind.DEVELOPER, run_id=run_id, attempt=2, workspace=record.path)
    blocked = await gateway.invoke(alive, run_call(), step="lint")
    assert blocked.outcome is ToolOutcome.DENIED and blocked.error is not None
    assert blocked.error.code == "uncertain_effects" and sandbox.requests == []
    assert audit.rows[crashed.id].status is InvocationStatus.UNCERTAIN
    fresh = await gateway.invoke(alive, run_call(), step="lint-again")
    assert fresh.ok and len(sandbox.requests) == 1
    twice = await gateway.invoke(alive, run_call(), step="lint-again")
    assert twice.error is not None and twice.error.code == "duplicate_invocation"
    assert len(sandbox.requests) == 1
    # The runner's own idempotency answer is surfaced as a conflict, not retried locally.
    sandbox.script(ProgramId.RUFF_CHECK, SandboxError("execution_in_progress", "ya corre"))
    conflict = await gateway.invoke(alive, run_call(), step="lint-3")
    assert conflict.error is not None and conflict.error.code == "execution_in_progress"


async def test_sandbox_output_is_redacted_and_bounded_before_reaching_the_model(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    sandbox = FakeSandbox()
    leaky = SandboxResult(
        execution_id=run_id,
        status=SandboxStatus.COMPLETED,
        exit_code=1,
        stdout="token=abcdefghijklmnop\nFAILED tests/test_x.py\n",
        stderr=(
            "Authorization: Bearer "
            "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXNpZ25hdHVyZQ\n"
        ),
        stdout_truncated=True,
        duration_ms=50,
        runner_id="fake-runner",
    )
    sandbox.script(ProgramId.PYTEST, leaky)
    (record.path / "tests").mkdir()
    gateway, _ = gateway_for(box, sandbox, enabled_risks=READ_EXEC, grants=EXEC_GRANTS)
    qa = make_context(AgentKind.QA, run_id=run_id, workspace=record.path)
    result = await gateway.invoke(qa, run_call("pytest"), step="t")
    assert result.ok and result.output is not None  # exit code 1 is a result, not a tool failure
    assert result.output["exit_code"] == 1 and result.output["stdout_truncated"] is True
    assert (
        "abcdefghijklmnop" not in result.output["stdout"]
        and "[REDACTED]" in result.output["stdout"]
    )
    assert "[REDACTED:jwt]" in result.output["stderr"] and result.output["redactions"] == 2
    assert result.output["execution_id"] == str(result.invocation_id)

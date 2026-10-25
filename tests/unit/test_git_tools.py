"""Git tools through the gateway: nominal reads, blocked writes, managed worktrees only."""

import uuid
from collections.abc import Mapping
from pathlib import Path

import pytest

from app.contracts.tools import AgentKind, RiskClass, ToolCall, ToolOutcome, ToolResult
from app.git import StatusEntry
from app.security import DEFAULT_AGENT_GRANTS, AgentGrant
from app.tools import GitTools, ToolLimits, default_registry
from tests.unit.git_fixtures import GitSandbox, make_sandbox
from tests.unit.tool_fixtures import make_context, make_gateway

pytestmark = pytest.mark.anyio
ALL_RISKS = frozenset(RiskClass)


# Test-only grants that would let a developer write, to prove the global risk
# gate (and the adapter) still refuse; the product grants stay read-only.
WRITE_GRANTS: Mapping[AgentKind, AgentGrant] = {
    AgentKind.DEVELOPER: AgentGrant(tools=frozenset({"git.*", "workspace.*"}), risks=ALL_RISKS)
}


def tools_for(  # type: ignore[no-untyped-def]
    box: GitSandbox,
    *,
    enabled_risks: frozenset[RiskClass] | None = None,
    grants: Mapping[AgentKind, AgentGrant] = DEFAULT_AGENT_GRANTS,
):
    limits = ToolLimits()
    registry = default_registry(limits, GitTools(box.adapter, box.manager, limits))
    return make_gateway(list(registry), grants=grants, enabled_risks=enabled_risks)


def code(result: ToolResult) -> str:
    assert result.error is not None, result
    return result.error.code


async def test_default_registry_registers_git_reads_and_write_contracts(tmp_path: Path):
    box = make_sandbox(tmp_path)
    gateway, _ = tools_for(box)
    assert gateway.registry.names() == [
        "git.commit",
        "git.diff",
        "git.log",
        "git.push",
        "git.status",
        "workspace.find",
        "workspace.list_dir",
        "workspace.read_file",
    ]
    risks = {t.spec.name: t.spec.risk for t in gateway.registry}
    assert risks["git.commit"] is RiskClass.WRITE and risks["git.push"] is RiskClass.NETWORK
    assert all(risks[n] is RiskClass.READ for n in ("git.log", "git.diff", "git.status"))


async def test_git_reads_work_on_the_run_worktree_with_nominal_arguments(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    gateway, audit = tools_for(box)
    context = make_context(AgentKind.DEVELOPER, run_id=run_id, workspace=record.path)
    log = await gateway.invoke(
        context, ToolCall(name="git.log", arguments={"max_count": 5}), step="log"
    )
    assert log.ok and log.output is not None
    assert log.output["branch"] == f"run/{run_id}"
    assert [c["subject"] for c in log.output["commits"]] == ["seed"]
    status = await gateway.invoke(context, ToolCall(name="git.status"), step="status0")
    assert status.ok and status.output is not None and status.output["clean"] is True
    (record.path / "src" / "app.py").write_text("print('agent')\n")
    diff = await gateway.invoke(context, ToolCall(name="git.diff"), step="diff")
    assert diff.ok and diff.output is not None
    assert [f["path"] for f in diff.output["files"]] == ["src/app.py"]
    assert "+print('agent')" in diff.output["patch"]
    status2 = await gateway.invoke(context, ToolCall(name="git.status"), step="status1")
    assert status2.output is not None and status2.output["entries"] == [
        {"path": "src/app.py", "state": " M"}
    ]
    rows = await audit.list_for_run(run_id)
    assert [r.tool_name for r in rows] == ["git.log", "git.status", "git.diff", "git.status"]
    assert all(r.risk is RiskClass.READ for r in rows)


async def test_models_cannot_pass_git_arguments_or_escape_the_worktree(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    gateway, _ = tools_for(box)
    context = make_context(AgentKind.QA, run_id=run_id, workspace=record.path)
    injected = await gateway.invoke(
        context, ToolCall(name="git.log", arguments={"ref": "--output=/tmp/pwned"}), step="a"
    )
    assert injected.outcome is ToolOutcome.FAILED and code(injected) == "ref_invalid"
    escaped = await gateway.invoke(
        context, ToolCall(name="git.diff", arguments={"path": "../../etc/passwd"}), step="b"
    )
    assert code(escaped) == "pathspec_invalid"
    extra = await gateway.invoke(
        context, ToolCall(name="git.log", arguments={"args": ["--all"]}), step="c"
    )
    assert extra.outcome is ToolOutcome.INVALID_INPUT
    raw = await gateway.invoke(
        context, ToolCall(name="git.status", arguments={"cmd": "push"}), step="d"
    )
    assert raw.outcome is ToolOutcome.INVALID_INPUT
    assert not Path("/tmp/pwned").exists()


async def test_git_tools_refuse_unmanaged_or_foreign_workspaces(tmp_path: Path):
    box = make_sandbox(tmp_path)
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    record = box.manager.ensure(mine, "alpha", "demo")
    gateway, _ = tools_for(box)
    plain = tmp_path / "plain"
    plain.mkdir()
    unmanaged = make_context(AgentKind.DEVELOPER, run_id=mine, workspace=plain)
    assert code(await gateway.invoke(unmanaged, ToolCall(name="git.status"), step="s1")) == (
        "workspace_not_managed"
    )
    other_run = make_context(AgentKind.DEVELOPER, run_id=theirs, workspace=record.path)
    assert code(await gateway.invoke(other_run, ToolCall(name="git.status"), step="s2")) == (
        "workspace_mismatch"
    )
    other_project = make_context(
        AgentKind.DEVELOPER, run_id=mine, project="beta", workspace=record.path
    )
    assert code(await gateway.invoke(other_project, ToolCall(name="git.status"), step="s3")) == (
        "workspace_mismatch"
    )
    # The managed repository itself (bare, under the repositories root) is no workspace.
    host = make_context(AgentKind.DEVELOPER, run_id=mine, workspace=box.config.path)
    assert code(await gateway.invoke(host, ToolCall(name="git.log"), step="s4")) == (
        "workspace_not_managed"
    )
    none = make_context(AgentKind.DEVELOPER, run_id=mine)
    assert code(await gateway.invoke(none, ToolCall(name="git.log"), step="s5")) == (
        "workspace_unavailable"
    )


async def test_write_and_push_are_blocked_for_every_agent(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    gateway, audit = tools_for(box)
    (record.path / "x.txt").write_text("x\n")
    # Default grants are read-only: nobody is even granted the write contracts.
    for agent in AgentKind:
        context = make_context(agent, run_id=run_id, workspace=record.path)
        names = {d.name for d in gateway.available_for(context)}
        assert not names & {"git.commit", "git.push"}, agent
        commit = await gateway.invoke(
            context, ToolCall(name="git.commit", arguments={"message": "x"}), step=f"c-{agent}"
        )
        push = await gateway.invoke(context, ToolCall(name="git.push"), step=f"p-{agent}")
        assert (commit.outcome, code(commit)) == (ToolOutcome.DENIED, "tool_not_granted"), agent
        assert (push.outcome, code(push)) == (ToolOutcome.DENIED, "tool_not_granted"), agent
    assert len(await audit.list_for_run(run_id)) == 2 * len(AgentKind)
    # Even a grant that names write and network risks hits the global gate.
    gated, _ = tools_for(box, grants=WRITE_GRANTS)
    context = make_context(AgentKind.DEVELOPER, run_id=run_id, workspace=record.path)
    assert {d.name for d in gated.available_for(context)} == {
        "git.diff",
        "git.log",
        "git.status",
        "workspace.find",
        "workspace.list_dir",
        "workspace.read_file",
    }
    commit = await gated.invoke(
        context, ToolCall(name="git.commit", arguments={"message": "x"}), step="gated-commit"
    )
    push = await gated.invoke(context, ToolCall(name="git.push"), step="gated-push")
    assert code(commit) == "risk_not_enabled" and code(push) == "risk_not_enabled"
    # Nothing was committed anywhere.
    assert box.adapter.status(record.path).entries == [StatusEntry(path="x.txt", state="??")]
    assert box.adapter.head(record.path).subject == "seed"


async def test_even_with_the_gate_open_the_adapter_protects_refs_and_refuses_push(tmp_path: Path):
    """A test-only policy with every risk enabled: the contracts work on run
    branches with the service identity, and push stays off at the adapter."""
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    gateway, _ = tools_for(box, enabled_risks=ALL_RISKS, grants=WRITE_GRANTS)
    context = make_context(AgentKind.DEVELOPER, run_id=run_id, workspace=record.path)
    (record.path / "feature.py").write_text("ok\n")
    commit = await gateway.invoke(
        context, ToolCall(name="git.commit", arguments={"message": "feat: x"}), step="commit"
    )
    assert commit.ok and commit.output is not None
    assert commit.output["branch"] == f"run/{run_id}"
    head = box.adapter.head(record.path)
    assert head.author_name == "AgentForge" and head.subject == "feat: x"
    assert box.adapter.rev_parse(box.config, "main") != head.sha  # main untouched
    again = await gateway.invoke(
        context, ToolCall(name="git.commit", arguments={"message": "feat: x"}), step="commit"
    )
    assert code(again) == "duplicate_invocation"
    push = await gateway.invoke(context, ToolCall(name="git.push"), step="push")
    assert push.outcome is ToolOutcome.FAILED and code(push) == "push_disabled"


async def test_step_one_read_tools_work_inside_a_worktree(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    gateway, _ = tools_for(box)
    context = make_context(AgentKind.ARCHITECT, run_id=run_id, workspace=record.path)
    readme = await gateway.invoke(
        context, ToolCall(name="workspace.read_file", arguments={"path": "README.md"}), step="r"
    )
    assert readme.ok and readme.output is not None and readme.output["content"] == "# Demo\n"
    listing = await gateway.invoke(context, ToolCall(name="workspace.list_dir"), step="l")
    assert listing.output is not None
    names = [e["name"] for e in listing.output["entries"]]
    assert "README.md" in names and ".git" not in names
    gitfile = await gateway.invoke(
        context, ToolCall(name="workspace.read_file", arguments={"path": ".git"}), step="g"
    )
    assert code(gitfile) == "path_denied"  # the worktree's link to the repository stays opaque
    found = await gateway.invoke(
        context, ToolCall(name="workspace.find", arguments={"pattern": "**/*.py"}), step="f"
    )
    assert found.output is not None and found.output["matches"] == ["src/app.py"]

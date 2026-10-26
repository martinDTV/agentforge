import asyncio
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.agents.development import DeveloperChanges, FileEdit, QAObservation, QAReview
from app.contracts.lifecycle import RunEventKind
from app.contracts.tools import AgentKind, InvocationStatus, ToolCall
from app.infrastructure.db import InMemoryToolInvocationRepository
from app.infrastructure.llm.budget import BudgetedLLM, DevelopmentBudgetError
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.infrastructure.sandbox import FakeSandbox
from app.orchestration.development import (
    TECHNICAL_FAILURE_CODE,
    DevelopmentDeps,
    build_development_graph,
)
from app.orchestration.events import events_from_update, revision_from_update
from app.sandbox import ProgramId, SandboxError, SandboxResult, SandboxStatus
from app.security import DEFAULT_AGENT_GRANTS
from app.security.development import DevelopmentScope, DevelopmentToolPolicy
from app.tools import ExecRunTool, GitTools, ToolGateway, ToolLimits, default_registry
from app.tools.write_file import WriteFileTool
from tests.unit.git_fixtures import make_sandbox
from tests.unit.tool_fixtures import make_context

pytestmark = pytest.mark.anyio


def setup(tmp_path, writable: frozenset[str] = frozenset({"src/app.py"})):
    box = make_sandbox(tmp_path, project="sandbox-test", repository="sandbox-repo")
    rid = uuid.uuid4()
    record = box.manager.ensure(rid, "sandbox-test", "sandbox-repo")
    (record.path / "tests").mkdir()
    (record.path / "tests/test_app.py").write_text("def test_ok():\n    assert True\n")
    scope = DevelopmentScope(
        rid,
        "sandbox-test",
        "sandbox-repo",
        record.path,
        datetime.now(UTC) + timedelta(minutes=5),
        writable,
    )
    sandbox = FakeSandbox()
    limits = ToolLimits()
    registry = default_registry(
        limits, GitTools(box.adapter, box.manager, limits), ExecRunTool(sandbox, box.manager)
    )
    registry.register(WriteFileTool(box.manager, scope))
    audit = InMemoryToolInvocationRepository()
    gateway = ToolGateway(registry, DevelopmentToolPolicy(scope), audit)
    ctx = make_context(project="sandbox-test", run_id=rid, workspace=record.path)
    model = FakeLLMAdapter()
    model.register(
        DeveloperChanges,
        DeveloperChanges(
            summary="implementado", edits=[FileEdit(path="src/app.py", content="VALUE = 1\n")]
        ),
    )
    model.register(QAReview, QAReview(approved=True, summary="correcto", issues=[]))
    deps = DevelopmentDeps(scope, gateway, BudgetedLLM(model), ("src/app.py", "tests/test_app.py"))
    return box, scope, sandbox, audit, gateway, ctx, model, deps


def write_call(path="src/app.py", content="VALUE = 1\n", previous="print('hi')\n"):
    return ToolCall(
        name="workspace.write_file",
        arguments={
            "path": path,
            "content": content,
            "expected_sha256": hashlib.sha256(previous.encode()).hexdigest()
            if previous is not None
            else None,
        },
    )


async def test_write_is_scoped_audited_and_qa_cannot_write(tmp_path):
    _, scope, _, audit, gateway, ctx, _, _ = setup(tmp_path)
    result = await gateway.invoke(ctx, write_call(), step="write")
    assert result.ok and (scope.workspace_root / "src/app.py").read_text() == "VALUE = 1\n"
    assert len(await audit.list_for_run(scope.run_id)) == 1
    qa = make_context(
        AgentKind.QA, project=scope.project_id, run_id=scope.run_id, workspace=scope.workspace_root
    )
    assert (
        await gateway.invoke(qa, write_call(), step="qa-write")
    ).error.code == "tool_not_granted"
    wrong = ctx.model_copy(update={"run_id": uuid.uuid4()})
    assert (
        await gateway.invoke(wrong, write_call(), step="wrong")
    ).error.code == "development_scope_denied"
    assert not DEFAULT_AGENT_GRANTS[AgentKind.DEVELOPER].covers(
        gateway.registry.get("exec.run").spec
    )


async def test_write_conflict_and_duplicate_never_overwrite(tmp_path):
    _, scope, _, _, gateway, ctx, _, _ = setup(tmp_path)
    stale = await gateway.invoke(ctx, write_call(previous="stale"), step="stale")
    assert stale.error.code == "write_conflict"
    assert (await gateway.invoke(ctx, write_call(), step="ok")).ok
    assert (await gateway.invoke(ctx, write_call(), step="ok")).error.code == "duplicate_invocation"
    assert (scope.workspace_root / "src/app.py").read_text() == "VALUE = 1\n"


@pytest.mark.parametrize(
    "path", ["../escape.py", "/tmp/escape.py", ".git/config", ".env", "tests/test_app.py"]
)
async def test_dangerous_and_unapproved_writes_are_rejected(tmp_path, path):
    _, _, _, _, gateway, ctx, _, _ = setup(tmp_path)
    result = await gateway.invoke(ctx, write_call(path=path, previous=None), step="bad")
    assert not result.ok


@pytest.mark.parametrize("link", ["symbolic", "hard"])
async def test_write_refuses_links_even_for_authorized_path(tmp_path, link):
    _, scope, _, _, gateway, ctx, _, _ = setup(tmp_path)
    victim = tmp_path / "victim.py"
    victim.write_text("private")
    target = scope.workspace_root / "src/app.py"
    target.unlink()
    if link == "symbolic":
        target.symlink_to(victim)
    else:
        target.hardlink_to(victim)
    assert not (await gateway.invoke(ctx, write_call(previous="private"), step="link")).ok
    assert victim.read_text() == "private"


async def test_developer_qa_graph_finishes_only_after_technical_checks(tmp_path):
    _, scope, sandbox, audit, _, _, model, deps = setup(tmp_path)
    graph = build_development_graph(deps)
    result = await graph.ainvoke(
        {"task": "implement value", "workflow_kind": "development"},
        {"configurable": {"thread_id": str(scope.run_id)}},
    )
    assert result["completed"] and result["llm_calls"] == 2
    assert len(model.calls) == 2 and len(sandbox.requests) == 5
    assert {r.agent for r in await audit.list_for_run(scope.run_id)} == {
        AgentKind.DEVELOPER,
        AgentKind.QA,
    }


async def test_false_qa_success_cannot_override_failed_tests_and_loop_is_bounded(tmp_path):
    _, scope, sandbox, _, _, _, model, deps = setup(tmp_path)
    sandbox.script(
        ProgramId.PYTEST,
        lambda request: SandboxResult(
            execution_id=request.execution_id,
            status=SandboxStatus.COMPLETED,
            exit_code=1,
            stdout="1 failed",
            stderr="",
            duration_ms=1,
            runner_id="fake",
            backend="fake",
        ),
    )
    result = await build_development_graph(deps).ainvoke(
        {"task": "implement value"},
        {"configurable": {"thread_id": str(scope.run_id)}, "recursion_limit": 30},
    )
    assert result["error"]["code"] == "correction_limit"
    assert result["revision"] == 3 and len(model.calls) == 8
    assert not result.get("completed")


async def test_qa_feedback_causes_a_developer_correction(tmp_path):
    _, scope, _, _, _, _, model, deps = setup(tmp_path)
    model.register_sequence(
        QAReview,
        [
            QAReview(
                approved=False,
                summary="falta caso",
                issues=[
                    QAObservation(code="missing_test", path="src/app.py", detail="corrige el caso")
                ],
            ),
            QAReview(approved=True, summary="corregido", issues=[]),
        ],
    )
    result = await build_development_graph(deps).ainvoke(
        {"task": "implement value"}, {"configurable": {"thread_id": str(scope.run_id)}}
    )
    assert result["completed"] and result["revision"] == 1 and len(model.calls) == 4
    assert "missing_test" in model.calls[2][1]


async def test_budget_refuses_request_before_model_call():
    fake = FakeLLMAdapter()
    bounded = BudgetedLLM(fake, max_reserved_tokens=1)
    with pytest.raises(DevelopmentBudgetError):
        bounded.generate_structured("system", "user", QAReview)
    assert not fake.calls and bounded.calls == 0


async def test_expired_scope_refuses_every_tool(tmp_path):
    _, scope, _, _, gateway, ctx, _, _ = setup(tmp_path)
    policy = DevelopmentToolPolicy(
        replace(scope, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    assert (
        policy.decide(ctx.principal, ctx, gateway.registry.get("exec.run").spec).code
        == "development_scope_denied"
    )


async def test_cancellation_reaches_remote_sandbox_and_closes_audit(tmp_path):
    _, scope, sandbox, audit, gateway, ctx, _, _ = setup(tmp_path)
    sandbox.delay_seconds = 0.2
    task = asyncio.create_task(
        gateway.invoke(
            ctx,
            ToolCall(name="exec.run", arguments={"program": "ruff_check", "paths": ["src"]}),
            step="cancel",
        )
    )
    while not sandbox.requests:
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sandbox.cancelled) == 1
    rows = await audit.list_for_run(scope.run_id)
    assert rows[0].error_code == "cancelled"
    # A cancelled sandbox run may have had its effect: the audit says so.
    assert rows[0].status is InvocationStatus.UNCERTAIN


def _failing(program_output: str = "1 failed"):
    return lambda request: SandboxResult(
        execution_id=request.execution_id,
        status=SandboxStatus.COMPLETED,
        exit_code=1,
        stdout=program_output,
        stderr="",
        duration_ms=1,
        runner_id="fake",
        backend="fake",
    )


async def test_a_missing_writable_file_is_created_and_then_edited(tmp_path):
    _, scope, _, _, _, _, model, deps = setup(
        tmp_path, writable=frozenset({"src/app.py", "src/extra.py"})
    )
    model.register(
        DeveloperChanges,
        DeveloperChanges(
            summary="dos archivos",
            edits=[
                FileEdit(path="src/app.py", content="VALUE = 1\n"),
                FileEdit(path="src/extra.py", content="EXTRA = 2\n"),
            ],
        ),
    )
    model.register_sequence(
        QAReview,
        [
            QAReview(
                approved=False,
                summary="falta",
                issues=[QAObservation(code="x", path="src/extra.py", detail="d")],
            ),
            QAReview(approved=True, summary="ok", issues=[]),
        ],
    )
    result = await build_development_graph(deps).ainvoke(
        {"task": "t"}, {"configurable": {"thread_id": str(scope.run_id)}}
    )
    assert result["completed"] and result["revision"] == 1
    assert (scope.workspace_root / "src/extra.py").read_text() == "EXTRA = 2\n"
    first = json.loads(model.calls[0][1])
    assert first["must_create"] == ["src/extra.py"] and first["may_edit"] == ["src/app.py"]
    third = json.loads(model.calls[2][1])  # Developer, second revision
    assert third["must_create"] == [] and "src/extra.py" in third["files"]
    assert third["previous_qa"]["issues"][0]["code"] == "x"
    assert result["file_hashes"]["src/extra.py"] is not None
    assert sorted(result["changed_files"]) == ["src/app.py", "src/extra.py"]


async def test_failed_checks_force_rejection_even_if_qa_approves(tmp_path):
    _, scope, sandbox, _, _, _, model, deps = setup(tmp_path)
    sandbox.script(ProgramId.PYTEST, _failing("FAILED tests/test_app.py::test_ok"))
    deps = replace(deps, max_corrections=0)
    result = await build_development_graph(deps).ainvoke(
        {"task": "t"}, {"configurable": {"thread_id": str(scope.run_id)}}
    )
    assert result["error"]["code"] == "correction_limit" and not result.get("completed")
    review = result["qa_review"]
    assert review["approved"] is False
    assert review["issues"][0]["code"] == TECHNICAL_FAILURE_CODE
    assert "pytest" in review["issues"][0]["detail"]
    assert len(model.calls) == 2  # one Developer, one QA: the limit was zero corrections


def test_qa_review_event_is_recorded_even_when_the_limit_ends_the_run():
    update = {
        "qa": {
            "qa_review": {"approved": False, "issues": [{"code": "x"}]},
            "validation": [{"exit_code": 1}, {"exit_code": 0}],
            "error": {"code": "correction_limit"},
        }
    }
    events = events_from_update(update, revision=3)
    assert [e.kind for e in events] == [RunEventKind.QA_REVIEWED, RunEventKind.RUN_FAILED]
    assert events[0].ordinal == 3 and events[0].payload["failed_checks"] == 1
    approving = {"qa": {"qa_review": {"approved": True, "issues": []}, "completed": True}}
    kinds = events_from_update(approving, revision=1)
    assert [e.kind for e in kinds] == [RunEventKind.QA_REVIEWED, RunEventKind.RUN_COMPLETED]
    assert kinds[0].payload["approved"] is True
    assert events[1].payload == {"node": "qa", "code": "correction_limit"}
    assert revision_from_update({"revise_development": {"revision": 2}}, 1) == 2
    assert revision_from_update({"developer": {"revision": 0, "changes": {}}}, 5) == 0


async def test_runner_failure_ends_the_run_with_its_code_and_no_qa_call(tmp_path):
    _, scope, sandbox, _, _, _, model, deps = setup(tmp_path)
    sandbox.fail_with = SandboxError("runner_unreachable", "sin runner", retryable=True)
    result = await build_development_graph(deps).ainvoke(
        {"task": "t"}, {"configurable": {"thread_id": str(scope.run_id)}}
    )
    assert result["error"]["code"] == "runner_unreachable" and not result.get("completed")
    assert len(model.calls) == 1  # Developer only: QA never ran
    assert (scope.workspace_root / "src/app.py").read_text() == "VALUE = 1\n"


async def test_budget_exhaustion_ends_the_run_explicitly(tmp_path):
    _, scope, _, _, _, _, model, deps = setup(tmp_path)
    deps = replace(deps, llm=BudgetedLLM(model, max_calls=1))
    result = await build_development_graph(deps).ainvoke(
        {"task": "t"}, {"configurable": {"thread_id": str(scope.run_id)}}
    )
    assert result["error"]["code"] == "development_budget_exceeded"
    assert len(model.calls) == 1 and result["usage"]["calls"] == 1


async def test_wall_clock_timeout_cancels_the_remote_execution(tmp_path):
    _, scope, sandbox, audit, _, _, _, deps = setup(tmp_path)
    sandbox.delay_seconds = 1.0
    deps = replace(deps, wall_seconds=0.3)
    result = await build_development_graph(deps).ainvoke(
        {"task": "t"}, {"configurable": {"thread_id": str(scope.run_id)}}
    )
    assert result["error"]["code"] == "development_timeout"
    assert len(sandbox.cancelled) == 1  # the gateway told the sandbox to stop
    rows = await audit.list_for_run(scope.run_id)
    assert any(row.error_code == "cancelled" for row in rows)


def test_budgeted_llm_passes_the_output_bound_only_to_adapters_that_accept_it():
    class Recording:
        max_tokens: int | None = None

        def generate_structured(self, system_prompt, user_prompt, schema, max_tokens=4096):
            self.max_tokens = max_tokens
            return QAReview(approved=True, summary="ok", issues=[])

    recording = Recording()
    out = BudgetedLLM(recording, max_output_tokens=2048).generate_structured("s", "u", QAReview)
    assert recording.max_tokens == 2048 and out.approved
    fake = FakeLLMAdapter()
    fake.register(QAReview, QAReview(approved=True, summary="ok", issues=[]))
    bounded = BudgetedLLM(fake)
    bounded.generate_structured("s", "u", QAReview)
    assert fake.calls[0][2] is QAReview and bounded.usage()["calls"] == 1
    with pytest.raises(ValueError):
        BudgetedLLM(fake, max_calls=0)


async def test_identical_failures_are_flagged_to_the_developer_as_repeated(tmp_path):
    _, scope, sandbox, _, _, _, model, deps = setup(tmp_path)
    sandbox.script(ProgramId.PYTEST, _failing("FAILED tests/test_app.py::test_ok - same"))
    deps = replace(deps, max_corrections=2)
    await build_development_graph(deps).ainvoke(
        {"task": "t"}, {"configurable": {"thread_id": str(scope.run_id)}}
    )
    developer_prompts = [json.loads(call[1]) for call in model.calls if call[2] is DeveloperChanges]
    assert developer_prompts[0]["repeated_failures"] == []
    # The first QA failure is new; from the second identical QA failure on,
    # the program is flagged so the model changes its hypothesis.
    assert developer_prompts[1]["repeated_failures"] == []
    assert developer_prompts[2]["repeated_failures"] == ["pytest"]
    assert developer_prompts[1]["previous_validation"][2]["program"] == "pytest"
    assert developer_prompts[2]["previous_summary"] == "implementado"


@pytest.mark.parametrize(
    "path", ["../x.py", "/abs.py", "src/../x.py", "./x.py", "src//x.py", "src/x.py/", "~/x.py", ""]
)
def test_scope_refuses_non_canonical_writable_paths(tmp_path, path):
    with pytest.raises(ValueError):
        DevelopmentScope(
            uuid.uuid4(),
            "sandbox-a",
            "sandbox-r",
            tmp_path,
            datetime.now(UTC) + timedelta(minutes=1),
            frozenset({path}),
        )
    with pytest.raises(ValueError):  # too many files
        DevelopmentScope(
            uuid.uuid4(),
            "sandbox-a",
            "sandbox-r",
            tmp_path,
            datetime.now(UTC) + timedelta(minutes=1),
            frozenset(f"src/f{i}.py" for i in range(17)),
        )


async def test_revocation_denies_every_tool_immediately(tmp_path):
    _, scope, _, audit, gateway, ctx, _, _ = setup(tmp_path)
    assert (await gateway.invoke(ctx, write_call(), step="before")).ok
    scope.revoke()
    assert scope.revoked
    denied = await gateway.invoke(
        ctx, write_call(content="X = 2\n", previous="VALUE = 1\n"), step="after"
    )
    assert denied.error.code == "development_scope_denied"
    read = await gateway.invoke(
        ctx, ToolCall(name="workspace.read_file", arguments={"path": "src/app.py"}), step="read"
    )
    assert read.error.code == "development_scope_denied"
    assert (scope.workspace_root / "src/app.py").read_text() == "VALUE = 1\n"
    assert {r.status for r in await audit.list_for_run(scope.run_id)} >= {InvocationStatus.DENIED}


def test_budget_reserves_every_repair_turn_before_the_first_request():
    class Repairing:
        max_repair_attempts = 2

        def generate_structured(self, system_prompt, user_prompt, schema, max_tokens=4096):
            return QAReview(approved=True, summary="ok", issues=[])

    once = BudgetedLLM(FakeLLMAdapter(), max_output_tokens=1000)
    thrice = BudgetedLLM(Repairing(), max_output_tokens=1000)
    fake = FakeLLMAdapter()
    fake.register(QAReview, QAReview(approved=True, summary="ok", issues=[]))
    once.delegate = fake
    once.generate_structured("s", "u", QAReview)
    thrice.generate_structured("s", "u", QAReview)
    assert thrice.reserved_tokens == 3 * once.reserved_tokens
    with pytest.raises(DevelopmentBudgetError):
        BudgetedLLM(Repairing(), max_reserved_tokens=2 * once.reserved_tokens).generate_structured(
            "s", "u", QAReview
        )


def _diff_result(request):
    return SandboxResult(
        execution_id=request.execution_id,
        status=SandboxStatus.COMPLETED,
        exit_code=1,
        stdout=(
            "--- src/app.py\n+++ src/app.py\n@@ -1 +1 @@\n"
            "-import os, sys\n+import os\n+import sys\n"
        ),
        stderr="",
        duration_ms=1,
        runner_id="fake",
        backend="fake",
    )


async def test_qa_attaches_ruffs_fix_preview_only_when_enabled_and_ruff_fails(tmp_path):
    _, scope, sandbox, audit, _, _, model, deps = setup(tmp_path)
    calls = {"ruff_check": 0}

    def flaky_ruff(request):
        calls["ruff_check"] += 1
        failing = calls["ruff_check"] <= 2  # Developer and QA of revision 0 fail, then clean
        return SandboxResult(
            execution_id=request.execution_id,
            status=SandboxStatus.COMPLETED,
            exit_code=1 if failing else 0,
            stdout="src/app.py:1:1: I001 [*] Import block is un-sorted or un-formatted\n"
            if failing
            else "All checks passed!\n",
            stderr="",
            duration_ms=1,
            runner_id="fake",
            backend="fake",
        )

    sandbox.script(ProgramId.RUFF_CHECK, flaky_ruff)
    sandbox.script(ProgramId.RUFF_CHECK_DIFF, _diff_result)
    deps = replace(deps, ruff_diff=True)
    result = await build_development_graph(deps).ainvoke(
        {"task": "t"}, {"configurable": {"thread_id": str(scope.run_id)}}
    )
    assert result["completed"] and result["revision"] == 1
    programs = [(r.agent.value, r.program.value) for r, _ in sandbox.requests]
    # Developer's own checks never ask for the preview; QA does, once, when ruff failed.
    assert programs.count(("developer", "ruff_check_diff")) == 0
    assert programs.count(("qa", "ruff_check_diff")) == 1
    diff_request = next(r for r, _ in sandbox.requests if r.program is ProgramId.RUFF_CHECK_DIFF)
    assert diff_request.argv[:4] == ["ruff", "check", "--no-cache", "--diff"]
    assert "--fix" not in diff_request.argv
    assert diff_request.argv[-3:] == ["--", "src", "tests"]
    qa_prompt = json.loads(model.calls[1][1])
    ruff_entry = next(v for v in qa_prompt["validation"] if v["program"] == "ruff_check")
    assert "+from" in ruff_entry["fix_preview"] or "+import sys" in ruff_entry["fix_preview"]
    developer_prompt = json.loads(model.calls[2][1])
    assert "+import sys" in developer_prompt["previous_validation"][0]["fix_preview"]
    rows = await audit.list_for_run(scope.run_id)
    diff_rows = [r for r in rows if r.step.endswith("ruff_check_diff")]
    assert len(diff_rows) == 1 and diff_rows[0].agent is AgentKind.QA
    assert diff_rows[0].risk is not None and diff_rows[0].risk.value == "exec"


async def test_preview_disabled_by_default_and_unavailable_on_an_old_runner(tmp_path):
    _, scope, sandbox, _, _, _, model, deps = setup(tmp_path)
    sandbox.script(ProgramId.RUFF_CHECK, _failing("src/app.py:1:1: I001 [*] un-sorted"))
    # Default: no preview requested at all.
    result = await build_development_graph(replace(deps, max_corrections=0)).ainvoke(
        {"task": "t"}, {"configurable": {"thread_id": str(scope.run_id)}}
    )
    assert result["error"]["code"] == "correction_limit"
    assert all(r.program is not ProgramId.RUFF_CHECK_DIFF for r, _ in sandbox.requests)
    # Enabled, but the runner does not know the program: reported, never fatal.
    (tmp_path / "second").mkdir()
    _, scope2, sandbox2, _, _, _, _, deps2 = setup(tmp_path / "second")
    sandbox2.script(ProgramId.RUFF_CHECK, _failing("src/app.py:1:1: I001 [*] un-sorted"))
    sandbox2.script(
        ProgramId.RUFF_CHECK_DIFF, SandboxError("runner_rejected", "programa desconocido")
    )
    result = await build_development_graph(
        replace(deps2, ruff_diff=True, max_corrections=0)
    ).ainvoke({"task": "t"}, {"configurable": {"thread_id": str(scope2.run_id)}})
    assert result["error"]["code"] == "correction_limit"
    ruff_entry = next(v for v in result["validation"] if v["program"] == "ruff_check")
    assert ruff_entry["fix_preview_error"] == "runner_rejected" and "fix_preview" not in ruff_entry

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.contracts.tools import AgentKind, InvocationStatus, RiskClass, ToolCall, ToolOutcome
from app.infrastructure.db import InMemoryToolInvocationRepository
from app.tools import ToolLimits, ToolRegistry, invocation_id
from app.tools.fake import (
    BrokenTool,
    EchoTool,
    FailingTool,
    SlowTool,
    StubTool,
    WrongOutputTool,
)
from app.tools.ports import InvocationCompletion, ToolContext, ToolInvocation
from tests.unit.tool_fixtures import make_context, make_gateway

pytestmark = pytest.mark.anyio
READ_WRITE = frozenset({RiskClass.READ, RiskClass.WRITE})


def echo(message: str) -> ToolCall:
    return ToolCall(name="fake.echo", arguments={"message": message})


async def test_successful_invocation_is_audited_with_digests_only():
    tool = EchoTool()
    gateway, audit = make_gateway([tool])
    context = make_context()
    result = await gateway.invoke(context, echo("hola"), step="dev:1:read")
    assert result.ok and result.outcome is ToolOutcome.OK
    assert result.output == {"message": "hola", "agent": "developer"}
    assert (result.step, result.attempt) == ("dev:1:read", 0)
    assert result.invocation_id == invocation_id(context.run_id, "dev:1:read", 0)
    seen = tool.calls[0][0]
    assert seen.run_id == context.run_id and seen.principal == context.principal
    assert seen.invocation is not None and seen.invocation.id == result.invocation_id
    assert (seen.invocation.step, seen.invocation.attempt) == ("dev:1:read", 0)
    assert context.invocation is None  # the caller's context is never mutated
    [row] = await audit.list_for_run(context.run_id)
    assert row.status is InvocationStatus.OK and row.tool_name == "fake.echo"
    assert row.risk is RiskClass.READ and row.agent is AgentKind.DEVELOPER
    assert row.actor_subject == "agentforge:developer" and row.project_id == "alpha"
    assert len(row.input_digest) == 64 and row.output_digest and row.output_bytes
    assert row.started_at is not None and row.finished_at is not None
    assert "hola" not in str(row.model_dump())  # never the arguments or the output


async def test_unregistered_tools_are_denied_and_audited():
    gateway, audit = make_gateway([EchoTool()])
    context = make_context()
    call = ToolCall(name="shell.exec", arguments={"cmd": "rm -rf /"})
    result = await gateway.invoke(context, call, step="s")
    assert result.outcome is ToolOutcome.DENIED and result.error is not None
    assert result.error.code == "tool_not_registered" and result.output is None
    [row] = await audit.list_for_run(context.run_id)
    assert row.status is InvocationStatus.DENIED and row.error_code == "tool_not_registered"
    assert row.risk is None and row.tool_name == "shell.exec"
    assert "rm -rf" not in str(row.model_dump())


async def test_policy_denials_never_reach_the_tool():
    stub = StubTool("workspace.noop", RiskClass.READ)
    writer = StubTool("workspace.writer", RiskClass.WRITE)
    gateway, audit = make_gateway([stub, writer])
    planner = make_context(AgentKind.PLANNER)
    denied = await gateway.invoke(planner, ToolCall(name="workspace.noop"), step="s")
    assert denied.outcome is ToolOutcome.DENIED and denied.error is not None
    assert denied.error.code == "tool_not_granted" and stub.calls == 0
    developer = make_context(AgentKind.DEVELOPER)
    gated = await gateway.invoke(developer, ToolCall(name="workspace.writer"), step="s")
    assert gated.error is not None and gated.error.code == "risk_not_enabled"
    assert writer.calls == 0
    rows = await audit.list_for_run(planner.run_id) + await audit.list_for_run(developer.run_id)
    assert [r.error_code for r in rows] == ["tool_not_granted", "risk_not_enabled"]


async def test_model_supplied_arguments_cannot_widen_the_context():
    tool = EchoTool()
    gateway, audit = make_gateway([tool])
    context = make_context()
    smuggled = [
        {"message": "x", "project_id": "beta"},
        {"message": "x", "agent": "security"},
        {"message": "x", "workspace_root": "/"},
        {"message": 123},
        {},
    ]
    for index, arguments in enumerate(smuggled):
        call = ToolCall(name="fake.echo", arguments=arguments)
        result = await gateway.invoke(context, call, step=f"s{index}")
        assert result.outcome is ToolOutcome.INVALID_INPUT, arguments
        assert result.error is not None and result.error.code == "invalid_arguments"
        assert result.error.details
    assert tool.calls == []
    rows = await audit.list_for_run(context.run_id)
    assert len(rows) == 5 and {r.status for r in rows} == {InvocationStatus.INVALID_INPUT}
    with pytest.raises(ValidationError):
        context.agent = AgentKind.SECURITY  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ToolCall(name="fake.echo", arguments={}, project_id="beta")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ToolContext.model_validate({**context.model_dump(), "agent": "root"})


async def test_tool_failures_become_structured_results():
    gateway, audit = make_gateway(
        [FailingTool(), BrokenTool(), WrongOutputTool(), SlowTool(0.5)],
        limits=ToolLimits(timeout_seconds=0.05),
    )
    context = make_context()
    expectations = {
        "fake.failing": "simulated_failure",
        "fake.broken": "tool_crashed",
        "fake.wrong_output": "invalid_output",
        "fake.slow": "timeout",
    }
    for name, code in expectations.items():
        result = await gateway.invoke(context, ToolCall(name=name), step=name)
        assert result.outcome is ToolOutcome.FAILED and result.error is not None, name
        assert result.error.code == code and result.output is None
        assert "secret-bearing" not in result.error.message
    rows = await audit.list_for_run(context.run_id)
    assert sorted(r.error_code or "" for r in rows) == sorted(expectations.values())
    assert all(r.status is InvocationStatus.FAILED and r.finished_at for r in rows)


async def test_outputs_are_bounded_and_redacted():
    gateway, _ = make_gateway([EchoTool()], limits=ToolLimits(max_output_bytes=1024))
    context = make_context()
    big = await gateway.invoke(context, echo("x" * 2000), step="big")
    assert big.outcome is ToolOutcome.FAILED and big.error is not None
    assert big.error.code == "output_too_large"
    leaky = await gateway.invoke(context, echo("password=hunter2secret ok"), step="leak")
    assert leaky.ok and leaky.output is not None
    assert leaky.output["message"] == "password=[REDACTED] ok"


# -- idempotency: logical steps across attempts and restarts ------------------------------


async def test_steps_survive_restarts_and_detect_conflicts():
    tool = EchoTool()
    audit = InMemoryToolInvocationRepository()
    gateway, _ = make_gateway([tool], audit=audit)
    run_id = uuid.uuid4()
    first = make_context(run_id=run_id, attempt=1)
    ok = await gateway.invoke(first, echo("1"), step="dev:read")
    assert ok.ok and ok.invocation_id == invocation_id(run_id, "dev:read", 1)
    # Same step again inside the same attempt: a loop or a bug, never a retry.
    again = await gateway.invoke(first, echo("1"), step="dev:read")
    assert again.outcome is ToolOutcome.DENIED and again.error is not None
    assert again.error.code == "duplicate_invocation"
    # The process restarts (new gateway, same audit) and re-drives as attempt 2:
    # a read-only step simply runs again, with a stable, distinct id.
    restarted, _ = make_gateway([tool], audit=audit)
    second = make_context(run_id=run_id, attempt=2)
    retry = await restarted.invoke(second, echo("1"), step="dev:read")
    assert retry.ok and retry.invocation_id == invocation_id(run_id, "dev:read", 2)
    # The same step with other arguments is a conflict, not a retry.
    third = make_context(run_id=run_id, attempt=3)
    conflict = await restarted.invoke(third, echo("other"), step="dev:read")
    assert conflict.outcome is ToolOutcome.DENIED and conflict.error is not None
    assert conflict.error.code == "invocation_conflict"
    assert [m for _, m in tool.calls] == ["1", "1"]
    rows = await audit.find_step(run_id, "dev:read")
    assert [(r.attempt, r.status) for r in rows] == [
        (1, InvocationStatus.OK),
        (2, InvocationStatus.OK),
        (3, InvocationStatus.DENIED),
    ]


async def _crashed_row(
    audit: InMemoryToolInvocationRepository,
    context: ToolContext,
    tool_name: str,
    step: str,
    *,
    status: InvocationStatus,
    digest: str,
) -> ToolInvocation:
    """What a dead attempt leaves behind: a row claimed (and maybe running) but never closed."""
    row = ToolInvocation(
        id=invocation_id(context.run_id, step, context.attempt),
        run_id=context.run_id,
        step=step,
        attempt=context.attempt,
        tool_name=tool_name,
        agent=context.agent,
        actor_subject=context.principal.subject,
        project_id=context.project_id,
        status=InvocationStatus.PENDING,
        input_digest=digest,
        started_at=datetime.now(UTC),
    )
    assert await audit.start(row)
    if status is InvocationStatus.RUNNING:
        assert await audit.transition(
            row.id, expected=InvocationStatus.PENDING, status=InvocationStatus.RUNNING
        )
    return row


async def test_uncertain_steps_block_tools_with_effects_but_not_reads():
    from app.tools.gateway import digest_arguments

    writer = StubTool("workspace.writer", RiskClass.WRITE)
    reader = StubTool("workspace.reader", RiskClass.READ)
    audit = InMemoryToolInvocationRepository()
    gateway, _ = make_gateway([writer, reader], audit=audit, enabled_risks=READ_WRITE)
    run_id = uuid.uuid4()
    dead = make_context(run_id=run_id, attempt=1)
    digest = digest_arguments({})
    await _crashed_row(
        audit, dead, "workspace.writer", "w", status=InvocationStatus.RUNNING, digest=digest
    )
    await _crashed_row(
        audit, dead, "workspace.reader", "r", status=InvocationStatus.RUNNING, digest=digest
    )
    alive = make_context(run_id=run_id, attempt=2)
    blocked = await gateway.invoke(alive, ToolCall(name="workspace.writer"), step="w")
    assert blocked.outcome is ToolOutcome.DENIED and blocked.error is not None
    assert blocked.error.code == "uncertain_effects" and writer.calls == 0
    allowed = await gateway.invoke(alive, ToolCall(name="workspace.reader"), step="r")
    assert allowed.ok and reader.calls == 1
    w_rows = await audit.find_step(run_id, "w")
    r_rows = await audit.find_step(run_id, "r")
    assert [(r.attempt, r.status, r.error_code) for r in w_rows] == [
        (1, InvocationStatus.UNCERTAIN, "process_lost"),
        (2, InvocationStatus.DENIED, "uncertain_effects"),
    ]
    assert [(r.attempt, r.status) for r in r_rows] == [
        (1, InvocationStatus.UNCERTAIN),
        (2, InvocationStatus.OK),
    ]
    # A third attempt still sees the uncertainty: it does not expire by itself.
    later = make_context(run_id=run_id, attempt=3)
    still = await gateway.invoke(later, ToolCall(name="workspace.writer"), step="w")
    assert still.error is not None and still.error.code == "uncertain_effects"


async def test_completed_steps_with_effects_are_not_repeated_but_failed_ones_may_retry():
    writer = StubTool("workspace.writer", RiskClass.WRITE)
    audit = InMemoryToolInvocationRepository()
    gateway, _ = make_gateway([writer], audit=audit, enabled_risks=READ_WRITE)
    run_id = uuid.uuid4()
    done = await gateway.invoke(
        make_context(run_id=run_id, attempt=1), ToolCall(name="workspace.writer"), step="commit"
    )
    assert done.ok and writer.calls == 1
    repeat = await gateway.invoke(
        make_context(run_id=run_id, attempt=2), ToolCall(name="workspace.writer"), step="commit"
    )
    assert repeat.outcome is ToolOutcome.DENIED and repeat.error is not None
    assert repeat.error.code == "already_completed" and writer.calls == 1
    failing = StubTool("workspace.flaky", RiskClass.WRITE)
    gateway2, audit2 = make_gateway([failing], enabled_risks=READ_WRITE)
    first = make_context(run_id=run_id, attempt=1)
    row = ToolInvocation(
        id=invocation_id(run_id, "flaky", 1),
        run_id=run_id,
        step="flaky",
        attempt=1,
        tool_name="workspace.flaky",
        agent=first.agent,
        actor_subject=first.principal.subject,
        project_id=first.project_id,
        status=InvocationStatus.FAILED,
        error_code="git_failed",
        input_digest=__import__(
            "app.tools.gateway", fromlist=["digest_arguments"]
        ).digest_arguments({}),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    assert await audit2.start(row)
    retried = await gateway2.invoke(
        make_context(run_id=run_id, attempt=2), ToolCall(name="workspace.flaky"), step="flaky"
    )
    assert retried.ok and failing.calls == 1


async def test_pending_rows_of_dead_attempts_are_closed_and_do_not_block():
    from app.tools.gateway import digest_arguments

    writer = StubTool("workspace.writer", RiskClass.WRITE)
    audit = InMemoryToolInvocationRepository()
    gateway, _ = make_gateway([writer], audit=audit, enabled_risks=READ_WRITE)
    run_id = uuid.uuid4()
    dead = make_context(run_id=run_id, attempt=1)
    pending = await _crashed_row(
        audit,
        dead,
        "workspace.writer",
        "w",
        status=InvocationStatus.PENDING,
        digest=digest_arguments({}),
    )
    result = await gateway.invoke(
        make_context(run_id=run_id, attempt=2), ToolCall(name="workspace.writer"), step="w"
    )
    assert result.ok and writer.calls == 1
    closed = audit.rows[pending.id]
    assert (
        closed.status is InvocationStatus.FAILED and closed.error_code == "abandoned_before_start"
    )
    assert closed.finished_at is not None


async def test_without_audit_nothing_runs():
    tool = EchoTool()
    audit = InMemoryToolInvocationRepository()
    audit.fail_with = RuntimeError("audit down")
    gateway, _ = make_gateway([tool], audit=audit)
    with pytest.raises(RuntimeError, match="audit down"):
        await gateway.invoke(make_context(), echo("x"), step="s")
    assert tool.calls == []


async def test_invalid_step_keys_are_a_caller_bug_not_a_result():
    gateway, _ = make_gateway([EchoTool()])
    for bad in ("", "with space", "-dash", "x" * 129):
        with pytest.raises(ValueError, match="paso lógico"):
            await gateway.invoke(make_context(), echo("x"), step=bad)


async def test_available_tools_follow_the_same_policy_as_invoke():
    gateway, _ = make_gateway([EchoTool(), StubTool("workspace.writer", RiskClass.WRITE)])
    developer = [d.name for d in gateway.available_for(make_context(AgentKind.DEVELOPER))]
    assert developer == ["fake.echo"]  # the write tool is gated out
    assert gateway.available_for(make_context(AgentKind.PLANNER)) == []
    security = [d.name for d in gateway.available_for(make_context(AgentKind.SECURITY))]
    assert security == []  # granted only workspace.read_file, which is not registered here


async def test_in_memory_audit_mirrors_the_database_rules():
    audit = InMemoryToolInvocationRepository()
    now = datetime.now(UTC)
    row = ToolInvocation(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        step="s",
        attempt=0,
        tool_name="fake.echo",
        agent=AgentKind.QA,
        actor_subject="agentforge:qa",
        project_id="alpha",
        status=InvocationStatus.PENDING,
        input_digest="0" * 64,
        started_at=now,
    )
    assert await audit.start(row) is True
    assert await audit.start(row.model_copy(update={"id": uuid.uuid4()})) is False  # same key
    done = InvocationCompletion(finished_at=now, duration_ms=1)
    assert (
        await audit.transition(
            row.id, expected=InvocationStatus.RUNNING, status=InvocationStatus.OK, completion=done
        )
        is False
    )  # not running yet
    assert await audit.transition(
        row.id, expected=InvocationStatus.PENDING, status=InvocationStatus.RUNNING
    )
    assert await audit.transition(
        row.id, expected=InvocationStatus.RUNNING, status=InvocationStatus.OK, completion=done
    )
    assert audit.rows[row.id].finished_at == now
    assert ToolRegistry([]).names() == []


def test_create_app_wires_the_gateway_with_the_read_only_registry(fake_llm):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.orchestration.limits import RunLimits

    with TestClient(create_app(fake_llm, RunLimits())) as client:
        gateway = client.app.state.tool_gateway  # type: ignore[attr-defined]
        assert gateway.registry.names() == [
            "workspace.find",
            "workspace.list_dir",
            "workspace.read_file",
        ]  # no managed repositories configured: no git tools
        assert client.app.state.worktrees is None  # type: ignore[attr-defined]
        assert gateway.available_for(make_context(AgentKind.PLANNER)) == []
        assert [d.name for d in gateway.available_for(make_context(AgentKind.QA))] == [
            "workspace.find",
            "workspace.list_dir",
            "workspace.read_file",
        ]


class CancellableSlowTool(SlowTool):
    """A slow tool that can stop its remote work when the gateway asks."""

    def __init__(self, seconds: float, *, declared_timeout: float | None = None) -> None:
        super().__init__(seconds)
        self.cancelled: list[ToolContext] = []
        if declared_timeout is not None:
            from app.tools import ToolSpec

            self.spec = ToolSpec(
                name="fake.slow",
                description="lenta pero cancelable",
                risk=RiskClass.READ,
                input_model=self.spec.input_model,
                output_model=self.spec.output_model,
                timeout_seconds=declared_timeout,
            )

    def cancel(self, context: ToolContext) -> bool:
        self.cancelled.append(context)
        return True


async def test_timeouts_honour_the_tool_declaration_within_the_cap_and_call_cancel():
    slow = CancellableSlowTool(0.5, declared_timeout=60.0)
    gateway, audit = make_gateway(
        [slow], limits=ToolLimits(timeout_seconds=0.05, max_timeout_seconds=0.1)
    )
    context = make_context()
    result = await gateway.invoke(context, ToolCall(name="fake.slow"), step="slow")
    assert result.outcome is ToolOutcome.FAILED and result.error is not None
    assert result.error.code == "timeout" and "0.1" in result.error.message  # cap wins
    assert len(slow.cancelled) == 1
    assert slow.cancelled[0].invocation is not None
    assert slow.cancelled[0].invocation.id == result.invocation_id
    [row] = await audit.list_for_run(context.run_id)
    assert row.status is InvocationStatus.FAILED and row.error_code == "timeout"
    # A declared timeout under the cap is used as such.
    patient = CancellableSlowTool(0.05, declared_timeout=1.0)
    gateway2, _ = make_gateway([patient], limits=ToolLimits(timeout_seconds=0.01))
    assert (await gateway2.invoke(make_context(), ToolCall(name="fake.slow"), step="s")).ok
    assert patient.cancelled == []

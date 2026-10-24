"""Business persistence through RunService with in-memory fakes (no PostgreSQL, no Vertex)."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, TypeVar

import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError

from app.contracts.lifecycle import RunEventKind, RunLifecycleStatus
from app.contracts.planning import ArchitectureReview, DevelopmentPlan
from app.infrastructure.db import DatabaseUnavailableError, InMemoryRunRepository
from app.infrastructure.llm.errors import LLMTimeoutError
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.main import create_app
from app.orchestration.exceptions import RunAlreadyDecidedError
from app.orchestration.graph import build_graph
from app.orchestration.limits import RunLimits
from app.orchestration.ports import RunEvent
from app.orchestration.runs import RunService
from app.orchestration.status import checkpoint_id_of

T = TypeVar("T", bound=BaseModel)
S = RunLifecycleStatus
TASK = "Diseña un módulo de pacientes con pruebas"
RUNS = "/api/v1/runs"
pytestmark = pytest.mark.anyio


def config(run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": run_id}}


def kinds(events: list[Any]) -> list[tuple[str, int]]:
    return [(e.kind.value, e.ordinal) for e in events]


class GraphProxy:
    """Wraps the compiled graph to observe or sabotage ``astream`` calls."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.on_stream: Any = None
        self.fail_resume_with: Exception | None = None
        self.fail_all_with: Exception | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def astream(self, graph_input: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        if self.fail_all_with is not None:
            raise self.fail_all_with
        if isinstance(graph_input, Command) and self.fail_resume_with is not None:
            raise self.fail_resume_with
        if self.on_stream is not None:
            self.on_stream(graph_input)
        return self.inner.astream(graph_input, *args, **kwargs)  # type: ignore[no-any-return]


def make(
    llm: Any, limits: RunLimits | None = None
) -> tuple[RunService, InMemoryRunRepository, GraphProxy]:
    repository = InMemoryRunRepository()
    proxy = GraphProxy(build_graph(llm, limits))
    return RunService(proxy, repository), repository, proxy  # type: ignore[arg-type]


# 1. creation and persistent read --------------------------------------------


async def test_row_exists_as_running_before_the_first_llm_call(fake_llm: FakeLLMAdapter):
    repository = InMemoryRunRepository()
    seen: dict[str, Any] = {}

    class Observer:
        def generate_structured(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
            if schema is DevelopmentPlan and not seen:
                (row,) = repository.runs.values()
                seen["status"] = row.status
                seen["events"] = [e.kind for e in repository.events[row.id]]
            return fake_llm.generate_structured(system_prompt, user_prompt, schema)

    service = RunService(build_graph(Observer()), repository)  # type: ignore[arg-type]
    created = (await service.create(TASK)).response
    assert seen == {"status": S.RUNNING, "events": [RunEventKind.RUN_CREATED]}
    row = repository.runs[uuid.UUID(created.run_id)]
    assert row.status is S.WAITING_APPROVAL
    assert row.task == TASK and row.plan is not None and row.architecture is not None
    assert (row.revision, row.llm_calls, row.architect_approved) == (0, 2, True)
    snapshot = await service.graph.aget_state(config(created.run_id))
    assert row.last_checkpoint_id == checkpoint_id_of(snapshot) is not None
    assert (await service.get(created.run_id)) == created


# 2. errors and results -----------------------------------------------------


async def test_failures_and_final_results_are_persisted(fake_llm: FakeLLMAdapter):
    fake_llm.register_sequence(DevelopmentPlan, [LLMTimeoutError("sin respuesta")])
    service, repository, _ = make(fake_llm)
    failed = (await service.create(TASK)).response
    row = repository.runs[uuid.UUID(failed.run_id)]
    assert row.status is S.FAILED and failed.status == "failed"
    assert row.error is not None and row.error.code == "llm_timeout"
    assert row.plan is None and row.llm_calls == 1
    events = await repository.list_events(row.id)
    assert kinds(events)[-1] == ("run_failed", 0)
    assert events[-1].payload == {"node": "planner", "code": "llm_timeout"}
    with pytest.raises(RunAlreadyDecidedError):
        await service.approve(failed.run_id, True)


async def test_unexpected_exception_is_projected_as_failed_and_reraised(fake_llm: FakeLLMAdapter):
    fake_llm.register_sequence(ArchitectureReview, [RuntimeError("boom")])
    service, repository, _ = make(fake_llm)
    run_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="boom"):
        await service.create(TASK, run_id=run_id)
    row = repository.runs[uuid.UUID(run_id)]
    assert row.status is S.FAILED
    assert row.error is not None and row.error.code == "internal_error"
    assert row.plan is not None  # the planner's checkpoint survived
    assert ("run_failed", 0) in kinds(await repository.list_events(row.id))


# 3. audit trail of the Planner -> Architect cycle ----------------------------


async def test_audit_trail_covers_the_revision_cycle_and_the_decision(
    fake_llm: FakeLLMAdapter,
    sample_plan: DevelopmentPlan,
    revised_plan: DevelopmentPlan,
    sample_review: ArchitectureReview,
    rejecting_review: ArchitectureReview,
):
    fake_llm.register_sequence(DevelopmentPlan, [sample_plan, revised_plan])
    fake_llm.register_sequence(ArchitectureReview, [rejecting_review, sample_review])
    service, repository, _ = make(fake_llm)
    created = (await service.create(TASK)).response
    rid = uuid.UUID(created.run_id)

    assert kinds(await repository.list_events(rid)) == [
        ("run_created", 0),
        ("planner_completed", 0),
        ("architect_reviewed", 0),
        ("revision_requested", 1),
        ("planner_completed", 1),
        ("architect_reviewed", 1),
        ("waiting_approval", 1),
    ]
    reviews = [
        e for e in await repository.list_events(rid) if e.kind is RunEventKind.ARCHITECT_REVIEWED
    ]
    assert reviews[0].payload == {"revision": 0, "approved": False, "issues": 1, "decisions": 0}
    assert reviews[1].payload["approved"] is True

    await service.approve(created.run_id, False)
    assert kinds(await repository.list_events(rid))[-3:] == [
        ("approval_recorded", 0),
        ("run_rejected", 0),
        ("approval_applied", 0),
    ]
    serialized = json.dumps([e.payload for e in await repository.list_events(rid)])
    for sensitive in (TASK, sample_plan.summary, "Falta manejo de errores"):
        assert sensitive not in serialized


# 4. retries do not duplicate events -----------------------------------------


async def test_retried_writes_and_repeated_reads_do_not_duplicate_events(fake_llm: FakeLLMAdapter):
    service, repository, _ = make(fake_llm)
    created = (await service.create(TASK)).response
    rid = uuid.UUID(created.run_id)
    before = kinds(await repository.list_events(rid))
    again = [
        RunEvent(kind=RunEventKind.PLANNER_COMPLETED),
        RunEvent(kind=RunEventKind.WAITING_APPROVAL),
    ]
    assert await repository.append_events(rid, again) == 0
    for _ in range(3):
        await service.get(created.run_id)
    assert kinds(await repository.list_events(rid)) == before


# 5. reconciliation after a crash between checkpoint and projection ----------


async def test_reads_reconcile_a_row_left_behind_by_a_crash(fake_llm: FakeLLMAdapter):
    service, repository, _ = make(fake_llm)
    rid = uuid.uuid4()
    await repository.create_run(rid, TASK, owner="dead-process", ttl_seconds=60)
    repository.expire_lease(rid)
    # The graph ran and checkpointed, but the process died before projecting.
    await service.graph.ainvoke({"task": TASK}, config(str(rid)))
    assert repository.runs[rid].status is S.RUNNING
    assert repository.runs[rid].last_checkpoint_id is None

    response = await service.get(str(rid))
    row = repository.runs[rid]
    assert response.status == "waiting_approval" and row.status is S.WAITING_APPROVAL
    assert row.last_checkpoint_id == checkpoint_id_of(
        await service.graph.aget_state(config(str(rid)))
    )
    projects_so_far = repository.calls.count("project")
    await service.get(str(rid))
    assert repository.calls.count("project") == projects_so_far  # nothing to reconcile now


async def test_reconciliation_never_regresses_a_recorded_decision(fake_llm: FakeLLMAdapter):
    service, repository, proxy = make(fake_llm)
    created = (await service.create(TASK)).response
    rid = uuid.UUID(created.run_id)
    proxy.fail_resume_with = RuntimeError("crash before resume")
    with pytest.raises(RuntimeError):
        await service.approve(created.run_id, True)
    # The checkpoint still says "waiting"; the row must keep saying "resume_pending".
    assert (await service.get(created.run_id)).status == "resume_pending"
    assert repository.runs[rid].status is S.RESUME_PENDING


async def test_terminal_rows_are_never_rewritten(fake_llm: FakeLLMAdapter):
    service, repository, _ = make(fake_llm)
    created = (await service.create(TASK)).response
    await service.approve(created.run_id, True)
    reads_before = repository.calls.count("project")
    assert (await service.get(created.run_id)).status == "approved"
    assert repository.calls.count("project") == reads_before


# 6 + 7. durable approvals ---------------------------------------------------


async def test_decision_is_recorded_before_the_graph_resumes(fake_llm: FakeLLMAdapter):
    service, repository, proxy = make(fake_llm)
    created = (await service.create(TASK)).response
    rid = uuid.UUID(created.run_id)
    observed: dict[str, Any] = {}

    def on_stream(graph_input: Any) -> None:
        if isinstance(graph_input, Command):
            observed["status"] = repository.runs[rid].status
            observed["decision"] = repository.approvals[rid].approved
            observed["applied"] = repository.approvals[rid].applied_at

    proxy.on_stream = on_stream
    await service.approve(created.run_id, False)
    assert observed == {"status": S.RESUME_PENDING, "decision": False, "applied": None}
    applied = repository.approvals[rid]
    assert applied.applied_at is not None
    assert repository.runs[rid].status is S.REJECTED


async def test_crash_before_resume_keeps_the_decision_and_it_can_be_applied_later(
    fake_llm: FakeLLMAdapter,
):
    service, repository, proxy = make(fake_llm)
    created = (await service.create(TASK)).response
    rid = uuid.UUID(created.run_id)
    calls_before = len(fake_llm.calls)
    proxy.fail_resume_with = RuntimeError("process died")
    with pytest.raises(RuntimeError, match="process died"):
        await service.approve(created.run_id, True)

    decision = repository.approvals[rid]
    assert decision.approved is True and decision.applied_at is None
    assert repository.runs[rid].status is S.RESUME_PENDING
    assert ("approval_recorded", 0) in kinds(await repository.list_events(rid))

    proxy.fail_resume_with = None  # "the process restarted"
    repository.expire_lease(rid)  # ...and the old lease ran out
    recovered = await service.apply_pending_approval(created.run_id)
    assert recovered.status == "approved"
    assert repository.approvals[rid].applied_at is not None
    assert kinds(await repository.list_events(rid))[-2:] == [
        ("run_approved", 0),
        ("approval_applied", 0),
    ]
    assert len(fake_llm.calls) == calls_before
    # Idempotent: applying again changes nothing.
    assert (await service.apply_pending_approval(created.run_id)).status == "approved"


async def test_crash_after_resume_but_before_projection_is_repaired(fake_llm: FakeLLMAdapter):
    service, repository, _ = make(fake_llm)
    created = (await service.create(TASK)).response
    rid = uuid.UUID(created.run_id)
    claim = await repository.claim_approval(rid, True, owner="dead-process", ttl_seconds=60)
    assert claim is not None and claim.claimed
    await service.graph.ainvoke(Command(resume={"approved": True}), config(created.run_id))
    assert repository.runs[rid].status is S.RESUME_PENDING
    repository.expire_lease(rid)  # the process that claimed it is gone
    recovered = await service.apply_pending_approval(created.run_id)
    assert recovered.status == "approved"
    assert repository.approvals[rid].applied_at is not None


# 8. duplicate or contradictory approvals -------------------------------------


async def test_duplicate_approval_replays_and_contradictory_one_is_refused(
    fake_llm: FakeLLMAdapter,
):
    service, repository, _ = make(fake_llm)
    created = (await service.create(TASK)).response
    first = await service.approve(created.run_id, True)
    replay = await service.approve(created.run_id, True)
    assert first.status == replay.status == "approved"
    with pytest.raises(RunAlreadyDecidedError, match="approved"):
        await service.approve(created.run_id, False)
    assert len(repository.approvals) == 1


# 9. database errors -> 503, but only connection-level ones -----------------


def test_database_outage_becomes_503_with_retry_after(fake_llm: FakeLLMAdapter):
    repository = InMemoryRunRepository()
    with TestClient(create_app(fake_llm, RunLimits(), repository=repository)) as client:
        run_id = client.post(RUNS, json={"task": TASK}).json()["run_id"]
        repository.fail_with = DatabaseUnavailableError("connection refused: 127.0.0.1:5432")
        for response in (
            client.post(RUNS, json={"task": TASK}),
            client.get(f"{RUNS}/{run_id}"),
            client.post(f"{RUNS}/{run_id}/approve", json={"approved": True}),
        ):
            assert response.status_code == 503
            assert response.headers["content-type"] == "application/problem+json"
            assert response.headers["Retry-After"] == "5"
            assert response.json()["code"] == "database_unavailable"
            assert "5432" not in response.text and "127.0.0.1" not in response.text
        repository.fail_with = None
        assert client.get(f"{RUNS}/{run_id}").status_code == 200


def test_programming_errors_stay_500_not_503(fake_llm: FakeLLMAdapter):
    repository = InMemoryRunRepository()
    app = create_app(fake_llm, RunLimits(), repository=repository)
    with TestClient(app, raise_server_exceptions=False) as client:
        repository.fail_with = ProgrammingError("SELECT nope", {}, Exception("syntax error"))
        response = client.post(RUNS, json={"task": TASK})
        repository.fail_with = None
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "nope" not in response.text


def test_raw_checkpointer_connection_errors_become_503(fake_llm: FakeLLMAdapter):
    repository = InMemoryRunRepository()
    app = create_app(fake_llm, RunLimits(), repository=repository)
    with TestClient(app, raise_server_exceptions=False) as client:
        proxy = GraphProxy(app.state.run_service.graph)
        proxy.fail_all_with = psycopg.OperationalError("server closed the connection unexpectedly")
        app.state.run_service._graph = proxy  # the checkpointer, not the repository, is down
        response = client.post(RUNS, json={"task": TASK})
    assert response.status_code == 503
    assert response.json()["code"] == "database_unavailable"
    assert response.headers["Retry-After"] == "5"

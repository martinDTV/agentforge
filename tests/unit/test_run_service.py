"""RunService drives the graph asynchronously against in-memory stores."""

import asyncio
import threading
import uuid
from typing import TypeVar

import pytest
from pydantic import BaseModel

from app.contracts.planning import DevelopmentPlan
from app.infrastructure.db import InMemoryRunRepository
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.orchestration.exceptions import RunAlreadyDecidedError, RunNotFoundError
from app.orchestration.graph import build_graph
from app.orchestration.limits import RunLimits
from app.orchestration.runs import RunService
from app.security import AuthorizationPolicy
from app.security.tokens import DisabledAuthentication

T = TypeVar("T", bound=BaseModel)
TASK = "Diseña un módulo de pacientes con pruebas"

# Local administrator of the unit suite: explicit, not implicit (see test_api_authz).
ADMIN = DisabledAuthentication().principal()
# Local semantics for the unit suite: the single developer may approve their own run.
LENIENT = AuthorizationPolicy(separation_of_duties=False)


pytestmark = pytest.mark.anyio


def make_service(
    llm: object, limits: RunLimits | None = None, repository: InMemoryRunRepository | None = None
) -> RunService:
    return RunService(
        build_graph(llm, limits),  # type: ignore[arg-type]
        repository or InMemoryRunRepository(),
        authorization=LENIENT,
    )


class GatedLLM:
    """Blocks the planner call until ``release`` is set; signals ``entered`` first."""

    def __init__(self, inner: FakeLLMAdapter) -> None:
        self.inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate_structured(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        if schema is DevelopmentPlan:
            self.entered.set()
            assert self.release.wait(10), "la prueba no liberó al planner"
        return self.inner.generate_structured(system_prompt, user_prompt, schema)


async def test_create_get_and_approve_roundtrip(fake_llm: FakeLLMAdapter):
    service = make_service(fake_llm)
    created = (await service.create(TASK, ADMIN)).response
    assert created.status == "waiting_approval"
    assert (await service.get(created.run_id, ADMIN)) == created
    approved = await service.approve(created.run_id, True, ADMIN)
    assert approved.status == "approved"
    assert service.active_run_ids == frozenset()
    assert await service.wait_until_idle(0.01) is True


async def test_unknown_or_malformed_run_ids_raise_not_found(fake_llm: FakeLLMAdapter):
    service = make_service(fake_llm)
    for run_id in ("missing", str(uuid.uuid4())):
        with pytest.raises(RunNotFoundError):
            await service.get(run_id, ADMIN)
        with pytest.raises(RunNotFoundError):
            await service.approve(run_id, True, ADMIN)


async def test_run_reads_as_running_while_this_process_drives_it(fake_llm: FakeLLMAdapter):
    gated = GatedLLM(fake_llm)
    service = make_service(gated)
    run_id = str(uuid.uuid4())
    pending = asyncio.create_task(service.create(TASK, ADMIN, run_id=run_id))
    await asyncio.to_thread(gated.entered.wait, 10)

    assert service.active_run_ids == frozenset({run_id})
    assert (await service.get(run_id, ADMIN)).status == "running"
    assert await service.wait_until_idle(0.05) is False

    gated.release.set()
    result = (await pending).response
    assert result.status == "waiting_approval"
    assert await service.wait_until_idle(1) is True
    assert service.active_run_ids == frozenset()


async def test_concurrent_approvals_apply_one_decision(fake_llm: FakeLLMAdapter):
    repository = InMemoryRunRepository()
    service = make_service(fake_llm, repository=repository)
    run_id = (await service.create(TASK, ADMIN)).response.run_id
    calls_before = len(fake_llm.calls)

    outcomes = await asyncio.gather(
        service.approve(run_id, True, ADMIN),
        service.approve(run_id, False, ADMIN),
        service.approve(run_id, True, ADMIN),
        return_exceptions=True,
    )

    responses = [o for o in outcomes if not isinstance(o, BaseException)]
    errors = [o for o in outcomes if isinstance(o, BaseException)]
    # The first caller claims; the contradictory one is refused; the same decision replays.
    assert len(errors) == 1 and isinstance(errors[0], RunAlreadyDecidedError)
    assert {r.status for r in responses} == {"approved"}
    assert len(fake_llm.calls) == calls_before  # resuming never calls the LLM
    assert len(repository.approvals) == 1


async def test_the_llm_runs_off_the_event_loop(fake_llm: FakeLLMAdapter):
    """A blocking LLM call must not stall other coroutines (sync nodes run in a thread)."""
    gated = GatedLLM(fake_llm)
    service = make_service(gated)
    pending = asyncio.create_task(service.create(TASK, ADMIN, run_id=str(uuid.uuid4())))
    await asyncio.to_thread(gated.entered.wait, 10)
    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0.01)
        ticks += 1
    gated.release.set()
    await pending
    assert ticks == 5


def test_graph_and_repository_are_exposed_read_only(fake_llm: FakeLLMAdapter):
    graph = build_graph(fake_llm)
    repository = InMemoryRunRepository()
    service = RunService(graph, repository, authorization=LENIENT)
    assert service.graph is graph and service.repository is repository
    assert (
        RunService(graph, repository).authorization.separation_of_duties is True
    )  # strict by default

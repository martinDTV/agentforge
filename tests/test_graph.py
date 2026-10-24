import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import ValidationError

from app.contracts.execution import RunResponse
from app.contracts.planning import ArchitectureReview, DevelopmentPlan
from app.infrastructure.llm.errors import LLMMalformedOutputError, LLMTimeoutError
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.orchestration.graph import BUDGET_EXCEEDED_CODE, RunGraph, build_graph
from app.orchestration.limits import RunLimits
from app.orchestration.status import (
    INTERNAL_ERROR_CODE,
    derive_error,
    derive_status,
    run_response_from_snapshot,
)

RUN_ID = "run-1"
CONFIG: RunnableConfig = {"configurable": {"thread_id": RUN_ID}}
TASK = "Diseña un módulo de pacientes con pruebas"


def run_graph(llm: FakeLLMAdapter, limits: RunLimits | None = None) -> tuple[RunGraph, RunResponse]:
    graph = build_graph(llm, limits)
    graph.invoke({"task": TASK}, CONFIG)
    return graph, run_response_from_snapshot(RUN_ID, graph.get_state(CONFIG))


def test_first_plan_approved_by_architect_waits_for_human(fake_llm: FakeLLMAdapter):
    _, result = run_graph(fake_llm)
    assert result.status == "waiting_approval"
    assert result.revision == 0
    assert result.llm_calls == 2
    assert result.architect_approved is True
    assert result.error is None


def test_architect_rejection_triggers_revision_with_feedback(
    fake_llm: FakeLLMAdapter,
    sample_plan: DevelopmentPlan,
    revised_plan: DevelopmentPlan,
    sample_review: ArchitectureReview,
    rejecting_review: ArchitectureReview,
):
    fake_llm.register_sequence(DevelopmentPlan, [sample_plan, revised_plan])
    fake_llm.register_sequence(ArchitectureReview, [rejecting_review, sample_review])

    _, result = run_graph(fake_llm)

    assert result.status == "waiting_approval"
    assert result.revision == 1
    assert result.llm_calls == 4
    assert result.architect_approved is True
    assert result.plan == revised_plan
    # planner, architect, planner (revision), architect
    assert [call[2] for call in fake_llm.calls] == [
        DevelopmentPlan,
        ArchitectureReview,
        DevelopmentPlan,
        ArchitectureReview,
    ]
    revision_prompt = fake_llm.calls[2][1]
    assert "Plan anterior" in revision_prompt
    assert "Falta manejo de errores" in revision_prompt
    assert "plan revisado número 1" in fake_llm.calls[3][1]


def test_revisions_stop_at_limit_and_defer_to_human(
    fake_llm: FakeLLMAdapter, rejecting_review: ArchitectureReview
):
    fake_llm.register(ArchitectureReview, rejecting_review)

    graph, result = run_graph(fake_llm, RunLimits(max_revisions=2, max_llm_calls=20))

    assert result.status == "waiting_approval"
    assert result.revision == 2
    assert result.llm_calls == 6
    assert result.architect_approved is False
    assert result.architecture is not None
    assert result.architecture.issues == ["Falta manejo de errores"]

    graph.invoke(Command(resume={"approved": True}), CONFIG)
    assert derive_status(graph.get_state(CONFIG)) == "approved"


def test_budget_prevents_revision_rounds_that_do_not_fit(
    fake_llm: FakeLLMAdapter, rejecting_review: ArchitectureReview
):
    fake_llm.register(ArchitectureReview, rejecting_review)

    _, result = run_graph(fake_llm, RunLimits(max_revisions=5, max_llm_calls=3))

    assert result.status == "waiting_approval"
    assert result.revision == 0
    assert result.llm_calls == 2
    assert result.architect_approved is False


def test_budget_hard_cap_fails_run_before_calling_the_llm(fake_llm: FakeLLMAdapter):
    _, result = run_graph(fake_llm, RunLimits(max_llm_calls=1))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == BUDGET_EXCEEDED_CODE
    assert result.llm_calls == 1
    assert result.plan is not None
    assert result.architecture is None
    assert len(fake_llm.calls) == 1


def test_planner_timeout_fails_run_without_plan(fake_llm: FakeLLMAdapter):
    fake_llm.register_sequence(DevelopmentPlan, [LLMTimeoutError("sin respuesta")])

    _, result = run_graph(fake_llm)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "llm_timeout"
    assert result.error.message == "sin respuesta"
    assert result.plan is None
    assert result.architect_approved is None
    assert result.llm_calls == 1


def test_architect_malformed_output_fails_run_but_keeps_plan(
    fake_llm: FakeLLMAdapter, sample_plan: DevelopmentPlan
):
    fake_llm.register_sequence(ArchitectureReview, [LLMMalformedOutputError("JSON inválido")])

    _, result = run_graph(fake_llm)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "llm_malformed_output"
    assert result.plan == sample_plan
    assert result.architecture is None
    assert result.llm_calls == 2


def test_unexpected_exception_propagates_and_run_reads_as_failed(fake_llm: FakeLLMAdapter):
    fake_llm.register_sequence(DevelopmentPlan, [RuntimeError("boom")])
    graph = build_graph(fake_llm)

    with pytest.raises(RuntimeError, match="boom"):
        graph.invoke({"task": TASK}, CONFIG)

    snapshot = graph.get_state(CONFIG)
    assert derive_status(snapshot) == "failed"
    error = derive_error(snapshot)
    assert error is not None
    assert error.code == INTERNAL_ERROR_CODE
    assert "boom" not in error.message


def test_human_rejection_is_terminal(fake_llm: FakeLLMAdapter):
    graph, _ = run_graph(fake_llm)
    graph.invoke(Command(resume={"approved": False}), CONFIG)
    snapshot = graph.get_state(CONFIG)
    assert derive_status(snapshot) == "rejected"
    assert derive_error(snapshot) is None


@pytest.mark.parametrize("kwargs", [{"max_llm_calls": 0}, {"max_revisions": -1}])
def test_run_limits_reject_invalid_values(kwargs: dict[str, int]):
    with pytest.raises(ValidationError):
        RunLimits(**kwargs)


def test_run_limits_are_frozen():
    limits = RunLimits()
    with pytest.raises(ValidationError):
        limits.max_revisions = 10  # type: ignore[misc]

import pytest

from app.agents.architect import ArchitectAgent
from app.agents.planner import PlannerAgent
from app.contracts.planning import ArchitectureReview, DevelopmentPlan
from app.infrastructure.llm.fake import FakeLLMAdapter


def test_planner_agent_returns_registered_plan(
    fake_llm: FakeLLMAdapter, sample_plan: DevelopmentPlan
):
    agent = PlannerAgent(fake_llm)
    result = agent.execute("Diseña un módulo de pacientes")
    assert result == sample_plan
    assert fake_llm.calls[0][2] is DevelopmentPlan


def test_architect_agent_returns_registered_review(
    fake_llm: FakeLLMAdapter, sample_plan: DevelopmentPlan, sample_review: ArchitectureReview
):
    agent = ArchitectAgent(fake_llm)
    result = agent.execute(task="Diseña un módulo de pacientes", plan=sample_plan)
    assert result == sample_review
    assert fake_llm.calls[0][2] is ArchitectureReview


def test_planner_agent_raises_when_schema_not_registered():
    empty_adapter = FakeLLMAdapter()
    agent = PlannerAgent(empty_adapter)
    with pytest.raises(LookupError):
        agent.execute("tarea cualquiera")


def test_planner_revision_prompt_includes_previous_plan_and_feedback(
    fake_llm: FakeLLMAdapter, sample_plan: DevelopmentPlan
):
    agent = PlannerAgent(fake_llm)
    agent.execute(
        "Diseña un módulo de pacientes",
        previous_plan=sample_plan,
        feedback=["Falta manejo de errores", "Sin pruebas"],
    )
    prompt = fake_llm.calls[0][1]
    assert "Plan anterior" in prompt
    assert sample_plan.summary in prompt
    assert "- Falta manejo de errores" in prompt
    assert "- Sin pruebas" in prompt


def test_planner_revision_without_specific_issues_is_still_a_revision(
    fake_llm: FakeLLMAdapter, sample_plan: DevelopmentPlan
):
    agent = PlannerAgent(fake_llm)
    agent.execute("Diseña un módulo de pacientes", previous_plan=sample_plan, feedback=[])
    assert "sin observaciones específicas" in fake_llm.calls[0][1]


def test_planner_first_call_sends_the_task_verbatim(fake_llm: FakeLLMAdapter):
    PlannerAgent(fake_llm).execute("Diseña un módulo de pacientes")
    assert fake_llm.calls[0][1] == "Diseña un módulo de pacientes"


def test_architect_mentions_revision_number_only_when_revising(
    fake_llm: FakeLLMAdapter, sample_plan: DevelopmentPlan
):
    agent = ArchitectAgent(fake_llm)
    agent.execute(task="t" * 10, plan=sample_plan)
    agent.execute(task="t" * 10, plan=sample_plan, revision=2)
    assert "plan revisado número" not in fake_llm.calls[0][1]
    assert "plan revisado número 2" in fake_llm.calls[1][1]


def test_fake_sequence_raises_registered_exception_then_is_exhausted(
    sample_plan: DevelopmentPlan,
):
    adapter = FakeLLMAdapter()
    adapter.register_sequence(DevelopmentPlan, [RuntimeError("boom"), sample_plan])
    agent = PlannerAgent(adapter)
    with pytest.raises(RuntimeError, match="boom"):
        agent.execute("tarea cualquiera")
    assert agent.execute("tarea cualquiera") == sample_plan
    with pytest.raises(LookupError, match="agotó"):
        agent.execute("tarea cualquiera")

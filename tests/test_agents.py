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

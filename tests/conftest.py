import pytest

from app.contracts.planning import ArchitectureReview, DevelopmentPlan, DevelopmentTask
from app.infrastructure.llm.fake import FakeLLMAdapter


@pytest.fixture
def sample_plan() -> DevelopmentPlan:
    return DevelopmentPlan(
        summary="Plan de prueba",
        tasks=[
            DevelopmentTask(
                id="t1",
                title="Analizar",
                description="Definir contratos",
                acceptance_criteria=["Contrato revisado"],
            )
        ],
        risks=["Ninguno relevante"],
    )


@pytest.fixture
def sample_review() -> ArchitectureReview:
    return ArchitectureReview(
        summary="Revisión de prueba",
        decisions=[],
        issues=[],
        approved=True,
    )


@pytest.fixture
def fake_llm(sample_plan: DevelopmentPlan, sample_review: ArchitectureReview) -> FakeLLMAdapter:
    adapter = FakeLLMAdapter()
    adapter.register(DevelopmentPlan, sample_plan)
    adapter.register(ArchitectureReview, sample_review)
    return adapter

"""Fixtures shared by the unit and the integration suites.

The unit suite (``tests/unit``, the default ``pytest`` target) needs no
external service. The integration suite (``tests/integration``) needs
PostgreSQL and is run explicitly. Neither may ever reach Vertex AI: the
autouse guard below makes constructing the real client fail loudly.
"""

import anthropic
import pytest

from app.contracts.planning import ArchitectureReview, DevelopmentPlan, DevelopmentTask
from app.infrastructure.llm.fake import FakeLLMAdapter


@pytest.fixture(autouse=True)
def _block_vertex_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(self: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError(
            "Vertex AI está bloqueado durante pytest: inyecta FakeLLMAdapter o un cliente stub"
        )

    monkeypatch.setattr(anthropic.AnthropicVertex, "__init__", _blocked)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


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
def revised_plan() -> DevelopmentPlan:
    return DevelopmentPlan(
        summary="Plan revisado",
        tasks=[
            DevelopmentTask(
                id="t1",
                title="Analizar",
                description="Definir contratos",
                acceptance_criteria=["Contrato revisado"],
            ),
            DevelopmentTask(
                id="t2",
                title="Manejar errores",
                description="Cubrir errores del proveedor",
                dependencies=["t1"],
                acceptance_criteria=["Errores cubiertos"],
            ),
        ],
        risks=[],
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
def rejecting_review() -> ArchitectureReview:
    return ArchitectureReview(
        summary="Plan incompleto",
        decisions=[],
        issues=["Falta manejo de errores"],
        approved=False,
    )


@pytest.fixture
def fake_llm(sample_plan: DevelopmentPlan, sample_review: ArchitectureReview) -> FakeLLMAdapter:
    """Approving fake: one plan, one approving review, on every call.

    Tests override individual schemas with ``register_sequence`` to drive
    revision loops and failures.
    """
    adapter = FakeLLMAdapter()
    adapter.register(DevelopmentPlan, sample_plan)
    adapter.register(ArchitectureReview, sample_review)
    return adapter

from app.contracts.planning import ArchitectureReview, DevelopmentPlan, DevelopmentTask
from app.settings import Settings, get_settings

from .errors import (
    LLMError,
    LLMMalformedOutputError,
    LLMRequestRejectedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from .fake import FakeLLMAdapter
from .port import LLMPort
from .vertex_claude import StructuredOutputError, VertexClaudeAdapter

__all__ = [
    "FakeLLMAdapter",
    "LLMError",
    "LLMMalformedOutputError",
    "LLMPort",
    "LLMRequestRejectedError",
    "LLMTimeoutError",
    "LLMUnavailableError",
    "StructuredOutputError",
    "VertexClaudeAdapter",
    "build_llm",
]


def _default_fake_adapter() -> FakeLLMAdapter:
    """Fake adapter preloaded with a minimal valid plan/review.

    Used when USE_FAKE_MODEL=true, so the API is runnable end-to-end for
    manual exploration without any network call or token cost.
    """
    adapter = FakeLLMAdapter()
    adapter.register(
        DevelopmentPlan,
        DevelopmentPlan(
            summary="Plan de demostración (modelo simulado)",
            tasks=[
                DevelopmentTask(
                    id="t1",
                    title="Analizar requerimiento",
                    description="Definir contratos y criterios de aceptación",
                    acceptance_criteria=["Contrato revisado"],
                )
            ],
            risks=["No se ejecuta código en esta fase"],
        ),
    )
    adapter.register(
        ArchitectureReview,
        ArchitectureReview(
            summary="Revisión de demostración (modelo simulado)",
            decisions=[],
            issues=[],
            approved=True,
        ),
    )
    return adapter


def build_llm(settings: Settings | None = None) -> LLMPort:
    settings = settings or get_settings()
    if settings.use_fake_model:
        return _default_fake_adapter()
    return VertexClaudeAdapter(settings)

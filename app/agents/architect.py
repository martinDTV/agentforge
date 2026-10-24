from app.contracts.planning import ArchitectureReview, DevelopmentPlan
from app.infrastructure.llm.port import LLMPort


class ArchitectAgent:
    def __init__(self, llm: LLMPort):
        self.llm = llm

    def execute(
        self,
        task: str,
        plan: DevelopmentPlan,
    ) -> ArchitectureReview:
        return self.llm.generate_structured(
            system_prompt=(
                "Eres un arquitecto principal de software. "
                "Analiza planes técnicos, detecta riesgos "
                "y propone decisiones arquitectónicas. "
                "Marca approved=false si existen problemas "
                "importantes sin resolver."
            ),
            user_prompt=(f"Solicitud:\n{task}\n\nPlan:\n{plan.model_dump_json(indent=2)}"),
            schema=ArchitectureReview,
        )

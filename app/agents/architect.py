from app.contracts.planning import ArchitectureReview, DevelopmentPlan
from app.infrastructure.llm.port import LLMPort

SYSTEM_PROMPT = (
    "Eres un arquitecto principal de software. "
    "Analiza planes técnicos, detecta riesgos "
    "y propone decisiones arquitectónicas. "
    "Marca approved=false solo si existen problemas "
    "importantes sin resolver y descríbelos en issues."
)


class ArchitectAgent:
    def __init__(self, llm: LLMPort):
        self.llm = llm

    def execute(
        self,
        task: str,
        plan: DevelopmentPlan,
        revision: int = 0,
    ) -> ArchitectureReview:
        revision_note = (
            f"Este es el plan revisado número {revision}. "
            "Si las observaciones anteriores fueron atendidas, marca approved=true.\n\n"
            if revision > 0
            else ""
        )
        return self.llm.generate_structured(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=(
                f"{revision_note}Solicitud:\n{task}\n\nPlan:\n{plan.model_dump_json(indent=2)}"
            ),
            schema=ArchitectureReview,
        )

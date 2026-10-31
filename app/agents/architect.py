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
        context: str | None = None,
    ) -> ArchitectureReview:
        revision_note = (
            f"Este es el plan revisado número {revision}. "
            "Si las observaciones anteriores fueron atendidas, marca approved=true.\n\n"
            if revision > 0
            else ""
        )
        prompt = f"{revision_note}Solicitud:\n{task}\n\nPlan:\n{plan.model_dump_json(indent=2)}"
        if context:
            # Platform constraints as data: the architect reviews against them but
            # cannot relax them; the deterministic gate already enforced them.
            prompt = f"{prompt}\n\n{context}"
        return self.llm.generate_structured(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
            schema=ArchitectureReview,
        )

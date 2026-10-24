from app.contracts.planning import DevelopmentPlan
from app.infrastructure.llm.port import LLMPort


class PlannerAgent:
    def __init__(self, llm: LLMPort):
        self.llm = llm

    def execute(self, task: str) -> DevelopmentPlan:
        return self.llm.generate_structured(
            system_prompt=(
                "Genera entre 3 y 5 tareas como máximo. "
                "Cada descripción debe tener máximo 180 caracteres. "
                "Cada tarea debe incluir de 1 a 3 criterios de aceptación. "
                "Identifica máximo 3 riesgos. "
                "No incluyas código, ejemplos ni explicaciones largas. "
            ),
            user_prompt=task,
            schema=DevelopmentPlan,
        )

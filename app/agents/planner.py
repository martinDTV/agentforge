from collections.abc import Sequence

from app.contracts.planning import DevelopmentPlan
from app.infrastructure.llm.port import LLMPort

SYSTEM_PROMPT = (
    "Genera entre 1 y 5 tareas como máximo: una sola si la solicitud es pequeña y "
    "autocontenida. "
    "Declara dependencies solo cuando una tarea necesite el resultado de otra y nunca "
    "formes ciclos. "
    "Cada descripción debe tener máximo 180 caracteres. "
    "Cada tarea debe incluir de 1 a 3 criterios de aceptación. "
    "Identifica máximo 3 riesgos. "
    "No incluyas código, ejemplos ni explicaciones largas. "
)


def _revision_prompt(task: str, previous_plan: DevelopmentPlan, feedback: Sequence[str]) -> str:
    issues = "\n".join(f"- {issue}" for issue in feedback) or "- (sin observaciones específicas)"
    return (
        f"Solicitud:\n{task}\n\n"
        f"Plan anterior:\n{previous_plan.model_dump_json(indent=2)}\n\n"
        f"Observaciones del arquitecto que debes resolver:\n{issues}\n\n"
        "Genera un plan revisado que atienda cada observación. "
        "Conserva lo que ya era correcto."
    )


class PlannerAgent:
    def __init__(self, llm: LLMPort):
        self.llm = llm

    def execute(
        self,
        task: str,
        previous_plan: DevelopmentPlan | None = None,
        feedback: Sequence[str] | None = None,
    ) -> DevelopmentPlan:
        """Produce a plan for ``task``.

        When ``previous_plan`` is given the call is a revision round: the
        prompt includes the earlier plan and the architect's ``feedback`` so
        the model fixes what was rejected instead of starting from scratch.
        """
        user_prompt = (
            task if previous_plan is None else _revision_prompt(task, previous_plan, feedback or [])
        )
        return self.llm.generate_structured(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema=DevelopmentPlan,
        )

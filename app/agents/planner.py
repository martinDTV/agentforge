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


def _with_context(prompt: str, context: str | None) -> str:
    """Platform facts travel as data after the request; they never become instructions."""
    return prompt if not context else f"{prompt}\n\n{context}"


def _revision_prompt(task: str, previous_plan: DevelopmentPlan, feedback: Sequence[str]) -> str:
    issues = "\n".join(f"- {issue}" for issue in feedback) or "- (sin observaciones específicas)"
    return (
        f"Solicitud:\n{task}\n\n"
        f"Plan anterior:\n{previous_plan.model_dump_json(indent=2)}\n\n"
        f"Observaciones que debes resolver (del arquitecto o de la plataforma):\n{issues}\n\n"
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
        context: str | None = None,
    ) -> DevelopmentPlan:
        """Produce a plan for ``task``.

        When ``previous_plan`` is given the call is a revision round: the
        prompt includes the earlier plan and the ``feedback`` (the architect's
        issues or the platform gate's violations) so the model fixes what was
        rejected instead of starting from scratch. ``context`` is the
        platform's deterministic constraints, appended as data.
        """
        user_prompt = (
            task if previous_plan is None else _revision_prompt(task, previous_plan, feedback or [])
        )
        return self.llm.generate_structured(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=_with_context(user_prompt, context),
            schema=DevelopmentPlan,
        )

import operator
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.agents.architect import ArchitectAgent
from app.agents.planner import PlannerAgent
from app.contracts.execution import RunError
from app.contracts.planning import ArchitectureReview, DevelopmentPlan
from app.infrastructure.llm.errors import LLMError
from app.infrastructure.llm.port import LLMPort
from app.orchestration.limits import RunLimits

BUDGET_EXCEEDED_CODE = "llm_budget_exceeded"


class RunState(TypedDict, total=False):
    """Every key any workflow writes.

    One shared channel set, on purpose: a process that compiled only the
    planning graph (the API) must be able to load a development checkpoint,
    derive its status and back-fill its audit from the history. Keys below
    ``error`` are written by the development workflow only (ADR 0008).
    """

    workflow_kind: str
    task: str
    plan: dict[str, Any]
    architecture: dict[str, Any]
    feedback: list[str]
    revision: int
    llm_calls: Annotated[int, operator.add]
    architect_approved: bool
    approved: bool
    completed: bool  # workflows without a human decision (development)
    error: dict[str, Any]
    changes: dict[str, Any]
    file_hashes: dict[str, str | None]
    developer_checks: list[dict[str, Any]]
    qa_review: dict[str, Any]
    validation: list[dict[str, Any]]
    prior_validation: list[dict[str, Any]]
    usage: dict[str, Any]
    changed_files: list[str]
    final_diff: dict[str, Any]


RunGraph = CompiledStateGraph[RunState, None, RunState, RunState]
RunStateGraph = StateGraph[RunState, None, RunState, RunState]


def _error_payload(exc: LLMError) -> dict[str, Any]:
    return RunError(code=exc.code, message=exc.message).model_dump()


def build_state_graph(llm: LLMPort, limits: RunLimits | None = None) -> RunStateGraph:
    """Assemble (not compile) the Planner <-> Architect -> Approval graph.

    Flow:
      START -> planner -> architect -> approval -> END
                 ^            |
                 +-- revise --+   (architect rejected, revisions and budget left)

    Any node may instead write ``error`` and route to END, which the API
    reports as ``failed``. Only ``LLMError`` is handled this way: anything
    else is a bug and is allowed to propagate.

    Injecting the port here (instead of each agent importing a concrete
    adapter) keeps the orchestrator free of any LLM provider dependency and
    lets tests compile the same graph shape against a FakeLLMAdapter.
    """
    limits = limits or RunLimits()
    planner = PlannerAgent(llm)
    architect = ArchitectAgent(llm)

    def budget_exhausted(state: RunState) -> dict[str, Any] | None:
        if state.get("llm_calls", 0) < limits.max_llm_calls:
            return None
        error = RunError(
            code=BUDGET_EXCEEDED_CODE,
            message=(
                f"Se alcanzó el límite de {limits.max_llm_calls} llamadas al LLM por ejecución."
            ),
        )
        return {"error": error.model_dump()}

    def planner_node(state: RunState) -> dict[str, Any]:
        if (blocked := budget_exhausted(state)) is not None:
            return blocked
        previous = DevelopmentPlan.model_validate(state["plan"]) if "plan" in state else None
        revision = state.get("revision", 0) + 1 if previous is not None else 0
        try:
            plan = planner.execute(
                state["task"],
                previous_plan=previous,
                feedback=state.get("feedback", []),
            )
        except LLMError as exc:
            return {"llm_calls": 1, "error": _error_payload(exc)}
        return {"plan": plan.model_dump(), "revision": revision, "llm_calls": 1}

    def architect_node(state: RunState) -> dict[str, Any]:
        if (blocked := budget_exhausted(state)) is not None:
            return blocked
        plan = DevelopmentPlan.model_validate(state["plan"])
        try:
            review: ArchitectureReview = architect.execute(
                task=state["task"], plan=plan, revision=state.get("revision", 0)
            )
        except LLMError as exc:
            return {"llm_calls": 1, "error": _error_payload(exc)}
        return {
            "architecture": review.model_dump(),
            "architect_approved": review.approved,
            "feedback": review.issues,
            "llm_calls": 1,
        }

    def approval_node(state: RunState) -> dict[str, Any]:
        response = interrupt(
            {
                "kind": "approve_plan",
                "plan": state["plan"],
                "architecture": state["architecture"],
                "architect_approved": state.get("architect_approved"),
                "revision": state.get("revision", 0),
            }
        )
        if not isinstance(response, dict) or type(response.get("approved")) is not bool:
            raise ValueError("Aprobación inválida")
        return {"approved": response["approved"]}

    def route_after_planner(state: RunState) -> str:
        return END if "error" in state else "architect"

    def route_after_architect(state: RunState) -> str:
        if "error" in state:
            return END
        if state.get("architect_approved"):
            return "approval"
        revisions_left = state.get("revision", 0) < limits.max_revisions
        round_fits_budget = state.get("llm_calls", 0) + 2 <= limits.max_llm_calls
        # Out of revisions or budget: the human still gets the final say, with
        # the architect's objections visible in the response.
        return "planner" if revisions_left and round_fits_budget else "approval"

    builder = StateGraph(RunState)
    builder.add_node("planner", planner_node)
    builder.add_node("architect", architect_node)
    builder.add_node("approval", approval_node)
    builder.add_edge(START, "planner")
    builder.add_conditional_edges(
        "planner", route_after_planner, {"architect": "architect", END: END}
    )
    builder.add_conditional_edges(
        "architect",
        route_after_architect,
        {"planner": "planner", "approval": "approval", END: END},
    )
    builder.add_edge("approval", END)
    return builder


def build_graph(
    llm: LLMPort,
    limits: RunLimits | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> RunGraph:
    """Compile the run graph against a checkpointer.

    ``InMemorySaver`` by default (unit tests, demos). Production injects the
    ``AsyncPostgresSaver`` opened in lifespan; the orchestrator only depends
    on the ``BaseCheckpointSaver`` interface, never on a concrete backend.
    """
    return build_state_graph(llm, limits).compile(checkpointer=checkpointer or InMemorySaver())

from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.agents.architect import ArchitectAgent
from app.agents.planner import PlannerAgent
from app.contracts.planning import ArchitectureReview, DevelopmentPlan
from app.infrastructure.llm.port import LLMPort


class RunState(TypedDict, total=False):
    task: str
    plan: dict[str, Any]
    architecture: dict[str, Any]
    approved: bool


def build_graph(llm: LLMPort) -> CompiledStateGraph[RunState, None, RunState, RunState]:
    """Compile the Planner -> Architect -> Approval graph for a given LLMPort.

    Injecting the port here (instead of each agent importing a concrete
    adapter) keeps the orchestrator free of any LLM provider dependency and
    lets tests compile the same graph shape against a FakeLLMAdapter.
    """
    planner = PlannerAgent(llm)
    architect = ArchitectAgent(llm)

    def planner_node(state: RunState) -> dict[str, Any]:
        plan: DevelopmentPlan = planner.execute(state["task"])
        return {"plan": plan.model_dump()}

    def architect_node(state: RunState) -> dict[str, Any]:
        plan = DevelopmentPlan.model_validate(state["plan"])
        review: ArchitectureReview = architect.execute(task=state["task"], plan=plan)
        return {"architecture": review.model_dump()}

    def approval_node(state: RunState) -> dict[str, Any]:
        response = interrupt(
            {
                "kind": "approve_plan",
                "plan": state["plan"],
                "architecture": state["architecture"],
            }
        )
        if not isinstance(response, dict) or type(response.get("approved")) is not bool:
            raise ValueError("Aprobación inválida")
        return {"approved": response["approved"]}

    builder = StateGraph(RunState)
    builder.add_node("planner", planner_node)
    builder.add_node("architect", architect_node)
    builder.add_node("approval", approval_node)
    builder.add_edge(START, "planner")
    builder.add_edge("planner", "architect")
    builder.add_edge("architect", "approval")
    builder.add_edge("approval", END)
    return builder.compile(checkpointer=InMemorySaver())

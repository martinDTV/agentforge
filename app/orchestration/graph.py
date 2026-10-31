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
from app.contracts.planning import (
    PLAN_INVALID_CODE,
    ArchitectureReview,
    DevelopmentPlan,
    PlanConstraints,
)
from app.infrastructure.llm.errors import LLMError
from app.infrastructure.llm.port import LLMPort
from app.orchestration.limits import RunLimits
from app.projects.plan_gate import policy_context_text, validate_plan

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
    # Governed development (Fase 7): where the run works and what a human approved.
    project_id: str
    repository_id: str
    workspace_root: str
    scope_proposal: dict[str, Any]
    approved_scope: dict[str, Any]
    development_deadline: str
    # Publication (Fase 7, Paso 3; ADR 0011): the frozen plan, the progress snapshot
    # and the positive markers the status derivation relies on.
    publication_plan: dict[str, Any]
    publication: dict[str, Any]
    publish_approved: bool
    publish_decided_by: str
    published: bool
    publication_interrupted: bool
    # Smart Router: requested mode, route followed, the recorded decision and the
    # plan verdict of the full route (``approved`` stays the planning workflow's).
    route_mode: str
    route: str
    route_decision: dict[str, Any]
    route_escalation: dict[str, Any]
    plan_approved: bool
    # Goal Supervisor (ADR 0012, D4): the child comes from an approved goal plan.
    planned_upstream: dict[str, Any]
    # Planning gate (H1): deterministic constraints the plan must respect (data built by
    # the platform) and the violations found in the current plan, kept as evidence.
    plan_constraints: dict[str, Any]
    plan_violations: list[dict[str, Any]]
    # Written by the governed qa node when QA approves: True when the repository has a
    # remote, so the run continues into publication instead of ending ``completed``.
    publication_expected: bool


RunGraph = CompiledStateGraph[RunState, None, RunState, RunState]
RunStateGraph = StateGraph[RunState, None, RunState, RunState]


def _error_payload(exc: LLMError) -> dict[str, Any]:
    return RunError(code=exc.code, message=exc.message).model_dump()


class PlanningNodes:
    """Planner and Architect node functions, shared by the planning graph and the
    full route of the development graph (Smart Router)."""

    def __init__(self, llm: LLMPort, limits: RunLimits, *, approval: str = "approval") -> None:
        self.limits = limits
        self._approval = approval
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

        def constraints_of(state: RunState) -> PlanConstraints | None:
            raw = state.get("plan_constraints")
            return PlanConstraints.model_validate(raw) if raw else None

        def context_of(state: RunState) -> str | None:
            constraints = constraints_of(state)
            return policy_context_text(constraints) if constraints is not None else None

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
                    context=context_of(state),
                )
            except LLMError as exc:
                return {"llm_calls": 1, "error": _error_payload(exc)}
            return {"plan": plan.model_dump(), "revision": revision, "llm_calls": 1}

        def plan_gate_node(state: RunState) -> dict[str, Any]:
            """Deterministic check of the plan against the platform constraints (H1).

            No model call. A violated plan goes back to the planner with the
            violations as feedback while revisions and budget allow it; otherwise
            the run fails ``plan_invalid`` keeping the plan and the violations as
            evidence. A human never sees a plan that breaks the constraints.
            """
            constraints = constraints_of(state)
            if constraints is None:
                return {"plan_violations": []}
            plan = DevelopmentPlan.model_validate(state["plan"])
            violations = [v.model_dump() for v in validate_plan(plan, constraints)]
            if not violations:
                return {"plan_violations": [], "feedback": []}
            revisions_left = state.get("revision", 0) < limits.max_revisions
            round_fits_budget = state.get("llm_calls", 0) + 2 <= limits.max_llm_calls
            if revisions_left and round_fits_budget:
                return {
                    "plan_violations": violations,
                    "feedback": [str(v["detail"]) for v in violations],
                }
            summary = "; ".join(str(v["detail"]) for v in violations)
            error = RunError(
                code=PLAN_INVALID_CODE,
                message=(
                    "El plan no cumple las restricciones de la plataforma tras las revisiones "
                    f"permitidas: {summary}"
                )[:2000],
            )
            return {"plan_violations": violations, "error": error.model_dump()}

        def architect_node(state: RunState) -> dict[str, Any]:
            if (blocked := budget_exhausted(state)) is not None:
                return blocked
            plan = DevelopmentPlan.model_validate(state["plan"])
            try:
                review: ArchitectureReview = architect.execute(
                    task=state["task"],
                    plan=plan,
                    revision=state.get("revision", 0),
                    context=context_of(state),
                )
            except LLMError as exc:
                return {"llm_calls": 1, "error": _error_payload(exc)}
            return {
                "architecture": review.model_dump(),
                "architect_approved": review.approved,
                "feedback": review.issues,
                "llm_calls": 1,
            }

        self.planner = planner_node
        self.plan_gate = plan_gate_node
        self.architect = architect_node

    @staticmethod
    def route_after_planner(state: RunState) -> str:
        return END if "error" in state else "plan_gate"

    @staticmethod
    def route_after_gate(state: RunState) -> str:
        if "error" in state:
            return END
        return "planner" if state.get("plan_violations") else "architect"

    def route_after_architect(self, state: RunState) -> str:
        if "error" in state:
            return END
        if state.get("architect_approved"):
            return self._approval
        revisions_left = state.get("revision", 0) < self.limits.max_revisions
        round_fits_budget = state.get("llm_calls", 0) + 2 <= self.limits.max_llm_calls
        # Out of revisions or budget: the human still gets the final say, with
        # the architect's objections visible in the response.
        return "planner" if revisions_left and round_fits_budget else self._approval


def build_state_graph(llm: LLMPort, limits: RunLimits | None = None) -> RunStateGraph:
    """Assemble (not compile) the Planner <-> Architect -> Approval graph.

    Flow:
      START -> planner -> plan_gate -> architect -> approval -> END
                 ^            |            |
                 +-- revise --+------------+   (gate violation or architect rejection,
                                                revisions and budget left)

    Any node may instead write ``error`` and route to END, which the API
    reports as ``failed``. Only ``LLMError`` is handled this way: anything
    else is a bug and is allowed to propagate.

    Injecting the port here (instead of each agent importing a concrete
    adapter) keeps the orchestrator free of any LLM provider dependency and
    lets tests compile the same graph shape against a FakeLLMAdapter.
    """
    limits = limits or RunLimits()
    nodes = PlanningNodes(llm, limits)

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

    builder = StateGraph(RunState)
    builder.add_node("planner", nodes.planner)
    builder.add_node("plan_gate", nodes.plan_gate)
    builder.add_node("architect", nodes.architect)
    builder.add_node("approval", approval_node)
    builder.add_edge(START, "planner")
    builder.add_conditional_edges(
        "planner", nodes.route_after_planner, {"plan_gate": "plan_gate", END: END}
    )
    builder.add_conditional_edges(
        "plan_gate",
        nodes.route_after_gate,
        {"planner": "planner", "architect": "architect", END: END},
    )
    builder.add_conditional_edges(
        "architect",
        nodes.route_after_architect,
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

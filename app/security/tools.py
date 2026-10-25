"""Who may invoke which tool (Fase 5, ADR 0006): deny by default, from a trusted context.

Every input to a decision comes from the orchestration, never from the model:
the principal is the automation identity minted for the run, the agent kind
is the node that is executing, the project is the run's project. The model
supplies a tool name and arguments, and neither can widen anything decided
here. A request for "more permissions" inside the arguments is just invalid
input for the tool's schema.
"""

from collections.abc import Mapping
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from app.contracts.tools import AgentKind, RiskClass

from .automation import AUTOMATION_ISSUER
from .policy import AuthorizationPolicy, Permission
from .principal import Principal


class ToolLike(Protocol):
    """What the policy needs to know about a tool; satisfied by ``ToolSpec``."""

    @property
    def name(self) -> str: ...

    @property
    def risk(self) -> RiskClass: ...


class ToolContextLike(Protocol):
    """What the policy needs from the execution context; satisfied by ``ToolContext``."""

    @property
    def project_id(self) -> str: ...

    @property
    def agent(self) -> AgentKind: ...


class AgentGrant(BaseModel):
    """Tools an agent may call: exact names or ``prefix.*`` patterns, within risk classes.

    Both conditions must hold: a ``workspace.*`` grant limited to ``read``
    does not cover a future ``workspace.write`` tool.
    """

    model_config = ConfigDict(frozen=True)

    tools: frozenset[str] = frozenset()
    risks: frozenset[RiskClass] = frozenset()

    def covers(self, tool: ToolLike) -> bool:
        if tool.risk not in self.risks:
            return False
        for pattern in self.tools:
            if pattern == tool.name:
                return True
            if pattern.endswith(".*") and tool.name.startswith(pattern[:-1]):
                return True
        return False


_WORKSPACE_READ = AgentGrant(
    tools=frozenset({"workspace.*", "git.*"}), risks=frozenset({RiskClass.READ})
)

DEFAULT_AGENT_GRANTS: Mapping[AgentKind, AgentGrant] = {
    AgentKind.PLANNER: AgentGrant(),  # plans from the task text alone
    AgentKind.ARCHITECT: _WORKSPACE_READ,
    AgentKind.DEVELOPER: _WORKSPACE_READ,  # write and exec arrive with the sandbox
    AgentKind.QA: _WORKSPACE_READ,
    AgentKind.SECURITY: _WORKSPACE_READ,
}

# Risk classes the platform can execute today. Paso 1 only observes: write,
# exec and network stay refused, whatever the grants say, until the sandbox
# and the Git adapter exist.
ENABLED_RISK_CLASSES: frozenset[RiskClass] = frozenset({RiskClass.READ})

DecisionOutcome = Literal["allowed", "denied"]


class ToolDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: DecisionOutcome
    code: str = ""
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == "allowed"


TOOL_ALLOWED = ToolDecision(outcome="allowed")


def _denied(code: str, reason: str) -> ToolDecision:
    return ToolDecision(outcome="denied", code=code, reason=reason)


class ToolAuthorizationPolicy:
    """Composes the run authorization policy: same roles, same project rules."""

    def __init__(
        self,
        authorization: AuthorizationPolicy,
        grants: Mapping[AgentKind, AgentGrant] | None = None,
        *,
        enabled_risks: frozenset[RiskClass] = ENABLED_RISK_CLASSES,
        trusted_issuers: frozenset[str] = frozenset({AUTOMATION_ISSUER}),
    ) -> None:
        self._authz = authorization
        self._grants = dict(grants if grants is not None else DEFAULT_AGENT_GRANTS)
        self._enabled = enabled_risks
        self._issuers = trusted_issuers

    @property
    def enabled_risks(self) -> frozenset[RiskClass]:
        return self._enabled

    def grant_for(self, agent: AgentKind) -> AgentGrant:
        return self._grants.get(agent, AgentGrant())

    def decide(
        self, principal: Principal, context: ToolContextLike, tool: ToolLike
    ) -> ToolDecision:
        """Checked in order: who, permission, project, agent grant, risk gate."""
        if principal.kind != "service" or principal.issuer not in self._issuers:
            return _denied(
                "untrusted_principal",
                "solo la identidad de automatización puede invocar herramientas",
            )
        if not self._authz.has(principal, Permission.TOOLS_EXECUTE):
            return _denied("permission_denied", "falta el permiso tools:execute")
        if not self._authz.can_access_project(principal, context.project_id):
            return _denied(
                "project_denied", f"el principal no pertenece al proyecto '{context.project_id}'"
            )
        if not self.grant_for(context.agent).covers(tool):
            return _denied(
                "tool_not_granted",
                f"el agente '{context.agent}' no tiene concedida la herramienta '{tool.name}'",
            )
        if tool.risk not in self._enabled:
            return _denied(
                "risk_not_enabled",
                f"la clase de riesgo '{tool.risk}' no está habilitada en esta fase",
            )
        return TOOL_ALLOWED

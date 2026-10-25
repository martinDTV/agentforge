"""Builders shared by the Tool Gateway unit tests (not collected by pytest)."""

import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path

from app.contracts.tools import AgentKind, RiskClass
from app.infrastructure.db import InMemoryToolInvocationRepository
from app.security import (
    AgentGrant,
    AuthorizationPolicy,
    AutomationIdentity,
    ToolAuthorizationPolicy,
)
from app.tools import ToolContext, ToolGateway, ToolLimits, ToolRegistry
from app.tools.ports import ToolPort

IDENTITY = AutomationIdentity()
PROJECT = "alpha"

# Grants used by the gateway tests: developers may call simulated and
# workspace tools in read AND write classes, so the global risk gate (not the
# grant) is what refuses write tools.
TEST_GRANTS: Mapping[AgentKind, AgentGrant] = {
    AgentKind.PLANNER: AgentGrant(),
    AgentKind.DEVELOPER: AgentGrant(
        tools=frozenset({"fake.*", "workspace.*"}),
        risks=frozenset({RiskClass.READ, RiskClass.WRITE}),
    ),
    AgentKind.SECURITY: AgentGrant(
        tools=frozenset({"workspace.read_file"}), risks=frozenset({RiskClass.READ})
    ),
}


def make_context(
    agent: AgentKind = AgentKind.DEVELOPER,
    *,
    project: str = PROJECT,
    run_id: uuid.UUID | None = None,
    attempt: int = 0,
    workspace: Path | None = None,
) -> ToolContext:
    return ToolContext(
        run_id=run_id or uuid.uuid4(),
        attempt=attempt,
        project_id=project,
        agent=agent,
        principal=IDENTITY.principal_for(agent, project),
        workspace_root=workspace,
    )


def make_gateway(
    tools: Iterable[ToolPort],
    *,
    limits: ToolLimits | None = None,
    grants: Mapping[AgentKind, AgentGrant] | None = None,
    audit: InMemoryToolInvocationRepository | None = None,
    enabled_risks: frozenset[RiskClass] | None = None,
) -> tuple[ToolGateway, InMemoryToolInvocationRepository]:
    """``enabled_risks`` widens the global risk gate for tests of the write
    contracts only; the product keeps it at ``{read}``."""
    sink = audit or InMemoryToolInvocationRepository()
    policy = ToolAuthorizationPolicy(
        AuthorizationPolicy(),
        grants if grants is not None else TEST_GRANTS,
        enabled_risks=enabled_risks if enabled_risks is not None else frozenset({RiskClass.READ}),
    )
    return ToolGateway(ToolRegistry(tools), policy, sink, limits=limits), sink

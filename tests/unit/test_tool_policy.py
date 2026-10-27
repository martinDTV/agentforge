import pytest

from app.contracts.tools import AgentKind, RiskClass
from app.security import (
    AGENT_ROLE,
    DEFAULT_ROLE_PERMISSIONS,
    AgentGrant,
    AuthorizationPolicy,
    AutomationIdentity,
    Permission,
    Principal,
    Role,
    ToolAuthorizationPolicy,
)
from app.tools import default_registry
from app.tools.fake import EchoTool, StubTool
from tests.unit.tool_fixtures import make_context

READ_TOOLS = default_registry().names()


def test_automation_identity_is_a_service_principal_with_tools_only():
    principal = AutomationIdentity().principal_for(AgentKind.DEVELOPER, "alpha")
    assert principal.subject == "agentforge:developer"
    assert principal.kind == "service" and principal.issuer == "agentforge:automation"
    assert principal.roles == {AGENT_ROLE} and principal.projects == {"alpha"}
    authz = AuthorizationPolicy()
    assert authz.permissions_of(principal) == {Permission.TOOLS_EXECUTE}
    assert not authz.has(principal, Permission.RUNS_READ)
    assert not authz.has(principal, Permission.RUNS_APPROVE)
    assert DEFAULT_ROLE_PERMISSIONS[Role.AGENT] == {Permission.TOOLS_EXECUTE}
    # No human role carries tools:execute; repos:write and repos:publish belong
    # to the humans who may ask for worktree writes and publication (ADR 0009/0011).
    for role in Role:
        granted = DEFAULT_ROLE_PERMISSIONS[role]
        assert (Permission.TOOLS_EXECUTE in granted) is (role is Role.AGENT)
        assert (Permission.REPOS_WRITE in granted) is (
            role in (Role.PROJECT_OPERATOR, Role.ADMIN, Role.PLATFORM_ADMIN)
        )
        assert (Permission.REPOS_PUBLISH in granted) is (
            role in (Role.PROJECT_OPERATOR, Role.ADMIN, Role.PLATFORM_ADMIN)
        )


@pytest.mark.parametrize("agent", list(AgentKind))
@pytest.mark.parametrize("tool_name", READ_TOOLS)
def test_default_matrix_grants_workspace_reads_to_every_agent_except_the_planner(
    agent: AgentKind, tool_name: str
):
    policy = ToolAuthorizationPolicy(AuthorizationPolicy())
    tool = default_registry().get(tool_name)
    assert tool is not None
    context = make_context(agent)
    decision = policy.decide(context.principal, context, tool.spec)
    if agent is AgentKind.PLANNER:
        assert decision.code == "tool_not_granted"
    else:
        assert decision.allowed, decision


def test_tools_outside_the_grant_are_denied_for_everyone_by_default():
    policy = ToolAuthorizationPolicy(AuthorizationPolicy())
    for agent in AgentKind:
        context = make_context(agent)
        assert policy.decide(context.principal, context, EchoTool.spec).code == "tool_not_granted"


def test_risk_gate_refuses_write_tools_even_when_granted():
    grants = {
        AgentKind.DEVELOPER: AgentGrant(
            tools=frozenset({"workspace.*"}), risks=frozenset({RiskClass.READ, RiskClass.WRITE})
        )
    }
    policy = ToolAuthorizationPolicy(AuthorizationPolicy(), grants)
    context = make_context(AgentKind.DEVELOPER)
    writer = StubTool("workspace.write_file", RiskClass.WRITE)
    assert policy.decide(context.principal, context, writer.spec).code == "risk_not_enabled"
    runner = StubTool("workspace.exec", RiskClass.EXEC)  # not even in the grant's risks
    assert policy.decide(context.principal, context, runner.spec).code == "tool_not_granted"
    assert policy.enabled_risks == {RiskClass.READ}


def test_grant_patterns_match_prefixes_and_exact_names_only():
    grant = AgentGrant(
        tools=frozenset({"workspace.*", "fake.echo"}), risks=frozenset({RiskClass.READ})
    )
    assert grant.covers(StubTool("workspace.read_file", RiskClass.READ).spec)
    assert grant.covers(StubTool("fake.echo", RiskClass.READ).spec)
    assert not grant.covers(StubTool("fake.echo2", RiskClass.READ).spec)
    assert not grant.covers(StubTool("workspaces.read", RiskClass.READ).spec)
    assert not grant.covers(StubTool("workspace.read_file", RiskClass.WRITE).spec)


def test_only_the_automation_identity_can_invoke_tools():
    policy = ToolAuthorizationPolicy(AuthorizationPolicy())
    tool = default_registry().get("workspace.read_file")
    assert tool is not None
    context = make_context(AgentKind.DEVELOPER)
    human_admin = Principal(
        subject="root",
        issuer="https://idp",
        roles=frozenset({"platform_admin"}),
        projects=frozenset(),
    )
    assert policy.decide(human_admin, context, tool.spec).code == "untrusted_principal"
    foreign_service = Principal(
        subject="svc",
        kind="service",
        issuer="https://idp",
        roles=frozenset({"agent"}),
        projects=frozenset({"alpha"}),
    )
    assert policy.decide(foreign_service, context, tool.spec).code == "untrusted_principal"
    no_role = context.principal.model_copy(update={"roles": frozenset()})
    assert policy.decide(no_role, context, tool.spec).code == "permission_denied"


def test_project_boundaries_hold_for_agents_too():
    policy = ToolAuthorizationPolicy(AuthorizationPolicy())
    tool = default_registry().get("workspace.list_dir")
    assert tool is not None
    beta = make_context(AgentKind.QA, project="beta")
    alpha_principal = AutomationIdentity().principal_for(AgentKind.QA, "alpha")
    assert policy.decide(alpha_principal, beta, tool.spec).code == "project_denied"
    legacy = make_context(AgentKind.QA, project="legacy")  # reserved: no token claims it
    assert policy.decide(legacy.principal, legacy, tool.spec).code == "project_denied"

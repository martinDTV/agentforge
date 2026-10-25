"""The automation identity (Fase 5): agents act as *AgentForge*, never as a person.

Tool calls are attributed to a service principal minted per run by trusted
code. It carries exactly one role (``agent``, which maps to ``tools:execute``)
and exactly one project, so even a misbehaving agent cannot read, approve or
resume runs, nor reach another project. Nothing here reads the environment:
the operator's SSH, GitHub, cloud or shell credentials are never inherited.
"""

from app.contracts.tools import AgentKind

from .principal import Principal

AUTOMATION_ISSUER = "agentforge:automation"
AUTOMATION_NAME = "agentforge"
AGENT_ROLE = "agent"


class AutomationIdentity:
    def __init__(self, name: str = AUTOMATION_NAME, issuer: str = AUTOMATION_ISSUER) -> None:
        self.name = name
        self.issuer = issuer

    def subject_for(self, agent: AgentKind) -> str:
        return f"{self.name}:{agent.value}"

    def principal_for(self, agent: AgentKind, project_id: str) -> Principal:
        """A principal valid for one agent kind inside one project, and nothing else."""
        return Principal(
            subject=self.subject_for(agent),
            kind="service",
            issuer=self.issuer,
            roles=frozenset({AGENT_ROLE}),
            projects=frozenset({project_id}),
        )

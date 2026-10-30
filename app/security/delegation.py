"""Delegated identity of the goal Supervisor (Fase 8, ADR 0012 §2.4; decision D3).

Child runs of a goal belong to the human who created the goal, so separation of
duties is untouched: that human still cannot approve their own children's scope
or publication in production. The coordinator acts *as* that human through a
principal minted by trusted code with exactly one role, ``goal_delegate``, whose
permissions are ``runs:create``, ``repos:write`` and ``runs:cancel`` and nothing
else. ``issuer`` tells the audit that it was the platform, not a token.
"""

from datetime import datetime
from uuid import UUID

from .policy import Role
from .principal import Principal

DELEGATION_ISSUER = "agentforge:delegation"


def delegated_principal(
    *, owner_subject: str, project_id: str, goal_id: UUID, expires_at: datetime
) -> Principal:
    """A principal valid for one project, for the life of one goal, and nothing else."""
    return Principal(
        subject=owner_subject,
        kind="service",
        issuer=DELEGATION_ISSUER,
        roles=frozenset({Role.GOAL_DELEGATE.value}),
        projects=frozenset({project_id}),
        expires_at=expires_at,
        token_id=f"goal:{goal_id}",
    )


def is_delegated(principal: Principal) -> bool:
    return principal.issuer == DELEGATION_ISSUER

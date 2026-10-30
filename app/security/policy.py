"""Authorization policy (ADR 0005): deny by default, decided outside FastAPI and SQL.

Three ingredients, kept apart on purpose:

- **permissions** are the only thing code checks (``runs:approve``), never roles;
- **roles** come from the token and map to permissions through a table;
- **scope** comes from project membership (``projects`` claim) plus run
  ownership. ``projects:all`` is the explicit, separate permission that lets a
  platform administrator cross project boundaries; a plain ``admin`` acts
  only inside the projects its token names.

Decisions distinguish *hidden* (the caller must not even learn the run
exists: the API answers 404) from *forbidden* (the run is visible but the
operation is not allowed: 403).
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from .errors import PermissionDeniedError, ProjectRequiredError, SelfApprovalForbiddenError
from .principal import Principal


class Permission(StrEnum):
    RUNS_CREATE = "runs:create"
    RUNS_READ = "runs:read"
    RUNS_APPROVE = "runs:approve"
    RUNS_RESUME = "runs:resume"
    RUNS_AUDIT_READ = "runs:audit:read"
    RECOVERY_SWEEP = "recovery:sweep"
    # Crosses project boundaries. Granted only by an explicit global role.
    PROJECTS_ALL = "projects:all"
    # Withdraw a run that waits for a decision or sits interrupted (Fase 7).
    RUNS_CANCEL = "runs:cancel"
    # Held only by the automation identity's agents (Fase 5, ``app/security/tools.py``).
    TOOLS_EXECUTE = "tools:execute"
    # Create development runs that write inside a worktree and approve their
    # scope (Fase 7, ADR 0009). Humans only; agents never hold it.
    REPOS_WRITE = "repos:write"
    # Approve publication to a remote: a branch and a draft pull request (ADR 0011).
    # Humans only; agents never hold it.
    REPOS_PUBLISH = "repos:publish"


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    APPROVER = "approver"
    # Humans who may ask AgentForge to write inside a project's repositories
    # and approve the exact scope of those writes (ADR 0009).
    PROJECT_OPERATOR = "project_operator"
    ADMIN = "admin"  # every run permission, but only inside the token's projects
    PLATFORM_ADMIN = "platform_admin"  # admin + projects:all
    AGENT = "agent"  # the automation identity: tools only, never runs
    # The goal Supervisor acting *for* the human who created a goal (ADR 0012, D3):
    # may create child runs and cancel its own children; never approves, resumes
    # or publishes anything, and never holds tools:execute.
    GOAL_DELEGATE = "goal_delegate"


_P = Permission
_PROJECT_ADMIN = frozenset(
    {
        _P.RUNS_CREATE,
        _P.RUNS_READ,
        _P.RUNS_APPROVE,
        _P.RUNS_RESUME,
        _P.RUNS_CANCEL,
        _P.RUNS_AUDIT_READ,
        _P.RECOVERY_SWEEP,
        _P.REPOS_WRITE,
        _P.REPOS_PUBLISH,
    }
)
_PROJECT_OPERATOR = frozenset(
    {
        _P.RUNS_CREATE,
        _P.RUNS_READ,
        _P.RUNS_APPROVE,
        _P.RUNS_RESUME,
        _P.RUNS_CANCEL,
        _P.RUNS_AUDIT_READ,
        _P.REPOS_WRITE,
        _P.REPOS_PUBLISH,
    }
)

DEFAULT_ROLE_PERMISSIONS: Mapping[str, frozenset[Permission]] = {
    Role.VIEWER: frozenset({_P.RUNS_READ, _P.RUNS_AUDIT_READ}),
    Role.OPERATOR: frozenset(
        {_P.RUNS_READ, _P.RUNS_AUDIT_READ, _P.RUNS_CREATE, _P.RUNS_RESUME, _P.RUNS_CANCEL}
    ),
    Role.APPROVER: frozenset({_P.RUNS_READ, _P.RUNS_AUDIT_READ, _P.RUNS_APPROVE}),
    Role.PROJECT_OPERATOR: _PROJECT_OPERATOR,
    Role.ADMIN: _PROJECT_ADMIN,
    Role.PLATFORM_ADMIN: _PROJECT_ADMIN | {_P.PROJECTS_ALL},
    Role.AGENT: frozenset({_P.TOOLS_EXECUTE}),
    Role.GOAL_DELEGATE: frozenset({_P.RUNS_CREATE, _P.REPOS_WRITE, _P.RUNS_CANCEL}),
}

# Rows that pre-date authentication live here (migration 0003). No token can
# claim this project: only ``projects:all`` reaches it.
LEGACY_PROJECT = "legacy"
LEGACY_OWNER = "legacy:unowned"
RESERVED_PROJECTS: frozenset[str] = frozenset({LEGACY_PROJECT})


class OwnedRun(Protocol):
    """What the policy needs to know about a run; satisfied by ``PersistedRun``."""

    @property
    def owner_subject(self) -> str: ...

    @property
    def project_id(self) -> str: ...


Outcome = Literal["allowed", "hidden", "forbidden"]


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == "allowed"


ALLOWED = Decision(outcome="allowed")


class AccessScope(BaseModel):
    """Which rows a principal may list or sweep; re-applied on every page.

    ``projects=None`` means every project (``projects:all``); an empty set
    means nothing at all. ``owner_only`` restricts a listing to the
    principal's own runs when it lacks ``runs:read`` in its projects.
    """

    model_config = ConfigDict(frozen=True)

    projects: frozenset[str] | None
    owner_only: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.projects is not None and not self.projects

    def narrowed_to(self, project_id: str | None) -> "AccessScope":
        """Apply a caller-supplied project filter without widening the scope."""
        if project_id is None:
            return self
        if self.projects is None or project_id in self.projects:
            return self.model_copy(update={"projects": frozenset({project_id})})
        return self.model_copy(update={"projects": frozenset()})


class AuthorizationPolicy:
    def __init__(
        self,
        role_permissions: Mapping[str, frozenset[Permission]] | None = None,
        *,
        separation_of_duties: bool = True,
        publish_distinct_approver: bool | None = None,
        reserved_projects: frozenset[str] = RESERVED_PROJECTS,
    ) -> None:
        self._roles = dict(role_permissions or DEFAULT_ROLE_PERMISSIONS)
        self.separation_of_duties = separation_of_duties
        # Publication (ADR 0011): besides never approving one's own run, the
        # publisher must differ from the scope approver (four real eyes).
        self.publish_distinct_approver = (
            separation_of_duties if publish_distinct_approver is None else publish_distinct_approver
        )
        self._reserved = reserved_projects

    # -- permissions --------------------------------------------------------------

    def permissions_of(self, principal: Principal) -> frozenset[Permission]:
        granted: set[Permission] = set()
        for role in principal.roles:
            granted |= self._roles.get(role, frozenset())  # unknown roles grant nothing
        return frozenset(granted)

    def has(self, principal: Principal, permission: Permission) -> bool:
        return permission in self.permissions_of(principal)

    # -- scope ----------------------------------------------------------------------

    def can_access_project(self, principal: Principal, project_id: str) -> bool:
        if self.has(principal, Permission.PROJECTS_ALL):
            return True
        if project_id in self._reserved:
            return False  # a token claiming "legacy" is not enough
        return project_id in principal.projects

    def can_see(self, principal: Principal, run: OwnedRun) -> bool:
        if not self.can_access_project(principal, run.project_id):
            return False
        return self.has(principal, Permission.RUNS_READ) or run.owner_subject == principal.subject

    # -- decisions ------------------------------------------------------------------

    def decide(self, principal: Principal, permission: Permission, run: OwnedRun) -> Decision:
        """Hidden when the run must stay invisible; forbidden when visible but not allowed."""
        if not self.can_see(principal, run):
            return Decision(outcome="hidden", reason="fuera del ámbito del principal")
        if permission is Permission.RUNS_READ:
            return ALLOWED
        if not self.has(principal, permission):
            return Decision(outcome="forbidden", reason=f"falta el permiso {permission}")
        if permission is Permission.RUNS_APPROVE and self.separation_of_duties:
            if run.owner_subject == principal.subject:
                return Decision(outcome="forbidden", reason="self_approval")
        return ALLOWED

    def decide_publish(
        self, principal: Principal, run: OwnedRun, *, scope_approver: str | None
    ) -> Decision:
        """May ``principal`` approve the publication of ``run``? (ADR 0011 §2)

        Requires ``runs:approve`` and ``repos:publish`` inside the project, never
        the run's creator, and, when ``publish_distinct_approver`` is on, never
        the subject that approved the scope. Agents are never visible principals.
        """
        base = self.decide(principal, Permission.RUNS_APPROVE, run)
        if not base.allowed:
            return base
        if not self.has(principal, Permission.REPOS_PUBLISH):
            return Decision(outcome="forbidden", reason="falta el permiso repos:publish")
        if run.owner_subject == principal.subject:
            return Decision(outcome="forbidden", reason="self_approval")
        if (
            self.publish_distinct_approver
            and scope_approver is not None
            and scope_approver == principal.subject
        ):
            return Decision(outcome="forbidden", reason="publish_same_approver")
        return ALLOWED

    def listing_scope(self, principal: Principal) -> AccessScope:
        """Rows visible in a listing: same rule as ``can_see``, expressed as a filter."""
        if self.has(principal, Permission.PROJECTS_ALL):
            return AccessScope(projects=None)
        projects = frozenset(principal.projects) - self._reserved
        owner_only = None if self.has(principal, Permission.RUNS_READ) else principal.subject
        return AccessScope(projects=projects, owner_only=owner_only)

    def sweep_scope(self, principal: Principal) -> AccessScope:
        """Projects a principal may recover; raises when it may not sweep at all."""
        if not self.has(principal, Permission.RECOVERY_SWEEP):
            raise PermissionDeniedError("Falta el permiso recovery:sweep.")
        if self.has(principal, Permission.PROJECTS_ALL):
            return AccessScope(projects=None)
        return AccessScope(projects=frozenset(principal.projects) - self._reserved)

    def resolve_project_for_create(self, principal: Principal, requested: str | None) -> str:
        """Which project a new run belongs to, or raise (403 / 400) before any write."""
        if not self.has(principal, Permission.RUNS_CREATE):
            raise PermissionDeniedError("Falta el permiso runs:create.")
        if requested is not None:
            if requested in self._reserved or not self.can_access_project(principal, requested):
                raise PermissionDeniedError(f"El principal no pertenece al proyecto '{requested}'.")
            return requested
        own = sorted(principal.projects - self._reserved)
        if len(own) == 1:
            return own[0]
        if not own:
            raise PermissionDeniedError("El token no pertenece a ningún proyecto.")
        raise ProjectRequiredError(own)

    def raise_for(self, decision: Decision, *, run_id: str) -> None:
        """Turn a non-allowed decision into the matching domain error."""
        if decision.outcome == "forbidden" and decision.reason == "self_approval":
            raise SelfApprovalForbiddenError()
        if decision.outcome == "forbidden" and decision.reason == "publish_same_approver":
            raise PermissionDeniedError(
                "Quien aprueba la publicación debe ser distinto de quien aprobó el alcance."
            )
        if decision.outcome == "forbidden":
            raise PermissionDeniedError(decision.reason.capitalize() + ".")
        # "hidden" is raised by the caller as RunNotFoundError: the policy does
        # not know orchestration's exceptions.

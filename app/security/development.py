"""Ephemeral operator authority for exactly one disposable development run.

The scope is the only thing that lets Developer write and lets either agent
execute programs. It is built by trusted code, lives in the process that runs
the graph, expires on its own and can be revoked explicitly; nothing a model
produces can widen it. The service's default grants and risk gate are not
consulted here and are not changed anywhere.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.contracts.scope import is_canonical_relative_path
from app.contracts.tools import AgentKind, RiskClass

from .policy import AuthorizationPolicy
from .principal import Principal
from .tools import AgentGrant, ToolAuthorizationPolicy, ToolContextLike, ToolDecision, ToolLike

SANDBOX_PREFIX = "sandbox-"
MAX_WRITABLE_PATHS = 16


def _utcnow() -> datetime:
    return datetime.now(UTC)


canonical_relative_path = is_canonical_relative_path


@dataclass(frozen=True)
class DevelopmentScope:
    run_id: UUID
    project_id: str
    repository_id: str
    workspace_root: Path
    expires_at: datetime
    writable_paths: frozenset[str]
    # The clock that decides expiry: the runtime's clock, so a deadline taken
    # from a checkpoint and the check against it never disagree.
    clock: Callable[[], datetime] = field(default=_utcnow, repr=False, compare=False)
    _revoked: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.project_id.startswith(SANDBOX_PREFIX) or not self.repository_id.startswith(
            SANDBOX_PREFIX
        ):
            raise ValueError("el MVP solo permite proyectos y repositorios sandbox-*")
        if self.expires_at.tzinfo is None or not self.writable_paths:
            raise ValueError("la autorización requiere caducidad y archivos explícitos")
        if len(self.writable_paths) > MAX_WRITABLE_PATHS:
            raise ValueError(f"como máximo {MAX_WRITABLE_PATHS} archivos escribibles")
        bad = sorted(p for p in self.writable_paths if not canonical_relative_path(p))
        if bad:
            raise ValueError(f"rutas escribibles no canónicas o fuera del worktree: {bad}")
        if not self.workspace_root.is_absolute():
            raise ValueError("workspace_root debe ser absoluto")

    @property
    def revoked(self) -> bool:
        return self._revoked.is_set()

    def revoke(self) -> None:
        """Withdraw the authority now; every later decision is denied, whatever the clock says."""
        self._revoked.set()

    def permits(self, context: ToolContextLike) -> bool:
        return (
            not self.revoked
            and self.clock() < self.expires_at
            and getattr(context, "run_id", None) == self.run_id
            and context.project_id == self.project_id
            and getattr(context, "workspace_root", None) == self.workspace_root
        )


_SHARED_TOOLS = frozenset(
    {
        "workspace.list_dir",
        "workspace.read_file",
        "workspace.find",
        "git.diff",
        "git.status",
        "git.log",
        "exec.run",
    }
)


class DevelopmentToolPolicy(ToolAuthorizationPolicy):
    """The platform policy, narrowed to one scope: Developer may also write."""

    def __init__(self, scope: DevelopmentScope) -> None:
        self.scope = scope
        super().__init__(
            AuthorizationPolicy(),
            {
                AgentKind.DEVELOPER: AgentGrant(
                    tools=_SHARED_TOOLS | {"workspace.write_file"},
                    risks=frozenset({RiskClass.READ, RiskClass.WRITE, RiskClass.EXEC}),
                ),
                AgentKind.QA: AgentGrant(
                    tools=_SHARED_TOOLS, risks=frozenset({RiskClass.READ, RiskClass.EXEC})
                ),
            },
            enabled_risks=frozenset({RiskClass.READ, RiskClass.WRITE, RiskClass.EXEC}),
        )

    def decide(
        self, principal: Principal, context: ToolContextLike, tool: ToolLike
    ) -> ToolDecision:
        if not self.scope.permits(context):
            return ToolDecision(
                outcome="denied",
                code="development_scope_denied",
                reason="fuera del run, proyecto, worktree o vigencia autorizados, o revocado",
            )
        return super().decide(principal, context, tool)

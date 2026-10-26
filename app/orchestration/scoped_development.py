"""Governed development workflow (Fase 7, Paso 1; ADR 0009).

    START -> scoper -> approve_scope -> developer -> apply_changes -> qa -> END
                                             ^                         |
                                             +-- revise_development <--+

- ``scoper`` creates the run's worktree, reads the repository through a
  read-only gateway and asks the model for a scope proposal; the orchestrator
  filters it against the repository policy (deny by default) and records the
  surviving files with their digest. Nothing is written.
- ``approve_scope`` pauses the graph (``interrupt``) until a human with
  ``runs:approve`` and ``repos:write`` decides. The resume payload must name
  the stage and the exact digest shown; anything else ends the run.
- Only then is the ephemeral ``DevelopmentScope`` built, from the approved
  list and the policy's limits, never from a later model answer. The
  Developer/QA loop is the one of Fase 6, rebuilt per node from the
  checkpoint so a decision taken in another process resumes the same run.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents.scoper import ScoperAgent
from app.contracts.scope import ScopeProposal
from app.contracts.tools import AgentKind, RiskClass, ToolCall, ToolOutcome
from app.git import GitOperationError, GitRepositoryPort
from app.git.mirrors import MirrorError, MirrorSyncRepository
from app.infrastructure.git.mirror import MirrorService, audited
from app.infrastructure.llm.budget import BudgetedLLM
from app.infrastructure.llm.errors import LLMError
from app.infrastructure.llm.port import LLMPort
from app.projects import (
    DATA_BOUNDARY_NOTICE,
    PolicyError,
    ProjectPolicyRegistry,
    RepositoryPolicy,
    check_against_policy,
    filter_scope,
    hidden_from_agents,
    injection_signals,
    matches_any,
)
from app.sandbox import SandboxPort
from app.security import (
    AgentGrant,
    AuthorizationPolicy,
    AutomationIdentity,
    ToolAuthorizationPolicy,
)
from app.security.development import DevelopmentScope, DevelopmentToolPolicy
from app.tools import (
    ExecRunTool,
    ExecutionPolicy,
    GitTools,
    ToolContext,
    ToolGateway,
    ToolLimits,
    default_registry,
)
from app.tools.ports import ToolInvocationRepository
from app.tools.write_file import WriteFileTool
from app.workspaces import WorkspaceError, WorktreeManager

from .development import (
    DevelopmentDeps,
    DevelopmentFailure,
    DevelopmentSession,
    DevelopmentState,
    revise,
)

SCOPE_STAGE = "scope"
CONTEXT_FILES: tuple[str, ...] = ("README.md", "pyproject.toml", "package.json")
MAX_LISTING = 400
_SCOPER_GRANTS = {
    AgentKind.SCOPER: AgentGrant(
        tools=frozenset({"workspace.*", "git.*"}), risks=frozenset({RiskClass.READ})
    )
}


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class DevelopmentRuntime:
    """Trusted, process-wide pieces; per-run authority is derived from the checkpoint."""

    policies: ProjectPolicyRegistry
    workspaces: WorktreeManager
    git: GitRepositoryPort
    sandbox: SandboxPort
    audit: ToolInvocationRepository
    llm: LLMPort
    limits: ToolLimits = field(default_factory=ToolLimits)
    execution_policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    clock: Callable[[], datetime] = _now
    # Fase 7, Paso 2 (ADR 0010): synchronises local mirrors of GitHub repositories
    # before a run starts. Only the platform holds it; agents never see it.
    mirrors: MirrorService | None = None
    mirror_syncs: MirrorSyncRepository | None = None

    def policy_for(self, state: DevelopmentState) -> RepositoryPolicy:
        return self.policies.require(state["repository_id"], state["project_id"])

    async def refresh_mirror(self, policy: RepositoryPolicy) -> dict[str, Any] | None:
        """Bring the mirror up to date and describe the base the run starts from.

        ``None`` for purely local repositories. Any identity, remote or fetch
        problem raises ``MirrorError`` (its code names the cause) and the run
        fails before any worktree exists.
        """
        if policy.remote is None:
            return None
        if self.mirrors is None:
            raise MirrorError(
                "mirror_unconfigured",
                f"el repositorio '{policy.repository_id}' requiere la GitHub App configurada",
            )
        mirrors = self.mirrors
        outcome = await audited(
            self.mirror_syncs,
            lambda: mirrors.refresh(policy.repository_id, policy.project_id),
        )
        return {
            "repository": policy.remote.full_name,
            "github_repository_id": policy.remote.github_repository_id,
            "branch": policy.remote.default_branch,
            "sha": outcome.record.base_sha,
            "changed": outcome.changed,
            "synced_at": outcome.sync.finished_at.isoformat(),
        }

    def budget_for(self, policy: RepositoryPolicy, state: DevelopmentState) -> BudgetedLLM:
        usage = state.get("usage") or {}
        return BudgetedLLM(
            self.llm,
            max_calls=policy.max_model_calls,
            max_reserved_tokens=int(policy.max_reserved_usd * 1_000_000 / 20.0),
            max_usd=policy.max_reserved_usd,
            calls=int(usage.get("calls", 0)),
            reserved_tokens=int(usage.get("reserved_tokens", 0)),
        )

    def read_gateway(self) -> ToolGateway:
        """Workspace and Git reads only: no write tool registered, no exec tool registered."""
        registry = default_registry(
            self.limits, GitTools(self.git, self.workspaces, self.limits), None
        )
        policy = ToolAuthorizationPolicy(
            AuthorizationPolicy(), _SCOPER_GRANTS, enabled_risks=frozenset({RiskClass.READ})
        )
        return ToolGateway(registry, policy, self.audit, limits=self.limits)

    def write_gateway(self, scope: DevelopmentScope) -> ToolGateway:
        registry = default_registry(
            self.limits,
            GitTools(self.git, self.workspaces, self.limits),
            ExecRunTool(self.sandbox, self.workspaces, git=self.git, policy=self.execution_policy),
        )
        registry.register(WriteFileTool(self.workspaces, scope))
        return ToolGateway(registry, DevelopmentToolPolicy(scope), self.audit, limits=self.limits)

    async def release(self, run_id: UUID, project_id: str, status: Any) -> None:
        """Mark the worktree as released when the run ends (best effort)."""
        try:
            if self.workspaces.get(run_id, project_id) is not None:
                await asyncio.to_thread(self.workspaces.release, run_id, project_id, status=status)
        except (WorkspaceError, GitOperationError, OSError):
            pass


def _run_id(config: RunnableConfig) -> UUID:
    return UUID(str(config["configurable"]["thread_id"]))


def _failure(exc: BaseException, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(exc, TimeoutError):
        code, message = "development_timeout", "se agotó el tiempo autorizado"
    else:
        code = str(getattr(exc, "code", "development_failed"))
        message = str(getattr(exc, "message", getattr(exc, "detail", exc)))
    update: dict[str, Any] = {"error": {"code": code, "message": message}}
    if usage is not None:
        update["usage"] = usage
    return update


_HANDLED = (
    DevelopmentFailure,
    LLMError,
    PolicyError,
    GitOperationError,
    MirrorError,
    WorkspaceError,
    TimeoutError,
)


def build_scoped_development_graph(
    runtime: DevelopmentRuntime, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> Any:
    identity = AutomationIdentity()

    async def read(
        gateway: ToolGateway, context: ToolContext, name: str, args: dict[str, Any], step: str
    ) -> dict[str, Any] | None:
        result = await gateway.invoke(context, ToolCall(name=name, arguments=args), step=step)
        if result.outcome is not ToolOutcome.OK:
            return None
        return result.output or {}

    async def scoper_node(state: DevelopmentState, config: RunnableConfig) -> dict[str, Any]:
        budget: BudgetedLLM | None = None
        try:
            run_id = _run_id(config)
            project_id, repository_id = state["project_id"], state["repository_id"]
            policy = runtime.policies.require(repository_id, project_id)
            budget = runtime.budget_for(policy, state)
            # Remote repositories: verify identity and fetch *before* the worktree
            # is cut, so the run starts from a known, recorded base commit.
            base = await runtime.refresh_mirror(policy)
            record = await asyncio.to_thread(
                runtime.workspaces.ensure, run_id, project_id, repository_id
            )
            gateway = runtime.read_gateway()
            context = ToolContext(
                run_id=run_id,
                project_id=project_id,
                agent=AgentKind.SCOPER,
                principal=identity.principal_for(AgentKind.SCOPER, project_id),
                workspace_root=record.path,
            )
            listing = await read(
                gateway, context, "workspace.find", {"pattern": "**/*"}, "scoper:0:find"
            )
            if listing is None:
                raise DevelopmentFailure(
                    "scope_context_unavailable", "no se pudo listar el worktree"
                )
            excluded = policy.all_excluded_context_globs
            visible, hidden = hidden_from_agents(list(listing.get("matches") or []), excluded)
            key_files: dict[str, str] = {}
            for index, name in enumerate(CONTEXT_FILES):
                if not (record.path / name).is_file() or matches_any(name, excluded):
                    continue
                output = await read(
                    gateway,
                    context,
                    "workspace.read_file",
                    {"path": name},
                    f"scoper:0:read:{index}",
                )
                if output is None or output.get("redactions"):
                    continue
                key_files[name] = str(output.get("content", ""))[:8000]
            signals = injection_signals(key_files)
            prompt = {
                "task": state["task"],
                "repository_id": repository_id,
                "data_boundary": DATA_BOUNDARY_NOTICE,
                "paths": visible[:MAX_LISTING],
                "listing_truncated": bool(listing.get("truncated")) or len(visible) > MAX_LISTING,
                "key_files": key_files,
                "writable_globs": list(policy.writable_globs),
                "denied_globs": list(policy.all_denied_globs),
                "max_files": policy.max_files_per_run,
                "test_paths": list(policy.test_paths),
            }
            proposal: ScopeProposal = await asyncio.wait_for(
                asyncio.to_thread(ScoperAgent(budget).execute, prompt),
                timeout=policy.model_timeout_seconds,
            )
            decision = filter_scope(proposal, policy, exists=lambda p: (record.path / p).is_file())
            if decision.scope is None:
                raise DevelopmentFailure(
                    "scope_empty",
                    "ningún archivo propuesto es admisible según la política del repositorio: "
                    + "; ".join(f"{r.path} ({r.reason})" for r in decision.rejected[:6]),
                )
            proposed_at = runtime.clock()
            deadline = proposed_at + timedelta(seconds=policy.scope_approval_ttl_seconds)
            return {
                "scope_proposal": {
                    "summary": proposal.summary,
                    "risks": list(proposal.risks),
                    "files": [f.model_dump(mode="json") for f in decision.scope.files],
                    "tests": list(decision.scope.tests),
                    "rejected": [r.model_dump(mode="json") for r in decision.rejected],
                    "digest": decision.scope.digest,
                    "base": base,
                    "hidden_paths": hidden,
                    "signals": [sig.model_dump(mode="json") for sig in signals],
                    "proposed_at": proposed_at.isoformat(),
                    "deadline": deadline.isoformat(),
                    "limits": {
                        "max_files_per_run": policy.max_files_per_run,
                        "max_corrections": policy.max_corrections,
                        "wall_seconds": policy.wall_seconds,
                        "max_model_calls": policy.max_model_calls,
                        "max_reserved_usd": policy.max_reserved_usd,
                    },
                },
                "workspace_root": str(record.path),
                "llm_calls": 1,
                "usage": budget.usage(),
                "revision": 0,
            }
        except _HANDLED as exc:
            return _failure(exc, budget.usage() if budget is not None else None)

    def approve_scope_node(state: DevelopmentState) -> dict[str, Any]:
        proposal = state["scope_proposal"]
        response = interrupt(
            {
                "kind": "approve_scope",
                "stage": SCOPE_STAGE,
                "scope_digest": proposal["digest"],
                "deadline": proposal["deadline"],
                "repository_id": state["repository_id"],
                "summary": proposal["summary"],
                "files": proposal["files"],
                "tests": proposal["tests"],
                "rejected": proposal["rejected"],
                "risks": proposal.get("risks", []),
                "limits": proposal["limits"],
                "base": proposal.get("base"),
                "hidden_paths": proposal.get("hidden_paths", 0),
                "signals": proposal.get("signals", []),
            }
        )
        if not isinstance(response, dict) or type(response.get("approved")) is not bool:
            return _failure(DevelopmentFailure("decision_invalid", "decisión de alcance inválida"))
        if (
            response.get("stage") != SCOPE_STAGE
            or response.get("scope_digest") != proposal["digest"]
        ):
            return _failure(
                DevelopmentFailure(
                    "decision_mismatch", "la decisión no corresponde al alcance propuesto"
                )
            )
        if not response["approved"]:
            return {"approved": False}
        try:
            policy = runtime.policy_for(state)
        except PolicyError as exc:
            return _failure(exc)
        deadline = runtime.clock() + timedelta(seconds=policy.wall_seconds)
        return {
            "approved_scope": {
                "repository_id": state["repository_id"],
                "files": proposal["files"],
                "tests": proposal["tests"],
                "digest": proposal["digest"],
                "base_sha": (proposal.get("base") or {}).get("sha"),
                "approved_at": runtime.clock().isoformat(),
            },
            "development_deadline": deadline.isoformat(),
        }

    def session_for(state: DevelopmentState, config: RunnableConfig) -> DevelopmentSession:
        run_id = _run_id(config)
        project_id, repository_id = state["project_id"], state["repository_id"]
        policy = runtime.policies.require(repository_id, project_id)
        approved = state.get("approved_scope")
        root_text = state.get("workspace_root")
        deadline_text = state.get("development_deadline")
        if not approved or not root_text or not deadline_text:
            raise DevelopmentFailure("scope_not_approved", "no hay un alcance aprobado")
        root = Path(root_text)
        record = runtime.workspaces.get(run_id, project_id)
        if record is None or record.path != root or record.repository_id != repository_id:
            raise DevelopmentFailure(
                "workspace_unavailable", "el worktree de la ejecución no existe"
            )
        # A policy tightened after the approval wins over the approval.
        outside = check_against_policy([f["path"] for f in approved["files"]], policy)
        if outside:
            raise DevelopmentFailure(
                "scope_outside_policy",
                "el alcance aprobado ya no está permitido por la política del repositorio: "
                + "; ".join(f"{r.path} ({r.reason})" for r in outside[:6]),
            )
        deadline = datetime.fromisoformat(deadline_text)
        scope = DevelopmentScope(
            run_id=run_id,
            project_id=project_id,
            repository_id=repository_id,
            workspace_root=root,
            expires_at=deadline,
            writable_paths=frozenset(f["path"] for f in approved["files"]),
            clock=runtime.clock,
        )
        excluded = policy.all_excluded_context_globs
        read_paths = [
            name
            for name in CONTEXT_FILES
            if (root / name).is_file() and not matches_any(name, excluded)
        ]
        read_paths += [f["path"] for f in approved["files"] if f["action"] == "edit"]
        read_paths += [t for t in approved.get("tests", []) if (root / t).is_file()]
        deps = DevelopmentDeps(
            scope=scope,
            gateway=runtime.write_gateway(scope),
            llm=runtime.budget_for(policy, state),
            read_paths=tuple(dict.fromkeys(read_paths)),
            lint_paths=tuple(policy.lint_paths),
            test_paths=tuple(policy.test_paths),
            max_corrections=policy.max_corrections,
            wall_seconds=policy.wall_seconds,
            model_timeout_seconds=policy.model_timeout_seconds,
            ruff_diff=policy.ruff_diff,
            max_file_bytes=policy.max_file_bytes,
            max_change_bytes=policy.max_change_bytes,
        )
        return DevelopmentSession(
            deps, deadline=deadline.timestamp(), now=lambda: runtime.clock().timestamp()
        )

    async def developer_node(state: DevelopmentState, config: RunnableConfig) -> dict[str, Any]:
        try:
            return await session_for(state, config).developer_node(state)
        except (DevelopmentFailure, PolicyError) as exc:
            return _failure(exc)

    async def apply_node(state: DevelopmentState, config: RunnableConfig) -> dict[str, Any]:
        try:
            return await session_for(state, config).apply_node(state)
        except (DevelopmentFailure, PolicyError) as exc:
            return _failure(exc)

    async def qa_node(state: DevelopmentState, config: RunnableConfig) -> dict[str, Any]:
        try:
            return await session_for(state, config).qa_node(state)
        except (DevelopmentFailure, PolicyError) as exc:
            return _failure(exc)

    def after_scoper(state: DevelopmentState) -> str:
        return END if state.get("error") else "approve_scope"

    def after_approval(state: DevelopmentState) -> str:
        if state.get("error") or state.get("approved") is False or not state.get("approved_scope"):
            return END
        return "developer"

    builder = StateGraph(DevelopmentState)
    builder.add_node("scoper", scoper_node)
    builder.add_node("approve_scope", approve_scope_node)
    builder.add_node("developer", developer_node)
    builder.add_node("apply_changes", apply_node)
    builder.add_node("qa", qa_node)
    builder.add_node("revise_development", revise)
    builder.add_edge(START, "scoper")
    builder.add_conditional_edges(
        "scoper", after_scoper, {"approve_scope": "approve_scope", END: END}
    )
    builder.add_conditional_edges(
        "approve_scope", after_approval, {"developer": "developer", END: END}
    )
    builder.add_conditional_edges(
        "developer",
        lambda s: END if s.get("error") else "apply_changes",
        {"apply_changes": "apply_changes", END: END},
    )
    builder.add_conditional_edges(
        "apply_changes", lambda s: END if s.get("error") else "qa", {"qa": "qa", END: END}
    )
    builder.add_conditional_edges(
        "qa",
        lambda s: END if s.get("error") or s.get("completed") else "revise_development",
        {"revise_development": "revise_development", END: END},
    )
    builder.add_edge("revise_development", "developer")
    return builder.compile(checkpointer=checkpointer or InMemorySaver())

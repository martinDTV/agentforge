"""Publication nodes of the governed development graph (Fase 7, Paso 3; ADR 0011).

    ... -> qa (approved) -> prepare_publication -> approve_publish -> publish -> END

- ``prepare_publication`` (platform): refreshes the mirror, checks that the
  base branch still points at the run's ``base_sha``, turns the worktree into
  exactly one commit, scans the diff (fail closed) and freezes a
  ``PublicationPlan`` whose digest is what a human will approve.
- ``approve_publish``: pauses the graph until a human with ``repos:publish``
  decides on that exact digest (the stage ``publish`` of ``run_decisions``).
- ``publish`` (platform, identity ``agentforge:publisher``): recomputes the
  plan from the mirror, refuses on any difference, then pushes
  ``<head_sha>:refs/heads/agentforge/<run_id>`` and opens a draft pull request,
  persisting each step in ``runs.publication`` with compare-and-set so a
  crash, a lost response or a second publisher can never duplicate anything.

Transient failures end the node with ``publication_interrupted`` (the run
becomes ``interrupted`` and is re-driven by recovery or ``resume``); content,
policy and identity failures end the run as ``failed``. Agents are not
involved at any point: no tool, no token, no model call.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from app.contracts.lifecycle import DecisionStage, RunEventKind
from app.git import GitOperationError, RepositoryConfig
from app.git.mirrors import MirrorError
from app.git.refs import run_branch
from app.git.remote import RemoteSpec
from app.infrastructure.git.askpass import askpass_credential
from app.infrastructure.git.publish import GitPublisher
from app.infrastructure.github import (
    PUSH_PERMISSIONS,
    GitHubError,
    GitHubPublisher,
    InstallationToken,
    InstallationTokenProvider,
)
from app.orchestration.ports import RunEvent, RunRepository
from app.projects import PolicyError, RepositoryPolicy
from app.publishing import (
    PUBLISHER_ACTOR,
    PublicationAuditRepository,
    PublicationError,
    PublicationFile,
    PublicationPlan,
    PublicationProgress,
    PublicationRecord,
    PublicationState,
    ScanReport,
    advance,
    commit_message,
    pull_request_body,
    pull_request_title,
    remote_branch_for,
    scan_changes,
)
from app.workspaces import WorkspaceError

from .development import DevelopmentFailure, DevelopmentState

logger = logging.getLogger(__name__)

PUBLISH_STAGE = "publish"
INTERRUPT_KIND = "approve_publish"
_S = PublicationState
# GitHub/Git codes that a later attempt may clear without any human action.
TRANSIENT_CODES: frozenset[str] = frozenset(
    {
        "github_unreachable",
        "github_failed",
        "token_invalid",
        "publication_timeout",
        "publication_failed",
        "mirror_locked",
        "mirror_sync_failed",
        "git_timeout",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


class PublicationInterrupted(Exception):
    """Internal: a transient failure; the node ends with ``publication_interrupted``."""

    def __init__(self, progress: PublicationProgress) -> None:
        super().__init__(progress.code or "publication_interrupted")
        self.progress = progress


class PublicationGate:
    """``PublicationAvailability`` answered from the configured ``PublicationRuntime`` (P1):
    no Publisher means nothing can be published; otherwise the allow-list decides."""

    def __init__(self, runtime: "PublicationRuntime | None") -> None:
        self._runtime = runtime

    def allows(self, repository_id: str) -> bool:
        return self._runtime is not None and self._runtime.allows(repository_id)


@dataclass
class PublicationRuntime:
    """Platform-only pieces the publication nodes need (never reachable by agents)."""

    git: GitPublisher
    github: GitHubPublisher
    tokens: InstallationTokenProvider
    audit: PublicationAuditRepository
    runs: RunRepository
    allowed_repositories: frozenset[str] = frozenset()  # empty = every repository with a remote
    askpass_dir: Path | None = None
    clock: Callable[[], datetime] = _now
    attempts: dict[UUID, int] = field(default_factory=dict)

    def allows(self, repository_id: str) -> bool:
        return not self.allowed_repositories or repository_id in self.allowed_repositories


class PublicationNodes:
    """Node functions bound to a ``DevelopmentRuntime`` (see ``scoped_development``)."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime  # DevelopmentRuntime; typed loosely to avoid a cycle

    # -- helpers ------------------------------------------------------------------------------

    @property
    def _publisher(self) -> PublicationRuntime | None:
        return cast(PublicationRuntime | None, self._runtime.publisher)

    def _config(self, repository_id: str, project_id: str) -> RepositoryConfig:
        return cast(
            RepositoryConfig,
            self._runtime.workspaces.repositories.require(repository_id, project_id),
        )

    async def _persist(
        self,
        run_id: UUID,
        progress: PublicationProgress,
        *,
        expected: PublicationState | None,
        plan: PublicationPlan | None,
        policy: RepositoryPolicy,
        token_expires_at: datetime | None = None,
    ) -> PublicationProgress:
        """CAS the progress into the row and append the audit row; conflicts are fatal."""
        publisher = self._publisher
        assert publisher is not None
        stamped = progress.model_copy(update={"updated_at": publisher.clock()})
        row = await publisher.runs.set_publication(
            run_id,
            stamped.as_json(),
            expected_state=expected.value if expected is not None else None,
            expect_absent=expected is None,
        )
        if row is None:
            raise PublicationError(
                "publication_state_conflict",
                f"otro publicador movió el estado de {run_id} (esperado {expected})",
            )
        await publisher.audit.record(
            PublicationRecord(
                run_id=run_id,
                repository_id=policy.repository_id,
                project_id=policy.project_id,
                state=stamped.state,
                code=stamped.code,
                retryable=stamped.retryable,
                remote_branch=stamped.remote_branch,
                base_branch=plan.base_branch if plan else policy.publish.base_branch,
                base_sha=stamped.base_sha,
                base_branch_head=plan.base_branch_head if plan else None,
                head_sha=stamped.head_sha,
                patch_digest=plan.patch_digest if plan else None,
                publish_digest=stamped.publish_digest,
                pr_number=stamped.pr_number,
                pr_url=stamped.pr_url,
                approved_by=stamped.approved_by,
                attempt=stamped.attempts,
                token_expires_at=token_expires_at,
                recorded_at=stamped.updated_at or publisher.clock(),
            )
        )
        return stamped

    async def _event(self, run_id: UUID, event: RunEvent) -> None:
        publisher = self._publisher
        assert publisher is not None
        await publisher.runs.append_events(run_id, [event])

    async def _decisions(self, run_id: UUID) -> dict[DecisionStage, Any]:
        publisher = self._publisher
        assert publisher is not None
        return {d.stage: d for d in await publisher.runs.list_decisions(run_id)}

    def _spec(self, policy: RepositoryPolicy) -> RemoteSpec:
        assert policy.remote is not None
        return policy.remote

    async def _build_plan(
        self,
        *,
        run_id: UUID,
        state: DevelopmentState,
        policy: RepositoryPolicy,
        base_branch_head: str,
        head_sha: str,
        title: str,
    ) -> tuple[PublicationPlan, ScanReport]:
        """Facts from the mirror -> scan -> plan. Used when preparing and again before pushing."""
        publisher = self._publisher
        assert publisher is not None
        spec = self._spec(policy)
        approved = state["approved_scope"]
        base_sha = str(approved["base_sha"])
        config = self._config(policy.repository_id, policy.project_id)
        mirror = config.path
        entries = await asyncio.to_thread(publisher.git.changed_entries, mirror, base_sha, head_sha)
        approved_actions = {f["path"]: f["action"] for f in approved["files"]}
        report = scan_changes(entries, approved=approved_actions, policy=policy)
        patch = await asyncio.to_thread(publisher.git.patch_digest, mirror, base_sha, head_sha)
        files = [
            PublicationFile(
                path=e.path,
                change=e.change,
                sha256=e.sha256,
                size=e.size or 0,
                mode=e.new_mode or e.old_mode or "100644",
            )
            for e in entries
        ] or [PublicationFile(path="(vacío)", change="modified")]
        plan = PublicationPlan(
            run_id=run_id,
            repository_id=policy.repository_id,
            project_id=policy.project_id,
            owner=spec.owner,
            name=spec.name,
            github_repository_id=spec.github_repository_id,
            installation_id=spec.installation_id,
            base_branch=spec.default_branch,
            base_sha=base_sha,
            base_branch_head=base_branch_head,
            remote_branch=remote_branch_for(policy.publish.branch_prefix, run_id),
            head_sha=head_sha,
            patch_digest=patch,
            files=files,
            scope_digest=str(approved["digest"]),
            scan_digest=report.digest(),
            title=title,
            summary=str((state.get("changes") or {}).get("summary", ""))[:4000],
            prepared_at=publisher.clock(),
        )
        return plan, report

    # -- nodes --------------------------------------------------------------------------------

    async def prepare_publication(
        self, state: DevelopmentState, config: RunnableConfig
    ) -> dict[str, Any]:
        run_id = UUID(str(config["configurable"]["thread_id"]))
        policy = self._runtime.policy_for(state)
        if policy.remote is None:
            return {}  # purely local repository: the run ends ``completed`` as before
        publisher = self._publisher
        if publisher is None or not publisher.allows(policy.repository_id):
            # P1: the work is done and verified but nothing can leave the host. Never end
            # ``completed`` (that silently discards the output): stop in a resumable
            # state the operator can continue once publication is enabled.
            row = await self._runtime.runs_get(run_id)
            existing = PublicationProgress.from_json(row.publication if row else None)
            if existing is not None and existing.state is not _S.UNAVAILABLE:
                raise PublicationError(
                    "publication_state_conflict",
                    f"la ejecución ya tiene una publicación en estado {existing.state}",
                )
            progress = PublicationProgress(
                state=_S.UNAVAILABLE,
                code="publish_unavailable",
                detail=(
                    f"la publicación no está habilitada para {policy.repository_id}: arranca la "
                    "API con GITHUB_WRITE_ENABLED=true y PUBLISH_ALLOWED_REPOSITORIES que lo "
                    "incluya y reanuda la ejecución"
                ),
                retryable=True,
            )
            await self._runtime.runs_set_publication(run_id, progress)
            return {"publication": progress.as_json(), "publication_interrupted": True}
        spec = self._spec(policy)
        try:
            base = await self._runtime.refresh_mirror(policy)
            assert base is not None
            approved = state.get("approved_scope") or {}
            base_sha = str(approved.get("base_sha") or "")
            if not base_sha:
                raise PublicationError("base_invalid", "la ejecución no registró su commit base")
            if base["sha"] != base_sha:
                raise PublicationError(
                    "base_moved",
                    f"la rama {spec.default_branch} avanzó de {base_sha[:12]} a "
                    f"{str(base['sha'])[:12]} desde la aprobación del alcance",
                )
            record = self._runtime.workspaces.get(run_id, policy.project_id)
            if record is None:
                raise DevelopmentFailure("workspace_unavailable", "el worktree no existe")
            repo_config = self._config(policy.repository_id, policy.project_id)
            decisions = await self._decisions(run_id)
            scope_decision = decisions.get(DecisionStage.SCOPE)
            title = pull_request_title(policy.publish.title_prefix, state["task"])
            message = commit_message(
                title=title,
                task=state["task"],
                run_id=run_id,
                base_sha=base_sha,
                scope_digest=str(approved["digest"]),
                files=[(f["action"], f["path"]) for f in approved["files"]],
                scope_approved_by=scope_decision.decided_by if scope_decision else None,
            )
            head_sha = await asyncio.to_thread(
                publisher.git.commit_run_branch,
                record.path,
                repo_config,
                base_sha=base_sha,
                message=message,
            )
            plan, report = await self._build_plan(
                run_id=run_id,
                state=state,
                policy=policy,
                base_branch_head=str(base["sha"]),
                head_sha=head_sha,
                title=title,
            )
            if report.blocking:
                raise PublicationError(
                    "publication_scan_failed",
                    "el escaneo previo bloqueó la publicación: " + ", ".join(report.codes),
                )
            progress = PublicationProgress(
                state=_S.PREPARED,
                publish_digest=plan.digest(),
                remote_branch=plan.remote_branch,
                base_sha=plan.base_sha,
                head_sha=plan.head_sha,
            )
            current_row = await publisher.runs.get_run(run_id)
            existing = PublicationProgress.from_json(
                current_row.publication if current_row is not None else None
            )
            if existing is None:
                progress = await self._persist(
                    run_id, progress, expected=None, plan=plan, policy=policy
                )
            elif existing.state is _S.UNAVAILABLE:
                # Resumed after publication was enabled: the plan is built now.
                progress = await self._persist(
                    run_id, progress, expected=_S.UNAVAILABLE, plan=plan, policy=policy
                )
            elif (
                existing.state is _S.PREPARED and existing.publish_digest == progress.publish_digest
            ):
                progress = existing  # re-executed after a crash: same plan, nothing to redo
            else:
                raise PublicationError(
                    "publication_state_conflict",
                    f"la ejecución ya tiene una publicación en estado {existing.state}",
                )
            await self._event(
                run_id,
                RunEvent(
                    kind=RunEventKind.PUBLICATION_PREPARED,
                    payload={
                        "files": len(plan.files),
                        "publish_digest": plan.digest(),
                        "base_sha": plan.base_sha,
                        "head_sha": plan.head_sha,
                        "remote_branch": plan.remote_branch,
                        "scan_findings": len(report.findings),
                        "executed_by": PUBLISHER_ACTOR,
                    },
                ),
            )
            deadline = publisher.clock() + timedelta(seconds=policy.publish.approval_ttl_seconds)
            return {
                "publication_plan": {
                    **plan.model_dump(mode="json"),
                    "digest": plan.digest(),
                    "deadline": deadline.isoformat(),
                    "scan": {"findings": [f.model_dump() for f in report.findings]},
                },
                "publication": progress.as_json(),
            }
        except (
            PublicationError,
            MirrorError,
            GitOperationError,
            PolicyError,
            WorkspaceError,
            DevelopmentFailure,
        ) as exc:
            code = str(getattr(exc, "code", "publication_failed"))
            failed = PublicationProgress(
                state=_S.FAILED, code=code, detail=str(getattr(exc, "detail", exc))[:500]
            )
            await self._runtime.runs_set_publication(run_id, failed)
            await self._event(
                run_id,
                RunEvent(
                    kind=RunEventKind.PUBLICATION_FAILED,
                    payload={"code": code, "stage": "prepare", "executed_by": PUBLISHER_ACTOR},
                ),
            )
            return {
                "error": {"code": code, "message": str(getattr(exc, "detail", exc))},
                "publication": failed.as_json(),
            }

    async def approve_publish(
        self, state: DevelopmentState, config: RunnableConfig
    ) -> dict[str, Any]:
        plan = state.get("publication_plan") or {}
        if not plan:
            return {}
        response = interrupt(
            {
                "kind": INTERRUPT_KIND,
                "stage": PUBLISH_STAGE,
                "publish_digest": plan["digest"],
                "deadline": plan["deadline"],
                "repository_id": state["repository_id"],
                "repository": f"{plan['owner']}/{plan['name']}",
                "remote_branch": plan["remote_branch"],
                "base_branch": plan["base_branch"],
                "base_sha": plan["base_sha"],
                "head_sha": plan["head_sha"],
                "patch_digest": plan["patch_digest"],
                "files": plan["files"],
                "title": plan["title"],
                "summary": plan.get("summary", ""),
                "scan": plan.get("scan", {}),
                "qa_checks": [
                    {"program": r.get("program"), "exit_code": r.get("exit_code")}
                    for r in (state.get("validation") or [])
                ],
            }
        )
        if not isinstance(response, dict) or type(response.get("approved")) is not bool:
            return {
                "error": {"code": "decision_invalid", "message": "decisión de publicación inválida"}
            }
        if (
            response.get("stage") != PUBLISH_STAGE
            or response.get("publish_digest") != plan["digest"]
        ):
            return {
                "error": {
                    "code": "decision_mismatch",
                    "message": "la decisión no corresponde al plan de publicación",
                }
            }
        decided_by = response.get("approved_by")
        if not response["approved"]:
            await self._record_rejection(
                UUID(str(config["configurable"]["thread_id"])),
                state,
                decided_by if isinstance(decided_by, str) else None,
            )
            return {"publish_approved": False, "publish_decided_by": decided_by}
        return {"publish_approved": True, "publish_decided_by": decided_by}

    async def _record_rejection(
        self, run_id: UUID, state: DevelopmentState, decided_by: str | None
    ) -> None:
        """The human declined: the plan is closed; nothing was or will be pushed."""
        publisher = self._publisher
        if publisher is None:
            return
        policy = self._runtime.policy_for(state)
        row = await publisher.runs.get_run(run_id)
        progress = PublicationProgress.from_json(row.publication if row else None)
        if progress is None or progress.state is not _S.PREPARED:
            return
        try:
            await self._persist(
                run_id,
                advance(progress, _S.REJECTED, approved_by=decided_by),
                expected=_S.PREPARED,
                plan=None,
                policy=policy,
            )
        except PublicationError:
            logger.warning("Rechazo de publicación de %s no persistido (estado movido)", run_id)

    async def publish(self, state: DevelopmentState, config: RunnableConfig) -> dict[str, Any]:
        run_id = UUID(str(config["configurable"]["thread_id"]))
        policy = self._runtime.policy_for(state)
        publisher = self._publisher
        plan_data = state.get("publication_plan") or {}
        if publisher is None or not plan_data:
            return {
                "error": {"code": "publish_unavailable", "message": "no hay publicador configurado"}
            }
        plan = PublicationPlan.model_validate(
            {k: v for k, v in plan_data.items() if k not in ("digest", "deadline", "scan")}
        )
        row = await publisher.runs.get_run(run_id)
        progress = PublicationProgress.from_json(row.publication if row else None)
        if progress is None:
            return {"error": {"code": "publication_state_conflict", "message": "sin estado"}}
        approver = state.get("publish_decided_by") or progress.approved_by
        attempt = progress.attempts + 1
        try:
            if progress.state is _S.FAILED and progress.retryable and progress.resume_from:
                progress = await self._persist(
                    run_id,
                    advance(progress, progress.resume_from, attempts=attempt),
                    expected=_S.FAILED,
                    plan=plan,
                    policy=policy,
                )
            elif progress.state is _S.PREPARED:
                progress = await self._persist(
                    run_id,
                    advance(progress, _S.APPROVED, approved_by=approver, attempts=attempt),
                    expected=_S.PREPARED,
                    plan=plan,
                    policy=policy,
                )
            elif progress.state in (_S.APPROVED, _S.PUSHED, _S.OPENED):
                progress = progress.model_copy(update={"attempts": attempt})
            else:
                raise PublicationError(
                    "publication_state_conflict",
                    f"la publicación está en estado {progress.state} y no puede continuar",
                )
            progress = await asyncio.wait_for(
                self._execute(run_id, state, policy, plan, progress),
                timeout=policy.publish.wall_seconds,
            )
            return {
                "publication": progress.as_json(),
                "published": True,
                "publication_interrupted": False,
            }
        except TimeoutError:
            failed = await self._fail(
                run_id,
                plan,
                policy,
                PublicationError(
                    "publication_timeout", "se agotó publish.wall_seconds", retryable=True
                ),
            )
            return {"publication": failed.as_json(), "publication_interrupted": True}
        except (PublicationError, GitHubError, MirrorError, GitOperationError) as exc:
            failed = await self._fail(run_id, plan, policy, exc)
            if failed.retryable:
                return {"publication": failed.as_json(), "publication_interrupted": True}
            return {
                "error": {
                    "code": failed.code or "publication_failed",
                    "message": failed.detail or "",
                },
                "publication": failed.as_json(),
                "publication_interrupted": False,
            }

    async def _fail(
        self,
        run_id: UUID,
        plan: PublicationPlan,
        policy: RepositoryPolicy,
        exc: Exception,
    ) -> PublicationProgress:
        """Record the failure from the *current* durable step, so a retry resumes there."""
        publisher = self._publisher
        assert publisher is not None
        row = await publisher.runs.get_run(run_id)
        progress = PublicationProgress.from_json(row.publication if row else None)
        if progress is None:
            progress = PublicationProgress(state=_S.APPROVED, publish_digest=plan.digest())
        code = str(getattr(exc, "code", "publication_failed"))
        retryable = bool(getattr(exc, "retryable", False)) or code in TRANSIENT_CODES
        if code in (
            "publish_digest_stale",
            "base_moved",
            "base_invalid",
            "remote_branch_conflict",
            "publication_scan_failed",
            "publication_state_conflict",
            "permission_not_allowed",
            "token_request_rejected",
            "installation_forbidden",
            "installation_not_found",
            "app_auth_failed",
            "pull_request_rejected",
            "remote_branch_invalid",
            "head_invalid",
        ):
            retryable = False
        detail = str(getattr(exc, "detail", exc))[:500]
        try:
            failed = advance(progress, _S.FAILED, code=code, detail=detail, retryable=retryable)
            failed = await self._persist(
                run_id, failed, expected=progress.state, plan=plan, policy=policy
            )
        except PublicationError:
            failed = progress.model_copy(
                update={"state": _S.FAILED, "code": code, "detail": detail, "retryable": retryable}
            )
        await self._event(
            run_id,
            RunEvent(
                kind=RunEventKind.PUBLICATION_FAILED,
                ordinal=progress.attempts,
                payload={
                    "code": code,
                    "retryable": retryable,
                    "stage": progress.state.value,
                    "executed_by": PUBLISHER_ACTOR,
                },
            ),
        )
        return failed

    async def _execute(
        self,
        run_id: UUID,
        state: DevelopmentState,
        policy: RepositoryPolicy,
        plan: PublicationPlan,
        progress: PublicationProgress,
    ) -> PublicationProgress:
        publisher = self._publisher
        assert publisher is not None
        spec = self._spec(policy)
        config = self._config(policy.repository_id, policy.project_id)
        mirror = config.path
        # 1. Recompute and compare: the mirror, the base and the content must be exactly
        #    what the human approved. Any difference stops before touching GitHub.
        base = await self._runtime.refresh_mirror(policy)
        assert base is not None
        if base["sha"] != plan.base_sha or base["sha"] != plan.base_branch_head:
            raise PublicationError("base_moved", f"la rama base ya no está en {plan.base_sha[:12]}")
        head = await asyncio.to_thread(publisher.git.head_of, mirror, run_branch(run_id))
        if head != plan.head_sha:
            raise PublicationError(
                "publish_digest_stale", "la rama de la ejecución ya no apunta al commit aprobado"
            )
        rebuilt, report = await self._build_plan(
            run_id=run_id,
            state=state,
            policy=policy,
            base_branch_head=str(base["sha"]),
            head_sha=head,
            title=plan.title,
        )
        if report.blocking:
            raise PublicationError(
                "publication_scan_failed",
                "el escaneo previo al push bloqueó: " + ", ".join(report.codes),
            )
        if rebuilt.digest() != plan.digest() or plan.digest() != progress.publish_digest:
            raise PublicationError(
                "publish_digest_stale", "el plan recalculado no coincide con el aprobado"
            )
        push_token: InstallationToken | None = None
        try:
            # 2. Push, with a token that can only write contents of this repository.
            if progress.state is _S.APPROVED:
                push_token = publisher.tokens.token_for(
                    spec.installation_id, spec.name, permissions=PUSH_PERMISSIONS
                )
                remote_head = await asyncio.to_thread(self._push, mirror, config, plan, push_token)
                progress = await self._persist(
                    run_id,
                    advance(
                        progress, _S.PUSHED, head_sha=remote_head, remote_branch=plan.remote_branch
                    ),
                    expected=_S.APPROVED,
                    plan=plan,
                    policy=policy,
                    token_expires_at=push_token.expires_at,
                )
                await self._event(
                    run_id,
                    RunEvent(
                        kind=RunEventKind.BRANCH_PUSHED,
                        payload={
                            "remote_branch": plan.remote_branch,
                            "head_sha": plan.head_sha,
                            "repository": plan.full_name,
                            "approved_by": progress.approved_by,
                            "executed_by": PUBLISHER_ACTOR,
                        },
                    ),
                )
            # 3. Draft pull request: look up first, create once, never merge.
            if progress.state is _S.PUSHED:
                decisions = await self._decisions(run_id)
                scope_decision = decisions.get(DecisionStage.SCOPE)
                publish_decision = decisions.get(DecisionStage.PUBLISH)
                body = pull_request_body(
                    plan,
                    task=state["task"],
                    developer_summary=str((state.get("changes") or {}).get("summary", "")),
                    qa_summary=str((state.get("qa_review") or {}).get("summary", "")),
                    qa_checks=list(state.get("validation") or []),
                    scope_approved_by=scope_decision.decided_by if scope_decision else None,
                    scope_approved_at=scope_decision.decided_at if scope_decision else None,
                    publish_approved_by=(
                        publish_decision.decided_by if publish_decision else progress.approved_by
                    ),
                    publish_approved_at=publish_decision.decided_at if publish_decision else None,
                )
                pr, _created = await asyncio.to_thread(
                    publisher.github.open_draft,
                    spec,
                    head_branch=plan.remote_branch,
                    title=plan.title,
                    body=body,
                )
                if pr.head_ref != plan.remote_branch or not pr.draft:
                    raise PublicationError(
                        "pull_request_rejected", "el pull request encontrado no es el esperado"
                    )
                progress = await self._persist(
                    run_id,
                    advance(progress, _S.OPENED, pr_number=pr.number, pr_url=pr.url),
                    expected=_S.PUSHED,
                    plan=plan,
                    policy=policy,
                )
                await self._event(
                    run_id,
                    RunEvent(
                        kind=RunEventKind.PULL_REQUEST_OPENED,
                        payload={
                            "pr_number": pr.number,
                            "pr_url": pr.url,
                            "draft": True,
                            "remote_branch": plan.remote_branch,
                            "approved_by": progress.approved_by,
                            "executed_by": PUBLISHER_ACTOR,
                        },
                    ),
                )
            # 4. Done: revoke what we used, record the terminal state.
            if progress.state is _S.OPENED:
                progress = await self._persist(
                    run_id,
                    advance(progress, _S.PUBLISHED),
                    expected=_S.OPENED,
                    plan=plan,
                    policy=policy,
                )
        finally:
            if push_token is not None:
                await asyncio.to_thread(publisher.tokens.revoke, push_token)
            await asyncio.to_thread(publisher.github.revoke, spec)
        return progress

    def _push(
        self,
        mirror: Path,
        config: RepositoryConfig,
        plan: PublicationPlan,
        token: InstallationToken,
    ) -> str:
        publisher = self._publisher
        assert publisher is not None
        with askpass_credential(token.reveal(), base_dir=publisher.askpass_dir) as credential:
            remote = publisher.git.remote_branch_head(
                mirror, plan.remote_branch, credential_env=credential.env
            )
            if remote == plan.head_sha:
                return plan.head_sha  # pushed by an earlier attempt whose state write was lost
            if remote is not None:
                raise PublicationError(
                    "remote_branch_conflict",
                    f"la rama {plan.remote_branch} ya existe con otro contenido; no se sobrescribe",
                )
            publisher.git.push_run_branch(
                mirror,
                config,
                head_sha=plan.head_sha,
                remote_branch=plan.remote_branch,
                branch_prefix=policy_prefix(plan.remote_branch),
                expected_remote_sha=None,
                credential_env=credential.env,
            )
        return plan.head_sha


def policy_prefix(remote_branch: str) -> str:
    """The reserved prefix a derived branch name was built with (``agentforge/`` + uuid)."""
    head, _, _ = remote_branch.rpartition("/")
    return head + "/"

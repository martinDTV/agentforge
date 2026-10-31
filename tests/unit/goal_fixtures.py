"""Fakes for the goal Supervisor's unit tests: a scripted stand-in for ``RunService`` that
writes real rows into ``InMemoryRunRepository`` (so the coordinator reads exactly what it
would read in production) and a scripted integration probe."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.contracts.execution import RunError
from app.contracts.goals import MergeEvidence, PlannedUpstream
from app.contracts.lifecycle import DecisionStage, RunLifecycleStatus, WorkflowKind
from app.contracts.planning import PlanConstraints
from app.contracts.routing import RouteMode
from app.goals.ports import IntegrationError, MergeProbe
from app.infrastructure.db import InMemoryRunRepository
from app.orchestration.exceptions import RunNotCancellableError
from app.orchestration.ports import RunProjection
from app.orchestration.runs import CreateResult, DecisionsView, _response
from app.orchestration.status import PendingDecision
from app.projects import ProjectPolicyRegistry, RepositoryPolicy
from app.security import Principal

S = RunLifecycleStatus
PROJECT = "sandbox-goals"
REPO = "sandbox-goals"
T0 = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
OWNER = Principal(
    subject="martin",
    kind="user",
    issuer="https://issuer.test",
    roles=frozenset({"project_operator"}),
    projects=frozenset({PROJECT}),
)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


def policy(*, remote: bool = True, **overrides: Any) -> RepositoryPolicy:
    from tests.unit.test_mirror_service import remote_spec

    return RepositoryPolicy(
        repository_id=REPO,
        project_id=PROJECT,
        writable_globs=["src/**", "tests/**"],
        remote=remote_spec() if remote else None,
        max_model_calls=4,
        max_reserved_usd=2.0,
        **overrides,
    )


def registry(*, remote: bool = True, **overrides: Any) -> ProjectPolicyRegistry:
    return ProjectPolicyRegistry([policy(remote=remote, **overrides)])


class FakeLauncher:
    """Creates run rows like ``RunService.create`` would (idempotency included) and lets a
    test move each child through the lifecycle statuses a real run passes through."""

    def __init__(self, repository: InMemoryRunRepository, clock: Callable[[], datetime]) -> None:
        self.repository = repository
        self.clock = clock
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[tuple[UUID, Principal]] = []
        self.decisions_calls: list[tuple[UUID, Principal]] = []
        self.fail_create_with: Exception | None = None
        self.scope_bases: dict[UUID, str] = {}  # run -> base sha shown at the scope stage
        self.scope_paths: dict[UUID, list[str]] = {}
        self.default_base = "b" * 40
        self.approval_ttl = timedelta(hours=24)

    # -- RunLauncher ---------------------------------------------------------------------

    async def create(
        self,
        task: str,
        principal: Principal,
        *,
        project_id: str | None = None,
        run_id: str | None = None,
        idempotency_key: str | None = None,
        workflow_kind: WorkflowKind | None = None,
        repository_id: str | None = None,
        route_mode: RouteMode | None = None,
        planned_upstream: PlannedUpstream | None = None,
        plan_constraints: PlanConstraints | None = None,
    ) -> CreateResult:
        self.created.append(
            {
                "task": task,
                "principal": principal,
                "project_id": project_id,
                "run_id": run_id,
                "idempotency_key": idempotency_key,
                "workflow_kind": workflow_kind,
                "repository_id": repository_id,
                "route_mode": route_mode,
                "planned_upstream": planned_upstream,
                "plan_constraints": plan_constraints,
            }
        )
        if self.fail_create_with is not None:
            raise self.fail_create_with
        assert run_id is not None and project_id is not None
        kind = workflow_kind or WorkflowKind.PLANNING
        outcome = await self.repository.create_run(
            UUID(run_id),
            task,
            owner="fake-launcher",
            ttl_seconds=60,
            owner_subject=principal.subject,
            project_id=project_id,
            idempotency_key=idempotency_key,
            workflow_kind=kind,
        )
        if outcome.created:
            assert outcome.lease is not None
            stage = DecisionStage.SCOPE if kind is WorkflowKind.DEVELOPMENT else DecisionStage.PLAN
            await self.repository.project(
                UUID(run_id),
                RunProjection(
                    status=S.WAITING_APPROVAL,
                    llm_calls=1,
                    pending_stage=stage,
                    approval_deadline=self.clock() + self.approval_ttl,
                ),
                lease=outcome.lease,
                release=True,
            )
        row = await self.repository.get_run(UUID(run_id))
        assert row is not None
        return CreateResult(response=_response(row), created=outcome.created)

    async def decisions(self, run_id: str, principal: Principal) -> DecisionsView:
        rid = UUID(run_id)
        self.decisions_calls.append((rid, principal))
        row = await self.repository.get_run(rid)
        assert row is not None
        pending = None
        if row.status is S.WAITING_APPROVAL and row.pending_stage is DecisionStage.SCOPE:
            pending = PendingDecision(
                stage=DecisionStage.SCOPE,
                scope_digest="d" * 64,
                deadline=row.approval_deadline,
                payload={
                    "base": {"sha": self.scope_bases.get(rid, self.default_base)},
                    "files": [
                        {"path": p, "action": "edit"}
                        for p in self.scope_paths.get(rid, ["src/app.py"])
                    ],
                },
            )
        return DecisionsView(run_id=run_id, status=row.status, pending=pending, decisions=[])

    async def cancel(self, run_id: str, principal: Principal) -> Any:
        rid = UUID(run_id)
        self.cancelled.append((rid, principal))
        cancelled = await self.repository.cancel_run(rid, actor=principal.subject)
        if cancelled is None:
            row = await self.repository.get_run(rid)
            raise RunNotCancellableError(run_id, row.status.value if row else "missing")
        return _response(cancelled)

    # -- moving a child along, as the real graph would -------------------------------------

    async def _move(self, run_id: UUID, *statuses: S, **fields: Any) -> None:
        for status in statuses:
            row = await self.repository.get_run(run_id)
            assert row is not None, run_id
            projection = RunProjection(
                status=status,
                llm_calls=fields.get("llm_calls", row.llm_calls),
                plan=fields.get("plan", row.plan),
                error=fields.get("error"),
                pending_stage=fields.get("pending_stage")
                if status in (S.WAITING_APPROVAL, S.RESUME_PENDING)
                else None,
                approval_deadline=fields.get("approval_deadline")
                if status in (S.WAITING_APPROVAL, S.RESUME_PENDING)
                else None,
            )
            updated = await self.repository.project(run_id, projection)
            assert updated is not None, (run_id, row.status, status)

    async def approve_scope(self, run_id: UUID) -> None:
        """Scope approved: the child develops and reaches the publish decision."""
        await self._move(run_id, S.RESUME_PENDING, pending_stage=DecisionStage.SCOPE)
        await self._move(
            run_id,
            S.WAITING_APPROVAL,
            pending_stage=DecisionStage.PUBLISH,
            approval_deadline=self.clock() + self.approval_ttl,
            llm_calls=3,
        )

    async def publish(self, run_id: UUID, *, pr_number: int, head_sha: str, base_sha: str) -> None:
        await self.repository.set_publication(
            run_id,
            {
                "state": "published",
                "remote_branch": f"agentforge/{run_id}",
                "base_sha": base_sha,
                "head_sha": head_sha,
                "pr_number": pr_number,
                "pr_url": f"https://github.test/pull/{pr_number}",
            },
        )
        await self._move(run_id, S.RESUME_PENDING, pending_stage=DecisionStage.PUBLISH)
        await self._move(run_id, S.PUBLISHED, llm_calls=4)

    async def complete_locally(self, run_id: UUID) -> None:
        await self._move(run_id, S.RESUME_PENDING, pending_stage=DecisionStage.SCOPE)
        await self._move(run_id, S.COMPLETED, llm_calls=4)

    async def complete_without_publication(self, run_id: UUID) -> None:
        """A remote child that ended ``completed`` with no publication at all (the pre-P1
        failure mode, kept to assert the Supervisor no longer calls it published)."""
        await self.complete_locally(run_id)

    async def resume(self, run_id: UUID) -> None:
        """What ``RunService.resume`` does once publication is enabled again: the run is
        driven into ``prepare_publication`` and waits for the human publish decision."""
        await self._move(run_id, S.RUNNING)
        await self._move(
            run_id,
            S.WAITING_APPROVAL,
            pending_stage=DecisionStage.PUBLISH,
            approval_deadline=self.clock() + self.approval_ttl,
        )

    async def publication_unavailable(self, run_id: UUID) -> None:
        """What P1 produces when publication is disabled: work done, run interrupted, the
        reason on the row, resumable once the operator enables publication."""
        await self._move(run_id, S.RESUME_PENDING, pending_stage=DecisionStage.SCOPE)
        await self._move(run_id, S.INTERRUPTED, llm_calls=4)
        await self.repository.set_publication(
            run_id,
            {
                "state": "unavailable",
                "code": "publish_unavailable",
                "detail": "la publicación no está habilitada",
                "retryable": True,
            },
        )

    async def fail(self, run_id: UUID, code: str) -> None:
        await self._move(run_id, S.FAILED, error=RunError(code=code, message=code))

    async def expire(self, run_id: UUID) -> None:
        """What the recovery sweep does once the decision window passed on the test clock."""
        expired = await self.repository.expire_waiting_runs(now=self.clock(), limit=50)
        assert any(r.id == run_id for r in expired), "la ejecución no estaba caducada"

    async def reject(self, run_id: UUID) -> None:
        await self._move(run_id, S.RESUME_PENDING, pending_stage=DecisionStage.SCOPE)
        await self._move(run_id, S.REJECTED)

    async def approve_plan(self, run_id: UUID, plan: dict[str, Any]) -> None:
        await self._move(run_id, S.RESUME_PENDING, pending_stage=DecisionStage.PLAN)
        await self._move(run_id, S.APPROVED, plan=plan, llm_calls=2)

    async def refuse_plan(self, run_id: UUID) -> None:
        await self._move(run_id, S.RESUME_PENDING, pending_stage=DecisionStage.PLAN)
        await self._move(run_id, S.REJECTED, llm_calls=2)

    async def refuse_plan_after_claim(self, run_id: UUID, lease: Any) -> None:
        """The decision was claimed (``resume_pending``, lease held): project the outcome."""
        row = await self.repository.get_run(run_id)
        assert row is not None and row.status is S.RESUME_PENDING
        updated = await self.repository.project(
            run_id,
            RunProjection(status=S.REJECTED, llm_calls=2, plan=row.plan),
            lease=lease,
            release=True,
        )
        assert updated is not None


class FakeProbe:
    """Scripted integration evidence: ``state`` per pull request number, and which commits
    the mirrored base contains."""

    def __init__(self) -> None:
        self.states: dict[int, str] = {}  # pr -> open | merged | closed
        self.merge_commits: dict[int, str] = {}
        self.verified: dict[int, bool] = {}
        self.ancestry: set[tuple[str, str]] = set()  # (ancestor, descendant)
        self.fail_with: IntegrationError | None = None
        self.calls: list[tuple[str, int]] = []
        self.paths: list[str] | None = None  # mirrored listing offered to the Planner (H1)
        self.listing_error: Exception | None = None

    def merged(self, pr: int, merge_commit_sha: str, *, verified: bool = True) -> None:
        self.states[pr] = "merged"
        self.merge_commits[pr] = merge_commit_sha
        self.verified[pr] = verified

    async def probe(self, repository_id: str, project_id: str, pr_number: int) -> MergeProbe:
        self.calls.append(("probe", pr_number))
        if self.fail_with is not None:
            raise self.fail_with
        state = self.states.get(pr_number, "open")
        if state != "merged":
            return MergeProbe(state="open" if state == "open" else "closed")
        return MergeProbe(
            state="merged",
            evidence=MergeEvidence(
                pr_number=pr_number,
                merged_at=T0,
                merge_commit_sha=self.merge_commits[pr_number],
                verified_in_mirror=self.verified.get(pr_number, True),
                base_after=self.merge_commits[pr_number],
            ),
        )

    async def contains(
        self, repository_id: str, project_id: str, *, ancestor: str, descendant: str
    ) -> bool:
        return (ancestor, descendant) in self.ancestry or ancestor == descendant

    async def list_paths(
        self, repository_id: str, project_id: str, *, limit: int
    ) -> list[str] | None:
        if self.listing_error is not None:
            raise self.listing_error
        return None if self.paths is None else list(self.paths)[:limit]


def plan_of(*tasks: tuple[str, list[str]]) -> dict[str, Any]:
    return {
        "summary": "plan",
        "tasks": [
            {
                "id": key,
                "title": f"Tarea {key}",
                "description": f"Implementar {key}",
                "dependencies": deps,
                "acceptance_criteria": [f"{key} funciona"],
            }
            for key, deps in tasks
        ],
        "risks": [],
    }

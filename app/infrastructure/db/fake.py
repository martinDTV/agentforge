"""In-memory ``RunRepository`` for the unit suite.

Same semantics as the PostgreSQL adapter (conditional transitions, monotonic
checkpoint ids, lease fencing, one decision per run, unique events) so the
service tests exercise the real rules. ``fail_with`` makes every call raise,
to simulate an outage; ``clock`` lets tests expire leases deterministically.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.contracts.execution import RunError
from app.contracts.lifecycle import (
    DecisionStage,
    RunEventKind,
    RunLifecycleStatus,
    WorkflowKind,
    can_transition,
)
from app.contracts.tools import InvocationStatus
from app.orchestration.exceptions import RunAlreadyExistsError
from app.orchestration.ports import (
    ApprovalClaim,
    ApprovalDecision,
    CreateOutcome,
    DecisionClaim,
    DecisionRecord,
    LeaseToken,
    PersistedEvent,
    PersistedRun,
    RunCursor,
    RunEvent,
    RunProjection,
)
from app.security import AccessScope
from app.tools.ports import InvocationCompletion, ToolInvocation


class InMemoryRunRepository:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.runs: dict[UUID, PersistedRun] = {}
        self.events: dict[UUID, list[PersistedEvent]] = {}
        self.approvals: dict[UUID, ApprovalDecision] = {}
        self.decisions: dict[UUID, dict[DecisionStage, DecisionRecord]] = {}
        self.fail_with: Exception | None = None
        self.calls: list[str] = []
        self.renewals: list[tuple[UUID, int]] = []
        self.clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._next_event_id = 1

    # -- helpers --------------------------------------------------------------

    def _enter(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_with is not None:
            raise self.fail_with

    def _fresh(self, run: PersistedRun) -> PersistedRun:
        active = run.lease_owner is not None and (
            run.lease_expires_at is not None and run.lease_expires_at > self.clock()
        )
        return run.model_copy(update={"lease_active": active})

    def _lease_free(self, run: PersistedRun) -> bool:
        return run.lease_owner is None or (
            run.lease_expires_at is not None and run.lease_expires_at < self.clock()
        )

    def _holds(self, run: PersistedRun, lease: LeaseToken) -> bool:
        return run.lease_owner == lease.owner and run.lease_generation == lease.generation

    def _store(self, run: PersistedRun) -> PersistedRun:
        self.runs[run.id] = run
        return self._fresh(run)

    # -- rows -----------------------------------------------------------------

    async def create_run(
        self,
        run_id: UUID,
        task: str,
        *,
        owner: str,
        ttl_seconds: float,
        owner_subject: str,
        project_id: str,
        idempotency_key: str | None = None,
        workflow_kind: WorkflowKind = WorkflowKind.PLANNING,
    ) -> CreateOutcome:
        self._enter("create_run")
        if idempotency_key is not None:
            for existing in self.runs.values():
                if (
                    existing.idempotency_key == idempotency_key
                    and existing.project_id == project_id
                    and existing.owner_subject == owner_subject
                ):
                    return CreateOutcome(run=self._fresh(existing), created=False)
        if run_id in self.runs:
            raise RunAlreadyExistsError(str(run_id))
        now = self.clock()
        run = PersistedRun(
            id=run_id,
            task=task,
            status=RunLifecycleStatus.RUNNING,
            owner_subject=owner_subject,
            project_id=project_id,
            workflow_kind=workflow_kind,
            idempotency_key=idempotency_key,
            attempt=1,
            lease_owner=owner,
            lease_expires_at=now + timedelta(seconds=ttl_seconds),
            lease_generation=1,
            created_at=now,
            updated_at=now,
        )
        self._append(
            run_id,
            [
                RunEvent(
                    kind=RunEventKind.RUN_CREATED,
                    payload={"initiated_by": owner_subject},
                    actor_subject=owner_subject,
                )
            ],
        )
        return CreateOutcome(
            run=self._store(run), created=True, lease=LeaseToken(owner=owner, generation=1)
        )

    async def get_run(self, run_id: UUID) -> PersistedRun | None:
        self._enter("get_run")
        run = self.runs.get(run_id)
        return self._fresh(run) if run is not None else None

    async def list_runs(
        self,
        *,
        scope: AccessScope,
        status: RunLifecycleStatus | None = None,
        owner_subject: str | None = None,
        before: RunCursor | None = None,
        limit: int,
    ) -> list[PersistedRun]:
        self._enter("list_runs")
        if scope.is_empty:
            return []
        rows = [
            run
            for run in self.runs.values()
            if (scope.projects is None or run.project_id in scope.projects)
            and (scope.owner_only is None or run.owner_subject == scope.owner_only)
            and (owner_subject is None or run.owner_subject == owner_subject)
            and (status is None or run.status is status)
            and (
                before is None or (run.created_at, run.id.int) < (before.created_at, before.id.int)
            )
        ]
        rows.sort(key=lambda r: (r.created_at, r.id.int), reverse=True)
        return [self._fresh(r) for r in rows[:limit]]

    async def project(
        self,
        run_id: UUID,
        projection: RunProjection,
        *,
        lease: LeaseToken | None = None,
        release: bool = False,
    ) -> PersistedRun | None:
        self._enter("project")
        current = self.runs.get(run_id)
        if current is None or not can_transition(current.status, projection.status):
            return None
        if (
            projection.checkpoint_id is not None
            and current.last_checkpoint_id is not None
            and current.last_checkpoint_id > projection.checkpoint_id
        ):
            return None
        if lease is not None and not self._holds(current, lease):
            return None
        changes: dict[str, object] = {
            "status": projection.status,
            "revision": projection.revision,
            "llm_calls": projection.llm_calls,
            "architect_approved": projection.architect_approved,
            "plan": projection.plan,
            "architecture": projection.architecture,
            "error": projection.error,
            "last_checkpoint_id": projection.checkpoint_id,
            "pending_stage": projection.pending_stage,
            "approval_deadline": projection.approval_deadline,
            "updated_at": self.clock(),
        }
        if release:
            changes.update(lease_owner=None, lease_expires_at=None)
        return self._store(current.model_copy(update=changes))

    # -- leases ---------------------------------------------------------------

    async def acquire_lease(
        self,
        run_id: UUID,
        *,
        owner: str,
        ttl_seconds: float,
        from_statuses: frozenset[RunLifecycleStatus],
        count_attempt: bool = False,
    ) -> tuple[PersistedRun, LeaseToken] | None:
        self._enter("acquire_lease")
        current = self.runs.get(run_id)
        if current is None or current.status not in from_statuses or not self._lease_free(current):
            return None
        generation = current.lease_generation + 1
        updated = self._store(
            current.model_copy(
                update={
                    "lease_owner": owner,
                    "lease_expires_at": self.clock() + timedelta(seconds=ttl_seconds),
                    "lease_generation": generation,
                    "attempt": current.attempt + (1 if count_attempt else 0),
                    "updated_at": self.clock(),
                }
            )
        )
        return updated, LeaseToken(owner=owner, generation=generation)

    async def bump_attempt(self, run_id: UUID, lease: LeaseToken) -> int | None:
        self._enter("bump_attempt")
        current = self.runs.get(run_id)
        if current is None or not self._holds(current, lease):
            return None
        updated = self._store(current.model_copy(update={"attempt": current.attempt + 1}))
        return updated.attempt

    async def renew_lease(self, run_id: UUID, lease: LeaseToken, ttl_seconds: float) -> bool:
        self._enter("renew_lease")
        current = self.runs.get(run_id)
        if current is None or not self._holds(current, lease):
            return False
        self.renewals.append((run_id, lease.generation))
        self._store(
            current.model_copy(
                update={"lease_expires_at": self.clock() + timedelta(seconds=ttl_seconds)}
            )
        )
        return True

    async def release_lease(self, run_id: UUID, lease: LeaseToken) -> bool:
        self._enter("release_lease")
        current = self.runs.get(run_id)
        if current is None or not self._holds(current, lease):
            return False
        self._store(current.model_copy(update={"lease_owner": None, "lease_expires_at": None}))
        return True

    async def release_leases_owned_by(self, owner: str) -> int:
        self._enter("release_leases_owned_by")
        released = 0
        for run_id, run in list(self.runs.items()):
            if run.lease_owner == owner:
                self._store(run.model_copy(update={"lease_owner": None, "lease_expires_at": None}))
                released += 1
        return released

    async def claim_expired_leases(
        self,
        *,
        owner: str,
        ttl_seconds: float,
        statuses: frozenset[RunLifecycleStatus],
        limit: int,
        projects: frozenset[str] | None = None,
    ) -> list[tuple[PersistedRun, LeaseToken]]:
        self._enter("claim_expired_leases")
        claimed: list[tuple[PersistedRun, LeaseToken]] = []
        for run in sorted(self.runs.values(), key=lambda r: r.updated_at):
            if len(claimed) >= limit:
                break
            if projects is not None and run.project_id not in projects:
                continue
            if run.status in statuses and self._lease_free(run):
                generation = run.lease_generation + 1
                updated = self._store(
                    run.model_copy(
                        update={
                            "lease_owner": owner,
                            "lease_expires_at": self.clock() + timedelta(seconds=ttl_seconds),
                            "lease_generation": generation,
                            "updated_at": self.clock(),
                        }
                    )
                )
                claimed.append((updated, LeaseToken(owner=owner, generation=generation)))
        return claimed

    # -- audit ----------------------------------------------------------------

    async def append_events(self, run_id: UUID, events: Sequence[RunEvent]) -> int:
        self._enter("append_events")
        return self._append(run_id, events)

    def _append(self, run_id: UUID, events: Sequence[RunEvent]) -> int:
        stored = self.events.setdefault(run_id, [])
        existing = {(e.kind, e.ordinal) for e in stored}
        inserted = 0
        for event in events:
            key = (event.kind, event.ordinal)
            if key in existing:
                continue
            existing.add(key)
            stored.append(
                PersistedEvent(
                    id=self._next_event_id, created_at=self.clock(), **event.model_dump()
                )
            )
            self._next_event_id += 1
            inserted += 1
        return inserted

    async def list_events(
        self, run_id: UUID, *, after_id: int | None = None, limit: int | None = None
    ) -> list[PersistedEvent]:
        self._enter("list_events")
        rows = [e for e in self.events.get(run_id, []) if after_id is None or e.id > after_id]
        return rows if limit is None else rows[:limit]

    # -- approvals ------------------------------------------------------------

    async def claim_approval(
        self,
        run_id: UUID,
        approved: bool,
        *,
        owner: str,
        ttl_seconds: float,
        decided_by: str | None,
    ) -> ApprovalClaim | None:
        claim = await self.claim_decision(
            run_id,
            DecisionStage.PLAN,
            approved,
            owner=owner,
            ttl_seconds=ttl_seconds,
            decided_by=decided_by,
        )
        if claim is None:
            return None
        return ApprovalClaim(
            claimed=claim.claimed,
            status=claim.status,
            existing=self.approvals.get(run_id),
            lease=claim.lease,
        )

    async def claim_decision(
        self,
        run_id: UUID,
        stage: DecisionStage,
        approved: bool,
        *,
        owner: str,
        ttl_seconds: float,
        decided_by: str | None,
        scope_digest: str | None = None,
    ) -> DecisionClaim | None:
        self._enter("claim_decision")
        current = self.runs.get(run_id)
        if current is None:
            return None
        if current.status is not RunLifecycleStatus.WAITING_APPROVAL or not self._lease_free(
            current
        ):
            return DecisionClaim(
                claimed=False,
                status=current.status,
                existing=self.decisions.get(run_id, {}).get(stage),
            )
        now = self.clock()
        decision = DecisionRecord(
            stage=stage,
            approved=approved,
            decided_at=now,
            decided_by=decided_by,
            scope_digest=scope_digest,
        )
        generation = current.lease_generation + 1
        self._store(
            current.model_copy(
                update={
                    "status": RunLifecycleStatus.RESUME_PENDING,
                    "lease_owner": owner,
                    "lease_expires_at": now + timedelta(seconds=ttl_seconds),
                    "lease_generation": generation,
                    "updated_at": now,
                }
            )
        )
        self.decisions.setdefault(run_id, {})[stage] = decision
        if stage is DecisionStage.PLAN:
            self.approvals[run_id] = ApprovalDecision(
                approved=approved, decided_at=now, decided_by=decided_by
            )
        payload: dict[str, Any] = {"approved": approved, "stage": stage.value}
        if decided_by is not None:
            payload["approved_by" if approved else "rejected_by"] = decided_by
        if scope_digest is not None:
            payload["scope_digest"] = scope_digest
        self._append(
            run_id,
            [
                RunEvent(
                    kind=RunEventKind.APPROVAL_RECORDED, payload=payload, actor_subject=decided_by
                )
            ],
        )
        return DecisionClaim(
            claimed=True,
            status=RunLifecycleStatus.RESUME_PENDING,
            existing=decision,
            lease=LeaseToken(owner=owner, generation=generation),
        )

    async def get_approval(self, run_id: UUID) -> ApprovalDecision | None:
        self._enter("get_approval")
        return self.approvals.get(run_id)

    async def get_pending_decision(self, run_id: UUID) -> DecisionRecord | None:
        self._enter("get_pending_decision")
        pending = [d for d in self.decisions.get(run_id, {}).values() if d.applied_at is None]
        if not pending:
            return None
        return max(pending, key=lambda d: d.decided_at)

    async def list_decisions(self, run_id: UUID) -> list[DecisionRecord]:
        self._enter("list_decisions")
        return sorted(self.decisions.get(run_id, {}).values(), key=lambda d: d.decided_at)

    async def mark_decision_applied(self, run_id: UUID, stage: DecisionStage) -> bool:
        self._enter("mark_decision_applied")
        decision = self.decisions.get(run_id, {}).get(stage)
        if decision is None or decision.applied_at is not None:
            return False
        now = self.clock()
        self.decisions[run_id][stage] = decision.model_copy(update={"applied_at": now})
        if stage is DecisionStage.PLAN and run_id in self.approvals:
            self.approvals[run_id] = self.approvals[run_id].model_copy(update={"applied_at": now})
        return True

    async def mark_approval_applied(self, run_id: UUID) -> bool:
        self._enter("mark_approval_applied")
        decision = self.approvals.get(run_id)
        if decision is None or decision.applied_at is not None:
            return False
        now = self.clock()
        self.approvals[run_id] = decision.model_copy(update={"applied_at": now})
        plan = self.decisions.get(run_id, {}).get(DecisionStage.PLAN)
        if plan is not None and plan.applied_at is None:
            self.decisions[run_id][DecisionStage.PLAN] = plan.model_copy(update={"applied_at": now})
        return True

    async def cancel_run(self, run_id: UUID, *, actor: str | None) -> PersistedRun | None:
        self._enter("cancel_run")
        current = self.runs.get(run_id)
        if (
            current is None
            or current.status
            not in (RunLifecycleStatus.WAITING_APPROVAL, RunLifecycleStatus.INTERRUPTED)
            or not self._lease_free(current)
        ):
            return None
        cancelled = self._store(
            current.model_copy(
                update={
                    "status": RunLifecycleStatus.CANCELLED,
                    "pending_stage": None,
                    "approval_deadline": None,
                    "updated_at": self.clock(),
                }
            )
        )
        self._append(
            run_id,
            [
                RunEvent(
                    kind=RunEventKind.RUN_CANCELLED,
                    payload={"cancelled_by": actor} if actor else {},
                    actor_subject=actor,
                )
            ],
        )
        return cancelled

    async def expire_waiting_runs(self, *, now: datetime, limit: int) -> list[PersistedRun]:
        self._enter("expire_waiting_runs")
        expired: list[PersistedRun] = []
        for run in sorted(self.runs.values(), key=lambda r: r.approval_deadline or now):
            if len(expired) >= limit:
                break
            if (
                run.status is RunLifecycleStatus.WAITING_APPROVAL
                and run.approval_deadline is not None
                and run.approval_deadline < now
                and self._lease_free(run)
            ):
                expired.append(
                    self._store(
                        run.model_copy(
                            update={
                                "status": RunLifecycleStatus.EXPIRED,
                                "pending_stage": None,
                                "approval_deadline": None,
                                "updated_at": self.clock(),
                            }
                        )
                    )
                )
                self._append(run.id, [RunEvent(kind=RunEventKind.RUN_EXPIRED)])
        return expired

    # -- test helpers ---------------------------------------------------------

    def expire_lease(self, run_id: UUID) -> None:
        """Simulate an owner that stopped heartbeating (SIGKILL, network partition)."""
        current = self.runs[run_id]
        self._store(
            current.model_copy(update={"lease_expires_at": self.clock() - timedelta(seconds=1)})
        )

    def set_error(self, run_id: UUID, error: RunError) -> None:
        self._store(self.runs[run_id].model_copy(update={"error": error}))


class InMemoryToolInvocationRepository:
    """Mirror of ``SqlAlchemyToolInvocationRepository`` for the unit suite."""

    def __init__(self) -> None:
        self.rows: dict[UUID, ToolInvocation] = {}
        self.fail_with: Exception | None = None

    def _enter(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    async def find_step(self, run_id: UUID, step: str) -> list[ToolInvocation]:
        self._enter()
        rows = [r for r in self.rows.values() if r.run_id == run_id and r.step == step]
        return sorted(rows, key=lambda r: r.attempt)

    async def start(self, invocation: ToolInvocation) -> bool:
        self._enter()
        key = (invocation.run_id, invocation.step, invocation.attempt)
        if invocation.id in self.rows or any(
            (r.run_id, r.step, r.attempt) == key for r in self.rows.values()
        ):
            return False
        self.rows[invocation.id] = invocation
        return True

    async def transition(
        self,
        invocation_id: UUID,
        *,
        expected: InvocationStatus,
        status: InvocationStatus,
        completion: InvocationCompletion | None = None,
    ) -> bool:
        self._enter()
        row = self.rows.get(invocation_id)
        if row is None or row.status is not expected:
            return False
        update: dict[str, object] = {"status": status}
        if completion is not None:
            update.update(
                error_code=completion.error_code,
                output_digest=completion.output_digest,
                output_bytes=completion.output_bytes,
                duration_ms=completion.duration_ms,
                finished_at=completion.finished_at,
            )
        self.rows[invocation_id] = row.model_copy(update=update)
        return True

    async def list_for_run(self, run_id: UUID, *, limit: int = 100) -> list[ToolInvocation]:
        self._enter()
        rows = [r for r in self.rows.values() if r.run_id == run_id]
        rows.sort(
            key=lambda r: (r.attempt, r.started_at or datetime.min.replace(tzinfo=UTC), r.step)
        )
        return rows[:limit]

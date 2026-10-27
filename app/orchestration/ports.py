"""Persistence port for runs: what the orchestration needs, nothing SQL-shaped.

Implementations: ``SqlAlchemyRunRepository`` (PostgreSQL) and
``InMemoryRunRepository`` (unit tests). Every method is one short unit of
work; none is ever called while the graph (and its LLM calls) is running
inside a transaction.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.execution import RunError
from app.contracts.lifecycle import DecisionStage, RunEventKind, RunLifecycleStatus, WorkflowKind
from app.security import AccessScope


class LeaseToken(BaseModel):
    """Proof of ownership: every write by a lease holder is conditioned on it.

    ``generation`` is monotonic per run, so two successive owners (even the
    same process reacquiring) can always be told apart.
    """

    model_config = ConfigDict(frozen=True)

    owner: str
    generation: int = Field(ge=0)


class PersistedRun(BaseModel):
    """Read model of a ``runs`` row."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    task: str
    status: RunLifecycleStatus
    owner_subject: str
    project_id: str
    workflow_kind: WorkflowKind = WorkflowKind.PLANNING
    revision: int = 0
    llm_calls: int = 0
    architect_approved: bool | None = None
    plan: dict[str, Any] | None = None
    architecture: dict[str, Any] | None = None
    error: RunError | None = None
    last_checkpoint_id: str | None = None
    # Fase 7: the human decision the run waits for, its deadline, what was published.
    pending_stage: DecisionStage | None = None
    approval_deadline: datetime | None = None
    publication: dict[str, Any] | None = None
    # Smart Router: route followed (fast/full) and the mode requested (auto/fast/full).
    route: str | None = None
    route_mode: str | None = None
    idempotency_key: str | None = None
    attempt: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    lease_generation: int = 0
    # Computed by the store with its own clock: a live lease means someone is driving the run.
    lease_active: bool = False
    created_at: datetime
    updated_at: datetime

    @property
    def lease(self) -> LeaseToken | None:
        if self.lease_owner is None:
            return None
        return LeaseToken(owner=self.lease_owner, generation=self.lease_generation)


class CreateOutcome(BaseModel):
    """Result of ``create_run``: a new row (with its lease) or an idempotent replay."""

    model_config = ConfigDict(frozen=True)

    run: PersistedRun
    created: bool
    lease: LeaseToken | None = None


class RunProjection(BaseModel):
    """What the graph's latest checkpoint says about a run, ready to be written.

    Written conditionally: the repository refuses it when the transition from
    the stored status is not allowed (terminal states, regressions).
    """

    model_config = ConfigDict(frozen=True)

    status: RunLifecycleStatus
    revision: int = Field(default=0, ge=0)
    llm_calls: int = Field(default=0, ge=0)
    architect_approved: bool | None = None
    plan: dict[str, Any] | None = None
    architecture: dict[str, Any] | None = None
    error: RunError | None = None
    checkpoint_id: str | None = None
    pending_stage: DecisionStage | None = None
    approval_deadline: datetime | None = None
    route: str | None = None
    route_mode: str | None = None


class RunEvent(BaseModel):
    """Audit event. ``(kind, ordinal)`` is unique per run: re-appending is a no-op.

    Payloads carry counters, codes and flags only: never prompts, plans,
    credentials or anything the checkpoint already stores.
    """

    model_config = ConfigDict(frozen=True)

    kind: RunEventKind
    ordinal: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    checkpoint_id: str | None = None
    # Human/service principal behind the event; None for agent and system events.
    actor_subject: str | None = None


class PersistedEvent(RunEvent):
    id: int
    created_at: datetime


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    approved: bool
    decided_at: datetime
    applied_at: datetime | None = None
    decided_by: str | None = None  # None only for pre-authentication approvals


class ApprovalClaim(BaseModel):
    """Outcome of the atomic ``waiting_approval -> resume_pending`` claim."""

    model_config = ConfigDict(frozen=True)

    claimed: bool
    status: RunLifecycleStatus
    existing: ApprovalDecision | None = None
    lease: LeaseToken | None = None


class DecisionRecord(BaseModel):
    """A human decision for one stage of a run (ADR 0009)."""

    model_config = ConfigDict(frozen=True)

    stage: DecisionStage
    approved: bool
    decided_at: datetime
    applied_at: datetime | None = None
    decided_by: str | None = None
    scope_digest: str | None = None


class DecisionClaim(BaseModel):
    """Outcome of the atomic claim of a stage decision."""

    model_config = ConfigDict(frozen=True)

    claimed: bool
    status: RunLifecycleStatus
    existing: DecisionRecord | None = None
    lease: LeaseToken | None = None


class RunCursor(BaseModel):
    """Keyset position in the runs listing (newest first)."""

    model_config = ConfigDict(frozen=True)

    created_at: datetime
    id: UUID


class RunRepository(Protocol):
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
        """Insert the row in ``running`` holding a fresh lease, plus ``run_created``.

        ``owner_subject``/``project_id`` are the principal and project the run
        belongs to (``owner`` is the lease holder, a process). With an
        ``idempotency_key`` the insert is atomic against the unique index over
        (project, owner_subject, key): a concurrent or earlier request with the
        same key *by the same principal in the same project* yields
        ``created=False`` and the existing row (the caller compares the task).
        """
        ...

    async def get_run(self, run_id: UUID) -> PersistedRun | None: ...

    async def list_runs(
        self,
        *,
        scope: AccessScope,
        status: RunLifecycleStatus | None = None,
        owner_subject: str | None = None,
        before: RunCursor | None = None,
        limit: int,
    ) -> list[PersistedRun]:
        """Newest first, keyset-paginated, always filtered by ``scope``."""
        ...

    async def project(
        self,
        run_id: UUID,
        projection: RunProjection,
        *,
        lease: LeaseToken | None = None,
        release: bool = False,
    ) -> PersistedRun | None:
        """Conditional write: allowed transition, monotonic checkpoint id and,
        when ``lease`` is given, current ownership. ``release`` clears the lease
        in the same statement. ``None`` when any condition fails."""
        ...

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
        """Take the lease only if it is free or expired and the status allows it."""
        ...

    async def bump_attempt(self, run_id: UUID, lease: LeaseToken) -> int | None:
        """Count one more drive of the graph (fenced); returns the new value."""
        ...

    async def renew_lease(self, run_id: UUID, lease: LeaseToken, ttl_seconds: float) -> bool: ...

    async def release_lease(self, run_id: UUID, lease: LeaseToken) -> bool: ...

    async def release_leases_owned_by(self, owner: str) -> int:
        """Shutdown: give back every lease this process still holds, nothing else."""
        ...

    async def claim_expired_leases(
        self,
        *,
        owner: str,
        ttl_seconds: float,
        statuses: frozenset[RunLifecycleStatus],
        limit: int,
        projects: frozenset[str] | None = None,
    ) -> list[tuple[PersistedRun, LeaseToken]]:
        """Recovery: atomically take over runs whose lease is free or expired
        (``FOR UPDATE SKIP LOCKED``), so concurrent sweepers never share one."""
        ...

    async def claim_interrupted_publications(
        self, *, owner: str, ttl_seconds: float, limit: int
    ) -> list[tuple[PersistedRun, LeaseToken]]:
        """Recovery (ADR 0011): ``interrupted`` runs whose publication is midway
        (prepared/approved/pushed/opened) or failed transiently, lease free or
        expired; claimed like ``claim_expired_leases``."""
        ...

    # -- audit ----------------------------------------------------------------

    async def append_events(self, run_id: UUID, events: Sequence[RunEvent]) -> int:
        """Insert events, ignoring duplicates; returns how many were new."""
        ...

    async def list_events(
        self, run_id: UUID, *, after_id: int | None = None, limit: int | None = None
    ) -> list[PersistedEvent]:
        """Oldest first; ``after_id`` and ``limit`` give keyset pages."""
        ...

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
        """One transaction: CAS to ``resume_pending``, record the decision and
        who made it, take the lease for applying it, emit the event. ``None``
        when the run does not exist; ``claimed=False`` carries the current
        status and any decision already recorded."""
        ...

    async def get_approval(self, run_id: UUID) -> ApprovalDecision | None: ...

    # -- decisions per stage (Fase 7) -------------------------------------------

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
        """Atomic ``waiting_approval -> resume_pending`` plus the decision row for
        ``stage`` (unique per run and stage). Stage ``plan`` also writes
        ``run_approvals`` for compatibility. ``None`` when the run does not exist;
        ``claimed=False`` carries the current status and the existing decision."""
        ...

    async def get_pending_decision(self, run_id: UUID) -> DecisionRecord | None:
        """The most recent decision not yet applied to the graph, if any."""
        ...

    async def list_decisions(self, run_id: UUID) -> list[DecisionRecord]: ...

    async def mark_decision_applied(self, run_id: UUID, stage: DecisionStage) -> bool: ...

    async def cancel_run(self, run_id: UUID, *, actor: str | None) -> PersistedRun | None:
        """``waiting_approval`` or ``interrupted`` with a free lease -> ``cancelled``,
        plus the ``run_cancelled`` event. ``None`` when the transition is not possible."""
        ...

    async def expire_waiting_runs(self, *, now: datetime, limit: int) -> list[PersistedRun]:
        """``waiting_approval`` rows whose deadline passed -> ``expired`` (plus event)."""
        ...

    async def mark_approval_applied(self, run_id: UUID) -> bool:
        """Set ``applied_at`` once; returns whether this call did it."""
        ...

    # -- publication (Fase 7, Paso 3) ----------------------------------------------

    async def set_publication(
        self,
        run_id: UUID,
        publication: dict[str, Any],
        *,
        expected_state: str | None = None,
        expect_absent: bool = False,
    ) -> PersistedRun | None:
        """Compare-and-set of ``runs.publication`` on its current ``state`` (or on
        its absence with ``expect_absent``); ``None`` when the state moved first.
        Independent of the run status and the lease: progress of a remote
        operation must survive a lost lease to stay idempotent."""
        ...

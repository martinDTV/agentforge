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
from app.contracts.lifecycle import RunEventKind, RunLifecycleStatus


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
    revision: int = 0
    llm_calls: int = 0
    architect_approved: bool | None = None
    plan: dict[str, Any] | None = None
    architecture: dict[str, Any] | None = None
    error: RunError | None = None
    last_checkpoint_id: str | None = None
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


class PersistedEvent(RunEvent):
    id: int
    created_at: datetime


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    approved: bool
    decided_at: datetime
    applied_at: datetime | None = None


class ApprovalClaim(BaseModel):
    """Outcome of the atomic ``waiting_approval -> resume_pending`` claim."""

    model_config = ConfigDict(frozen=True)

    claimed: bool
    status: RunLifecycleStatus
    existing: ApprovalDecision | None = None
    lease: LeaseToken | None = None


class RunRepository(Protocol):
    # -- rows -----------------------------------------------------------------

    async def create_run(
        self,
        run_id: UUID,
        task: str,
        *,
        owner: str,
        ttl_seconds: float,
        idempotency_key: str | None = None,
    ) -> CreateOutcome:
        """Insert the row in ``running`` holding a fresh lease, plus ``run_created``.

        With an ``idempotency_key`` the insert is atomic against the unique
        index: a concurrent or earlier request with the same key yields
        ``created=False`` and the existing row (the caller compares the task).
        """
        ...

    async def get_run(self, run_id: UUID) -> PersistedRun | None: ...

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
    ) -> list[tuple[PersistedRun, LeaseToken]]:
        """Recovery: atomically take over runs whose lease is free or expired
        (``FOR UPDATE SKIP LOCKED``), so concurrent sweepers never share one."""
        ...

    # -- audit ----------------------------------------------------------------

    async def append_events(self, run_id: UUID, events: Sequence[RunEvent]) -> int:
        """Insert events, ignoring duplicates; returns how many were new."""
        ...

    async def list_events(self, run_id: UUID) -> list[PersistedEvent]: ...

    # -- approvals ------------------------------------------------------------

    async def claim_approval(
        self, run_id: UUID, approved: bool, *, owner: str, ttl_seconds: float
    ) -> ApprovalClaim | None:
        """One transaction: CAS to ``resume_pending``, record the decision, take
        the lease for applying it, emit the event. ``None`` when the run does
        not exist; ``claimed=False`` carries the current status and any
        decision already recorded."""
        ...

    async def get_approval(self, run_id: UUID) -> ApprovalDecision | None: ...

    async def mark_approval_applied(self, run_id: UUID) -> bool:
        """Set ``applied_at`` once; returns whether this call did it."""
        ...

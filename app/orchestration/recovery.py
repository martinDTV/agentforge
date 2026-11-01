"""Recovery sweep: take over runs nobody is driving and finish what they need.

Runs at startup and periodically. Each sweep claims a bounded batch of rows
whose lease is free or expired (``FOR UPDATE SKIP LOCKED`` in PostgreSQL, so
two sweepers never share a run) and lets ``RunService.recover`` decide per
run from its checkpoint:

- finished or paused but never projected  -> re-project (+ backfill audit)
- decision recorded but not applied       -> apply it (no LLM cost)
- accepted but never started (only the     -> re-drive from that input, whatever
  input checkpoint exists, I0.1)             ``auto_resume`` says; attempts still bound it
- pending work, attempts left             -> ``interrupted`` (default) or re-drive
                                             when ``auto_resume`` is on
- pending work, attempts exhausted        -> ``failed`` / ``abandoned``

Nothing in ``running`` is ever marked failed just for being in ``running``.

The sweep classifies every claimed run and, when a run has to be driven, launches
the drive the way the service is configured (I0.2): as a tracked background task
in the API (``drive_mode="background"``), so a sweep returns as soon as its batch
is classified and never holds other sweeps or requests; awaited inline for
operator scripts and tests. The lease and heartbeat are the same either way.
"""

import asyncio
import logging

from pydantic import BaseModel, Field

from app.contracts.lifecycle import RunLifecycleStatus
from app.infrastructure.db.errors import DatabaseUnavailableError
from app.orchestration.exceptions import LeaseLostError, ShuttingDownError, SweepInProgressError
from app.orchestration.runs import RecoveryOutcome, RunService

logger = logging.getLogger(__name__)


_LAUNCHED = frozenset({RecoveryOutcome.RESUMED, RecoveryOutcome.APPROVAL_APPLIED})


class RecoveryReport(BaseModel):
    claimed: int = 0
    outcomes: dict[str, int] = Field(default_factory=dict)
    errors: int = 0
    expired: int = 0  # waiting runs whose decision window passed (Fase 7)
    # Re-drives handed to a background task (I0.2): counted in ``outcomes`` as
    # ``resumed``/``approval_applied`` but not finished when the sweep returns.
    launched: int = 0

    def add(self, outcome: RecoveryOutcome) -> None:
        self.outcomes[outcome.value] = self.outcomes.get(outcome.value, 0) + 1

    @property
    def touched(self) -> bool:
        return self.claimed > 0 or self.errors > 0 or self.expired > 0


class RecoveryService:
    """Runs sweeps one at a time per process.

    Rows are protected across processes by ``FOR UPDATE SKIP LOCKED`` and the
    leases; the lock here only keeps one process from launching several
    sweeps (manual + periodic) at once, which would multiply LLM work when
    auto-resume is on. A manual sweep that finds the lock busy fails fast
    (409) instead of queueing; the periodic one simply skips that tick.
    """

    def __init__(self, service: RunService) -> None:
        self._service = service
        self._lock = asyncio.Lock()

    @property
    def sweeping(self) -> bool:
        return self._lock.locked()

    @property
    def service(self) -> RunService:
        return self._service

    @property
    def candidate_statuses(self) -> frozenset[RunLifecycleStatus]:
        statuses = {RunLifecycleStatus.RUNNING, RunLifecycleStatus.RESUME_PENDING}
        if self._service.policy.auto_resume:
            statuses.add(RunLifecycleStatus.INTERRUPTED)
        return frozenset(statuses)

    async def sweep(
        self,
        *,
        projects: frozenset[str] | None = None,
        actor: str | None = None,
        wait: bool = True,
    ) -> RecoveryReport:
        """One synchronous pass. ``projects`` narrows the batch (manual, scoped
        sweeps); ``actor`` attributes the resulting events to a principal.
        With ``wait=False`` a busy instance raises ``SweepInProgressError``."""
        if not wait and self._lock.locked():
            raise SweepInProgressError()
        async with self._lock:
            return await self._sweep(projects=projects, actor=actor)

    async def _sweep(self, *, projects: frozenset[str] | None, actor: str | None) -> RecoveryReport:
        report = RecoveryReport()
        service = self._service
        try:
            if projects is None:
                report.expired = len(await service.expire_waiting(limit=service.policy.batch_size))
            claimed = await service.repository.claim_expired_leases(
                owner=service.owner,
                ttl_seconds=service.policy.lease_ttl_seconds,
                statuses=self.candidate_statuses,
                limit=service.policy.batch_size,
                projects=projects,
            )
        except DatabaseUnavailableError as exc:
            logger.warning("Barrido de recuperación sin base de datos: %s", exc.message)
            report.errors += 1
            return report
        try:
            # Publications stopped midway or failed transiently (ADR 0011): idempotent
            # re-drive, no model cost, bounded by max_attempts in ``recover``.
            if projects is None:
                claimed = list(claimed) + await service.repository.claim_interrupted_publications(
                    owner=service.owner,
                    ttl_seconds=service.policy.lease_ttl_seconds,
                    limit=service.policy.batch_size,
                )
        except DatabaseUnavailableError as exc:
            logger.warning("Barrido de publicaciones sin base de datos: %s", exc.message)
            report.errors += 1
        report.claimed = len(claimed)
        for row, lease in claimed:
            try:
                outcome = await service.recover(row, lease, actor=actor)
                report.add(outcome)
                if outcome in _LAUNCHED and service.drive_mode == "background":
                    report.launched += 1
            except (LeaseLostError, ShuttingDownError):
                report.add(RecoveryOutcome.SKIPPED)
            except DatabaseUnavailableError as exc:
                logger.warning("Recuperación de %s interrumpida: %s", row.id, exc.message)
                report.errors += 1
                break
            except Exception:
                logger.exception("Recuperación de %s falló", row.id)
                report.errors += 1
                try:
                    await service.repository.release_lease(row.id, lease)
                except DatabaseUnavailableError:
                    pass
        if report.touched:
            logger.info(
                "Barrido de recuperación: %d reclamados, %s, %d lanzados, %d errores",
                report.claimed,
                report.outcomes or "sin cambios",
                report.launched,
                report.errors,
            )
        return report

    async def run_periodically(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            if not self._service.accepting:
                return
            if self._lock.locked():
                continue  # a manual sweep is running; nothing is lost by skipping a tick
            try:
                await self.sweep()
            except Exception:  # pragma: no cover - defensive: never kill the loop
                logger.exception("Barrido de recuperación con error inesperado")

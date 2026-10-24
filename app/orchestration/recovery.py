"""Recovery sweep: take over runs nobody is driving and finish what they need.

Runs at startup and periodically. Each sweep claims a bounded batch of rows
whose lease is free or expired (``FOR UPDATE SKIP LOCKED`` in PostgreSQL, so
two sweepers never share a run) and lets ``RunService.recover`` decide per
run from its checkpoint:

- finished or paused but never projected  -> re-project (+ backfill audit)
- decision recorded but not applied       -> apply it (no LLM cost)
- pending work, attempts left             -> ``interrupted`` (default) or re-drive
                                             when ``auto_resume`` is on
- pending work, attempts exhausted        -> ``failed`` / ``abandoned``

Nothing in ``running`` is ever marked failed just for being in ``running``.
"""

import asyncio
import logging

from pydantic import BaseModel, Field

from app.contracts.lifecycle import RunLifecycleStatus
from app.infrastructure.db.errors import DatabaseUnavailableError
from app.orchestration.exceptions import LeaseLostError, ShuttingDownError
from app.orchestration.runs import RecoveryOutcome, RunService

logger = logging.getLogger(__name__)


class RecoveryReport(BaseModel):
    claimed: int = 0
    outcomes: dict[str, int] = Field(default_factory=dict)
    errors: int = 0

    def add(self, outcome: RecoveryOutcome) -> None:
        self.outcomes[outcome.value] = self.outcomes.get(outcome.value, 0) + 1

    @property
    def touched(self) -> bool:
        return self.claimed > 0 or self.errors > 0


class RecoveryService:
    def __init__(self, service: RunService) -> None:
        self._service = service

    @property
    def candidate_statuses(self) -> frozenset[RunLifecycleStatus]:
        statuses = {RunLifecycleStatus.RUNNING, RunLifecycleStatus.RESUME_PENDING}
        if self._service.policy.auto_resume:
            statuses.add(RunLifecycleStatus.INTERRUPTED)
        return frozenset(statuses)

    async def sweep(self) -> RecoveryReport:
        report = RecoveryReport()
        service = self._service
        try:
            claimed = await service.repository.claim_expired_leases(
                owner=service.owner,
                ttl_seconds=service.policy.lease_ttl_seconds,
                statuses=self.candidate_statuses,
                limit=service.policy.batch_size,
            )
        except DatabaseUnavailableError as exc:
            logger.warning("Barrido de recuperación sin base de datos: %s", exc.message)
            report.errors += 1
            return report
        report.claimed = len(claimed)
        for row, lease in claimed:
            try:
                report.add(await service.recover(row, lease))
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
                "Barrido de recuperación: %d reclamados, %s, %d errores",
                report.claimed,
                report.outcomes or "sin cambios",
                report.errors,
            )
        return report

    async def run_periodically(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            if not self._service.accepting:
                return
            try:
                await self.sweep()
            except Exception:  # pragma: no cover - defensive: never kill the loop
                logger.exception("Barrido de recuperación con error inesperado")

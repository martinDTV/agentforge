"""Delivery worker: claims due outbox items, sends them off the event loop, records the result.

Bounded retries with backoff; a non-retryable error (bad credentials, rejected
recipient) kills the item immediately with a clear ``last_error``; an uncertain
answer (connection lost after the data was sent) is retried with the *same*
Message-ID, so at worst the reader sees a duplicate they can recognise.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from .contracts import EmailMessage, EmailSender, NotificationOutbox, OutboxItem

logger = logging.getLogger(__name__)

BACKOFF_SECONDS: tuple[float, ...] = (30.0, 120.0, 600.0, 1800.0, 3600.0)
DEFAULT_MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1


class WorkerReport(BaseModel):
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    dead: int = 0
    errors: list[str] = Field(default_factory=list)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NotificationWorker:
    def __init__(
        self,
        outbox: NotificationOutbox,
        sender: EmailSender,
        *,
        thread_domain: str = "agentforge.local",
        batch_size: int = 20,
        lease_seconds: float = 120.0,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._outbox = outbox
        self._sender = sender
        self._domain = thread_domain
        self._batch = batch_size
        self._lease = lease_seconds
        self._max_attempts = max_attempts
        self._clock = clock

    def message_for(self, item: OutboxItem) -> EmailMessage:
        return EmailMessage(
            to=[item.recipient],
            subject=item.subject,
            text=item.text_body,
            html=item.html_body,
            message_id=item.message_id,
            references=[self._thread_for(item)],
        )

    def _thread_for(self, item: OutboxItem) -> str:
        if item.run_id is not None:
            return f"<agentforge.{item.run_id}.thread@{self._domain}>"
        return f"<agentforge.goal.{item.goal_id}.thread@{self._domain}>"

    async def run_once(self) -> WorkerReport:
        report = WorkerReport()
        now = self._clock()
        items = await self._outbox.claim(now=now, limit=self._batch, lease_seconds=self._lease)
        report.claimed = len(items)
        for item in items:
            outcome_ok: bool
            code: str | None
            detail: str | None
            retryable: bool
            try:
                outcome = await asyncio.to_thread(self._sender.send, self.message_for(item))
            except Exception as exc:  # the sender must not kill the worker
                logger.exception("Envío de notificación %s con error inesperado", item.id)
                outcome_ok, code, detail, retryable = (
                    False,
                    "sender_crashed",
                    type(exc).__name__,
                    True,
                )
            else:
                outcome_ok = outcome.ok
                code = outcome.code
                detail = outcome.detail
                retryable = outcome.retryable or outcome.uncertain
            moment = self._clock()
            if outcome_ok:
                await self._outbox.mark_sent(item.id, now=moment)
                report.sent += 1
                continue
            attempts = item.attempts  # already incremented by claim()
            dead = not retryable or attempts >= self._max_attempts
            delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
            error = f"{code or 'send_failed'}: {detail or ''}".strip()[:500]
            await self._outbox.mark_failed(
                item.id,
                now=moment,
                error=error,
                next_attempt_at=None if dead else moment + timedelta(seconds=delay),
                dead=dead,
            )
            if dead:
                report.dead += 1
                logger.error("Notificación %s descartada: %s", item.id, error)
            else:
                report.retried += 1
                logger.warning("Notificación %s reintento en %.0fs: %s", item.id, delay, error)
            report.errors.append(error)
        return report

    async def run_forever(self, interval_seconds: float) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive, never kill the loop
                logger.exception("Worker de notificaciones con error inesperado")
            await asyncio.sleep(interval_seconds)

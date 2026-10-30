"""Notifications (correo) de las etapas de cada ejecución.

Desacoplado del grafo: los eventos durables de ``run_events`` se traducen en
borradores de correo que se guardan en una bandeja de salida persistente
(``notification_outbox``) dentro de la misma transacción que el evento; un
worker independiente los entrega con reintentos acotados. Un fallo del correo
nunca toca la ejecución. Aquí no hay SMTP ni secretos: el adaptador vive en
``app/infrastructure/notifications`` y la contraseña solo en ``Settings``.
"""

from .contracts import (
    EmailMessage,
    EmailSender,
    NotificationKind,
    NotificationOutbox,
    OutboxItem,
    OutboxStatus,
    SendOutcome,
)
from .enqueue import PRIORITY_KINDS, NotificationEnqueuer, NotificationLevel, OutboxDraft
from .policy import GoalContext, NotificationDraft, NotificationPolicy, RunContext
from .templates import render_html, render_subject, render_text
from .worker import BACKOFF_SECONDS, NotificationWorker, WorkerReport

__all__ = [
    "BACKOFF_SECONDS",
    "EmailMessage",
    "EmailSender",
    "GoalContext",
    "NotificationDraft",
    "NotificationEnqueuer",
    "NotificationLevel",
    "PRIORITY_KINDS",
    "NotificationKind",
    "NotificationOutbox",
    "NotificationPolicy",
    "NotificationWorker",
    "OutboxDraft",
    "OutboxItem",
    "OutboxStatus",
    "RunContext",
    "SendOutcome",
    "WorkerReport",
    "render_html",
    "render_subject",
    "render_text",
]

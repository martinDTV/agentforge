"""Plain-text and small HTML renderings of a notification. No secrets, no file contents."""

import html
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class NotificationContent:
    project: str
    task: str
    run_id: str
    stage: str
    status: str
    moment: datetime
    next_action: str | None = None
    details: list[tuple[str, str]] = field(default_factory=list)
    link: str | None = None

    @property
    def short_run_id(self) -> str:
        return self.run_id[:8]


def _task_snippet(task: str, limit: int = 160) -> str:
    one = " ".join(task.split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


def render_subject(content: NotificationContent, headline: str) -> str:
    subject = f"[AgentForge] {content.project} · {content.short_run_id} · {headline}"
    return subject if len(subject) <= 200 else subject[:199] + "…"


def render_text(content: NotificationContent, headline: str) -> str:
    lines = [
        f"AgentForge — {headline}",
        "",
        f"Proyecto:   {content.project}",
        f"Ejecución:  {content.short_run_id} ({content.run_id})",
        f"Etapa:      {content.stage}",
        f"Estado:     {content.status}",
        f"Fecha:      {content.moment.isoformat(timespec='seconds')}",
        f"Tarea:      {_task_snippet(content.task)}",
    ]
    for label, value in content.details:
        lines.append(f"{label + ':':<12}{value}")
    if content.link:
        lines.append(f"Enlace:     {content.link}")
    if content.next_action:
        lines += ["", f"Siguiente acción: {content.next_action}"]
    lines += [
        "",
        "Este correo lo genera AgentForge; no contiene secretos ni archivos del repositorio.",
    ]
    return "\n".join(lines) + "\n"


def render_html(content: NotificationContent, headline: str) -> str:
    esc = html.escape
    rows = [
        ("Proyecto", content.project),
        ("Ejecución", f"{content.short_run_id} ({content.run_id})"),
        ("Etapa", content.stage),
        ("Estado", content.status),
        ("Fecha", content.moment.isoformat(timespec="seconds")),
        ("Tarea", _task_snippet(content.task)),
        *content.details,
    ]

    def cell(value: str) -> str:
        if value.startswith("https://"):
            return f"<a href='{esc(value, quote=True)}'>{esc(value)}</a>"
        return esc(value)

    table = "".join(
        f"<tr><th align='left' style='padding:4px 12px 4px 0;color:#555'>{esc(k)}</th>"
        f"<td style='padding:4px 0'>{cell(v)}</td></tr>"
        for k, v in rows
    )
    link = (
        f"<p><a href='{esc(content.link, quote=True)}'>{esc(content.link)}</a></p>"
        if content.link
        else ""
    )
    action = (
        "<p style='margin-top:16px'><strong>Siguiente acción:</strong> "
        f"{esc(content.next_action)}</p>"
        if content.next_action
        else ""
    )
    return (
        "<!doctype html><html><body style='font-family:-apple-system,Segoe UI,sans-serif;"
        "font-size:14px;color:#222'>"
        f"<h2 style='font-size:16px;margin:0 0 12px'>AgentForge — {esc(headline)}</h2>"
        f"<table cellspacing='0' cellpadding='0'>{table}</table>{link}{action}"
        "<p style='margin-top:20px;color:#777;font-size:12px'>Este correo lo genera AgentForge; "
        "no contiene secretos ni archivos del repositorio.</p></body></html>"
    )

"""Rendering of one notification as subject, text/plain and HTML.

The HTML is built for mail clients, not browsers: one 600 px table, inline
styles, system fonts, no images, no scripts, no remote resources, status
expressed with a word and a symbol (never colour alone). The plain-text
alternative carries every fact the HTML carries. Everything that came from
outside the platform (task text, model summaries, file paths, human reasons)
is already cleaned by ``sanitize`` and is HTML-escaped here.
"""

import html
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .sanitize import safe_https_url

MAX_SUBJECT = 200
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"


class Status(StrEnum):
    PENDING = "pendiente"  # a human must decide
    WORKING = "en curso"  # the platform or an agent is working
    COMPLETED = "completado"
    REJECTED = "rechazado"
    FAILED = "fallido"
    INFO = "informativo"


# (symbol, text colour, background, border). Symbols survive clients that drop styles.
_STATUS_STYLE: dict[Status, tuple[str, str, str, str]] = {
    Status.PENDING: ("●", "#7c4a03", "#fff4df", "#f0b24a"),
    Status.WORKING: ("◐", "#1e3a8a", "#e8effd", "#93b4f5"),
    Status.COMPLETED: ("✓", "#14532d", "#e6f6ea", "#7fcf98"),
    Status.REJECTED: ("✕", "#3f3f46", "#f1f1f3", "#b8b8c2"),
    Status.FAILED: ("✕", "#7f1d1d", "#fdecec", "#f0a3a3"),
    Status.INFO: ("ⓘ", "#1f2933", "#f3f4f6", "#cbd2d9"),
}
_STATUS_LABEL: dict[Status, str] = {
    Status.PENDING: "Pendiente de decisión",
    Status.WORKING: "En curso",
    Status.COMPLETED: "Completado",
    Status.REJECTED: "Rechazado",
    Status.FAILED: "Fallido",
    Status.INFO: "Informativo",
}


@dataclass(frozen=True)
class Section:
    title: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class Link:
    label: str
    url: str


@dataclass(frozen=True)
class Notification:
    """Everything a mail says, independent of its format."""

    headline: str  # "Acción requerida: aprobar plan"
    project: str
    status: Status
    summary: str  # what happened, one or two sentences
    subject_label: str  # "Ejecución" | "Objetivo"
    subject_id: str  # full identifier of the run or goal
    moment: datetime
    task: str | None = None  # the request this is about (bounded snippet)
    facts: tuple[tuple[str, str], ...] = ()  # key facts first
    sections: tuple[Section, ...] = ()  # files, risks, tasks, checks ...
    next_steps: tuple[str, ...] = ()  # "Qué sigue"
    cli: tuple[str, ...] = ()  # commands to run
    links: tuple[Link, ...] = ()  # https only, already validated
    details: tuple[tuple[str, str], ...] = ()  # technical, at the end
    notes: tuple[str, ...] = ()  # caveats, e.g. "texto generado por el modelo"
    subject_suffix: str | None = None  # a short discriminator, only when needed

    @property
    def short_id(self) -> str:
        return self.subject_id[:8]


def render_subject(notification: Notification) -> str:
    suffix = f" ({notification.subject_suffix})" if notification.subject_suffix else ""
    subject = f"[AgentForge] {notification.headline} — {notification.project}{suffix}"
    return subject if len(subject) <= MAX_SUBJECT else subject[: MAX_SUBJECT - 1] + "…"


# -- text/plain ---------------------------------------------------------------------------


def render_text(n: Notification) -> str:
    lines: list[str] = [f"AgentForge — {n.headline}", f"Estado: {_STATUS_LABEL[n.status]}", ""]
    lines.append(n.summary)
    lines.append("")
    lines.append(f"Proyecto: {n.project}")
    lines.append(f"{n.subject_label}: {n.subject_id}")
    if n.task:
        lines.append(f"Solicitud: {n.task}")
    for label, value in n.facts:
        lines.append(f"{label}: {value}")
    for section in n.sections:
        lines.append("")
        lines.append(f"{section.title}:")
        lines.extend(f"  - {item}" for item in section.items)
    if n.next_steps or n.cli:
        lines.append("")
        lines.append("Qué sigue:")
        lines.extend(f"  - {step}" for step in n.next_steps)
        if n.cli:
            lines.append("  Desde la terminal:")
            lines.extend(f"    {command}" for command in n.cli)
    if n.links:
        lines.append("")
        lines.append("Enlaces:")
        lines.extend(f"  - {link.label}: {link.url}" for link in n.links)
    if n.details:
        lines.append("")
        lines.append("Detalles técnicos:")
        lines.extend(f"  {label}: {value}" for label, value in n.details)
    for note in n.notes:
        lines.append("")
        lines.append(f"Nota: {note}")
    lines.append("")
    lines.append(f"Fecha: {n.moment.isoformat(timespec='seconds')}")
    lines.append(
        "Este correo lo genera AgentForge. No contiene secretos ni archivos del repositorio y "
        "ningún enlace aprueba nada por sí mismo."
    )
    return "\n".join(lines) + "\n"


# -- HTML ---------------------------------------------------------------------------------


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _rows(pairs: tuple[tuple[str, str], ...], *, muted: bool = False) -> str:
    colour = "#6b7280" if muted else "#1f2933"
    size = "13px" if muted else "14px"
    return "".join(
        "<tr>"
        f'<td style="padding:4px 12px 4px 0;color:#6b7280;font-size:{size};'
        f'vertical-align:top;white-space:nowrap">{_esc(label)}</td>'
        f'<td style="padding:4px 0;color:{colour};font-size:{size};vertical-align:top;'
        f'word-break:break-word">{_esc(value)}</td>'
        "</tr>"
        for label, value in pairs
    )


def _list(items: tuple[str, ...]) -> str:
    return "".join(
        f'<li style="margin:0 0 4px 0;color:#1f2933;font-size:14px;line-height:20px">'
        f"{_esc(item)}</li>"
        for item in items
    )


def _badge(status: Status) -> str:
    symbol, colour, background, border = _STATUS_STYLE[status]
    return (
        f'<span style="display:inline-block;padding:3px 10px;border-radius:12px;'
        f"border:1px solid {border};background:{background};color:{colour};font-size:12px;"
        f'font-weight:600;letter-spacing:0.2px">{symbol}&nbsp;{_esc(_STATUS_LABEL[status])}'
        "</span>"
    )


def _box(title: str, inner: str, *, background: str, border: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="margin:16px 0 0 0;background:{background};border:1px solid {border};'
        'border-radius:6px"><tr><td style="padding:12px 14px">'
        f'<div style="font-size:12px;font-weight:600;color:#6b7280;text-transform:uppercase;'
        f'letter-spacing:0.6px;margin:0 0 6px 0">{_esc(title)}</div>{inner}</td></tr></table>'
    )


def render_html(n: Notification) -> str:
    parts: list[str] = []
    parts.append(
        f'<h1 style="margin:0 0 8px 0;font-size:20px;line-height:26px;color:#111827;'
        f'font-weight:600">{_esc(n.headline)}</h1>'
    )
    parts.append(f'<div style="margin:0 0 14px 0">{_badge(n.status)}</div>')
    parts.append(
        f'<p style="margin:0 0 14px 0;font-size:15px;line-height:22px;color:#1f2933">'
        f"{_esc(n.summary)}</p>"
    )
    facts: list[tuple[str, str]] = [("Proyecto", n.project), (n.subject_label, n.subject_id)]
    if n.task:
        facts.append(("Solicitud", n.task))
    facts.extend(n.facts)
    parts.append(
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        f"{_rows(tuple(facts))}</table>"
    )
    for section in n.sections:
        parts.append(
            f'<h2 style="margin:18px 0 6px 0;font-size:14px;color:#374151;font-weight:600">'
            f"{_esc(section.title)}</h2>"
            f'<ul style="margin:0;padding:0 0 0 18px">{_list(section.items)}</ul>'
        )
    if n.next_steps or n.cli:
        inner = f'<ul style="margin:0;padding:0 0 0 18px">{_list(n.next_steps)}</ul>'
        if n.cli:
            commands = "<br>".join(_esc(c) for c in n.cli)
            inner += (
                f'<div style="margin:10px 0 0 0;padding:8px 10px;background:#111827;'
                f"color:#e5e7eb;border-radius:4px;font-family:{MONO};font-size:12px;"
                f'line-height:18px;word-break:break-all">{commands}</div>'
            )
        parts.append(_box("Qué sigue", inner, background="#f8fafc", border="#d9e2ec"))
    if n.links:
        anchors = "".join(
            f'<p style="margin:0 0 6px 0;font-size:14px"><a href="{_esc(link.url)}" '
            f'style="color:#1d4ed8;text-decoration:underline">{_esc(link.label)} →</a>'
            f'<br><span style="font-size:12px;color:#6b7280;word-break:break-all">'
            f"{_esc(link.url)}</span></p>"
            for link in n.links
            if safe_https_url(link.url) is not None
        )
        if anchors:
            parts.append(_box("Enlaces", anchors, background="#ffffff", border="#d9e2ec"))
    if n.details:
        parts.append(
            f'<h2 style="margin:18px 0 4px 0;font-size:12px;color:#6b7280;font-weight:600;'
            f'text-transform:uppercase;letter-spacing:0.6px">Detalles técnicos</h2>'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
            f"{_rows(n.details, muted=True)}</table>"
        )
    for note in n.notes:
        parts.append(
            f'<p style="margin:12px 0 0 0;font-size:12px;line-height:18px;color:#6b7280">'
            f"{_esc(note)}</p>"
        )
    body = "".join(parts)
    return (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(n.headline)}</title></head>"
        '<body style="margin:0;padding:0;background:#eef1f5">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="background:#eef1f5"><tr><td align="center" style="padding:20px 10px">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="max-width:600px;width:100%;background:#ffffff;border:1px solid #d9e2ec;'
        f'border-radius:8px;font-family:{FONT}">'
        '<tr><td style="padding:12px 24px;border-bottom:1px solid #e5e7eb;font-size:13px;'
        'font-weight:600;letter-spacing:0.8px;color:#6b7280;text-transform:uppercase">'
        "AgentForge</td></tr>"
        f'<tr><td style="padding:22px 24px 8px 24px">{body}</td></tr>'
        '<tr><td style="padding:14px 24px 20px 24px;border-top:1px solid #e5e7eb;'
        'font-size:12px;line-height:18px;color:#6b7280">'
        f"Fecha: {_esc(n.moment.isoformat(timespec='seconds'))}<br>"
        "Este correo lo genera AgentForge. No contiene secretos ni archivos del repositorio y "
        "ningún enlace aprueba nada por sí mismo.</td></tr>"
        "</table></td></tr></table></body></html>"
    )

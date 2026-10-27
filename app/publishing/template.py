"""Commit message, pull request title and body (ADR 0011 §6), rendered by the platform.

Model text is data: it is quoted inside a fenced block, truncated, stripped
of control characters and never allowed to mention users. No links, no
e-mail addresses and no credentials are ever interpolated.
"""

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from .plan import PublicationPlan

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MENTION = re.compile(r"@(?=[A-Za-z0-9_\-])")
AI_WARNING = (
    "Estos cambios fueron generados mediante agentes de IA (AgentForge) dentro de un alcance "
    "aprobado por una persona. Requieren revisión humana antes de fusionarse; AgentForge no "
    "fusiona Pull Requests."
)


def neutralize_mentions(text: str) -> str:
    """``@usuario`` never notifies anyone: a zero-width space follows the ``@``."""
    return _MENTION.sub("@​", text)


def quote_untrusted(text: str, *, limit: int = 4000) -> str:
    """Model text inside a fenced block: no Markdown, no HTML, no mentions, bounded."""
    cleaned = _CONTROL.sub("", text or "").replace("```", "'''").strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "\n[... truncado]"
    return "```text\n" + neutralize_mentions(cleaned) + "\n```"


def _one_line(text: str, limit: int) -> str:
    line = " ".join(_CONTROL.sub("", text or "").split())
    line = neutralize_mentions(line)
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def pull_request_title(prefix: str, task: str, *, limit: int = 120) -> str:
    prefix = _one_line(prefix, 32)
    return f"{prefix} {_one_line(task, max(10, limit - len(prefix) - 1))}"


def commit_message(
    *,
    title: str,
    task: str,
    run_id: UUID,
    base_sha: str,
    scope_digest: str,
    files: Sequence[tuple[str, str]],
    scope_approved_by: str | None,
) -> str:
    """Subject + body + trailers; ``files`` are ``(action, path)`` pairs of the approved scope."""
    subject = _one_line(title, 72)
    listed = "\n".join(f"- {action}: {path}" for action, path in files)
    trailers = [f"Run-Id: {run_id}", f"Base-Sha: {base_sha}", f"Scope-Digest: {scope_digest}"]
    if scope_approved_by:
        trailers.append(f"Scope-Approved-By: {_one_line(scope_approved_by, 80)}")
    return (
        f"{subject}\n\n"
        f"Tarea: {_one_line(task, 500)}\n\n"
        f"Archivos dentro del alcance aprobado:\n{listed}\n\n"
        "Generado por AgentForge; revisar antes de fusionar.\n\n" + "\n".join(trailers) + "\n"
    )


def _fmt(moment: datetime | None) -> str:
    return moment.isoformat(timespec="seconds") if moment else "-"


def pull_request_body(
    plan: PublicationPlan,
    *,
    task: str,
    developer_summary: str,
    qa_summary: str,
    qa_checks: Sequence[dict[str, Any]],
    scope_approved_by: str | None,
    scope_approved_at: datetime | None,
    publish_approved_by: str | None,
    publish_approved_at: datetime | None,
) -> str:
    files = "\n".join(
        f"| `{f.path}` | {f.change} | `{(f.sha256 or '-')[:12]}` | {f.size} |" for f in plan.files
    )
    checks = (
        "\n".join(
            f"| `{c.get('program', '?')}` | {c.get('exit_code', '?')} | "
            f"{'ok' if c.get('exit_code') == 0 else 'falló'} |"
            for c in qa_checks
        )
        or "| - | - | sin comprobaciones registradas |"
    )
    sections = [
        f"> ⚠️ {AI_WARNING}",
        "",
        "## Resumen",
        "",
        f"**Tarea solicitada**\n\n{quote_untrusted(task, limit=1500)}",
        "",
        "**Resumen de Developer (texto generado por el modelo)**\n\n"
        + quote_untrusted(developer_summary),
        "",
        "## Archivos modificados",
        "",
        "| Ruta | Cambio | SHA-256 | Bytes |",
        "| --- | --- | --- | --- |",
        files,
        "",
        "## Pruebas ejecutadas en el sandbox",
        "",
        "| Programa | Código de salida | Resultado |",
        "| --- | --- | --- |",
        checks,
        "",
        f"**Revisión de QA (texto generado por el modelo)**\n\n{quote_untrusted(qa_summary)}",
        "",
        "## Commit base y trazabilidad",
        "",
        f"- Repositorio: `{plan.full_name}` (id {plan.github_repository_id})",
        f"- Rama base: `{plan.base_branch}` en `{plan.base_sha}`",
        f"- Rama publicada: `{plan.remote_branch}` en `{plan.head_sha}`",
        f"- Digest del parche: `{plan.patch_digest}`",
        f"- Ejecución: `{plan.run_id}`",
        f"- Alcance aprobado por `{_one_line(scope_approved_by or '-', 80)}` "
        f"el {_fmt(scope_approved_at)} (digest `{plan.scope_digest[:16]}…`)",
        f"- Publicación aprobada por `{_one_line(publish_approved_by or '-', 80)}` "
        f"el {_fmt(publish_approved_at)} (digest `{plan.digest()[:16]}…`)",
        "",
        "_Pull Request en borrador creado por la GitHub App de AgentForge. No se fusiona "
        "automáticamente._",
    ]
    return "\n".join(sections) + "\n"

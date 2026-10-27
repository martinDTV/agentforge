"""Which durable events become notifications, and what each one says (ADR-free, see README).

One draft per ``(run, kind, ordinal)``: the dedup key makes re-appended events
and replayed transactions harmless. Repeated node events of a correction loop
keep their revision as ordinal, so the human sees "QA pidió correcciones
(ronda 2)" once, never once per model call.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.contracts.lifecycle import RunEventKind

from .contracts import NotificationKind
from .templates import NotificationContent, render_html, render_subject, render_text

_K = NotificationKind


@dataclass(frozen=True)
class RunContext:
    run_id: UUID
    project_id: str
    task: str
    workflow_kind: str = "development"
    repository_id: str | None = None


@dataclass(frozen=True)
class NotificationDraft:
    kind: NotificationKind
    ordinal: int
    subject: str
    text: str
    html: str
    headline: str

    def dedup_key(self, run_id: UUID, recipient: str) -> str:
        return f"{run_id}:{self.kind}:{self.ordinal}:{recipient.lower()}"


def message_id_for(run_id: UUID, kind: NotificationKind, ordinal: int, domain: str) -> str:
    return f"<agentforge.{run_id}.{kind}.{ordinal}@{domain}>"


def thread_root_id(run_id: UUID, domain: str) -> str:
    return f"<agentforge.{run_id}.thread@{domain}>"


class NotificationPolicy:
    def __init__(self, *, base_url: str | None = None) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None

    def _link(self, run_id: UUID) -> str | None:
        return f"{self._base_url}/api/v1/runs/{run_id}" if self._base_url else None

    def draft(
        self,
        run: RunContext,
        *,
        kind: RunEventKind,
        payload: dict[str, Any],
        ordinal: int,
        moment: datetime,
    ) -> NotificationDraft | None:
        """The notification an event triggers, or ``None`` for events nobody needs by mail."""
        spec = self._spec(run, kind, payload, ordinal)
        if spec is None:
            return None
        nkind, headline, stage, status, next_action, details, link, ord_ = spec
        content = NotificationContent(
            project=run.project_id,
            task=run.task,
            run_id=str(run.run_id),
            stage=stage,
            status=status,
            moment=moment,
            next_action=next_action,
            details=details,
            link=link or self._link(run.run_id),
        )
        return NotificationDraft(
            kind=nkind,
            ordinal=ord_,
            subject=render_subject(content, headline),
            text=render_text(content, headline),
            html=render_html(content, headline),
            headline=headline,
        )

    def _spec(
        self, run: RunContext, kind: RunEventKind, payload: dict[str, Any], ordinal: int
    ) -> (
        tuple[
            NotificationKind,
            str,
            str,
            str,
            str | None,
            list[tuple[str, str]],
            str | None,
            int,
        ]
        | None
    ):
        K = RunEventKind
        approve = "Revisa la propuesta y decide con POST /runs/{id}/approve"
        if kind is K.RUN_CREATED:
            return (_K.RUN_CREATED, "Ejecución creada", "inicio", "en curso", None, [], None, 0)
        if kind is K.ROUTE_SELECTED:
            route = str(payload.get("route", "?"))
            mode = str(payload.get("mode", "auto"))
            reason = str(payload.get("reason", ""))[:200]
            nxt = (
                "Planner y Architect preparan el plan (ruta completa)"
                if route == "full"
                else "Scoper prepara el alcance (ruta rápida)"
            )
            return (
                _K.ROUTE_SELECTED,
                f"Ruta seleccionada: {route}",
                "enrutamiento",
                "en curso",
                nxt,
                [("Modo", mode), ("Motivo", reason)],
                None,
                0,
            )
        if kind is K.ROUTE_ESCALATED:
            return (
                _K.ROUTE_ESCALATED,
                "Ruta escalada a completa",
                "enrutamiento",
                "en curso",
                "Planner y Architect revisan antes de aprobar el alcance",
                [("Motivo", str(payload.get("reason", ""))[:200])],
                None,
                0,
            )
        if kind is K.ARCHITECT_REVIEWED:
            approved = bool(payload.get("approved"))
            return (
                _K.PLANNING_COMPLETED,
                "Planeación finalizada" if approved else "Architect pidió revisar el plan",
                "planeación",
                "plan aprobado por Architect" if approved else f"revisión {ordinal}",
                "Esperando decisión humana sobre el plan" if approved else None,
                [("Observaciones", str(payload.get("issues", 0)))],
                None,
                ordinal,
            )
        if kind is K.WAITING_APPROVAL:
            stage = str(payload.get("stage") or "plan")
            deadline = str(payload.get("deadline", ""))
            if stage == "plan":
                return (
                    _K.PLAN_PENDING,
                    "Plan pendiente de aprobación",
                    "plan",
                    "esperando aprobación",
                    approve,
                    [("Caduca", deadline)],
                    None,
                    ordinal,
                )
            if stage == "scope":
                return (
                    _K.SCOPE_PENDING,
                    "Alcance pendiente de aprobación",
                    "scope",
                    "esperando aprobación",
                    approve,
                    [("Caduca", deadline)],
                    None,
                    ordinal,
                )
            if stage == "publish":
                return (
                    _K.PUBLISH_PENDING,
                    "Plan de publicación listo para aprobar",
                    "publish",
                    "esperando aprobación",
                    approve + " (stage=publish, publish_digest)",
                    [("Caduca", deadline)],
                    None,
                    ordinal,
                )
            return None
        if kind in (K.RUN_APPROVED, K.APPROVAL_RECORDED):
            # Human decisions. The development flow records them as APPROVAL_RECORDED
            # (with ``approved``) and the planning graph as RUN_APPROVED. Only the scope
            # approval starts work worth announcing: a plan approval is followed by the
            # scope-pending mail and a publish approval by the publication outcome.
            if kind is K.APPROVAL_RECORDED and not payload.get("approved", False):
                return None
            if str(payload.get("stage", "plan")) != "scope":
                return None
            by = str(payload.get("decided_by") or payload.get("approved_by") or "-")
            return (
                _K.DEVELOPER_STARTED,
                "Alcance aprobado; Developer iniciado",
                "developer",
                "en curso",
                "Developer escribe dentro del alcance; QA validará en el sandbox",
                [("Aprobado por", by)],
                None,
                0,
            )
        if kind is K.DEVELOPER_COMPLETED:
            return (
                _K.DEVELOPER_COMPLETED,
                f"Developer completado (ronda {ordinal + 1})",
                "developer",
                "cambios propuestos",
                "Se aplican los cambios y QA los valida",
                [("Archivos", str(payload.get("files", 0)))],
                None,
                ordinal,
            )
        if kind is K.DEVELOPER_APPLIED:
            return (
                _K.QA_STARTED,
                f"QA iniciado (ronda {ordinal + 1})",
                "qa",
                "en curso",
                "QA ejecuta ruff y pytest en el sandbox",
                [("Archivos", str(payload.get("files", 0)))],
                None,
                ordinal,
            )
        if kind is K.QA_REVIEWED:
            if payload.get("approved"):
                return (
                    _K.QA_APPROVED,
                    "QA aprobado",
                    "qa",
                    "aprobado",
                    "La plataforma prepara el plan de publicación",
                    [("Comprobaciones", str(payload.get("checks", 0)))],
                    None,
                    ordinal,
                )
            return (
                _K.QA_CHANGES_REQUESTED,
                f"QA solicitó correcciones (ronda {ordinal + 1})",
                "qa",
                "correcciones",
                "Developer corrige y QA vuelve a validar",
                [
                    ("Observaciones", str(payload.get("issues", 0))),
                    ("Comprobaciones fallidas", str(payload.get("failed_checks", 0))),
                ],
                None,
                ordinal,
            )
        if kind is K.PULL_REQUEST_OPENED:
            url = payload.get("pr_url")
            return (
                _K.PULL_REQUEST_OPENED,
                f"Pull Request #{payload.get('pr_number', '?')} creado (borrador)",
                "publish",
                "publicado",
                "Revisa el Pull Request en GitHub; AgentForge no fusiona",
                [
                    ("Pull Request", str(url) if url else "-"),
                    ("Rama", str(payload.get("remote_branch", ""))),
                ],
                None,
                0,
            )
        if kind is K.RUN_PUBLISHED:
            return (
                _K.RUN_PUBLISHED,
                "Ejecución publicada",
                "fin",
                "published",
                "Revisar y fusionar manualmente si procede",
                [("PR", str(payload.get("pr_number", "?")))],
                None,
                0,
            )
        if kind is K.RUN_COMPLETED:
            return (
                _K.RUN_COMPLETED,
                "Ejecución completada",
                "fin",
                "completed",
                "El trabajo queda en el worktree local",
                [],
                None,
                0,
            )
        if kind is K.RUN_FAILED:
            return (
                _K.RUN_FAILED,
                "Ejecución fallida",
                str(payload.get("node", "fin")),
                "failed",
                "Revisa el código de error y decide si crear una nueva ejecución",
                [("Código", str(payload.get("code", "unknown")))],
                None,
                0,
            )
        if kind is K.RUN_CANCELLED:
            return (
                _K.RUN_CANCELLED,
                "Ejecución cancelada",
                "fin",
                "cancelled",
                None,
                [("Por", str(payload.get("cancelled_by", "-")))],
                None,
                0,
            )
        if kind is K.RUN_EXPIRED:
            return (
                _K.RUN_EXPIRED,
                "Ejecución caducada sin decisión",
                "fin",
                "expired",
                "Crea una nueva ejecución si el trabajo sigue siendo necesario",
                [],
                None,
                0,
            )
        if kind is K.RUN_REJECTED:
            return (
                _K.RUN_COMPLETED,
                "Decisión: rechazada",
                "fin",
                "rejected",
                None,
                [("Etapa", str(payload.get("stage", "plan")))],
                None,
                0,
            )
        return None

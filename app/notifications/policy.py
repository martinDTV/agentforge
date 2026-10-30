"""Which durable events become notifications, and what each one says.

One draft per ``(run or goal, kind, ordinal)``: the dedup key makes re-appended
events and replayed transactions harmless. Everything a mail states comes from
one of three sources and is labelled accordingly:

- facts the platform recorded (status, stage, files, checks, PR numbers);
- the formal reason a human wrote with a decision (``reason``), shown as such;
- text written by a model (plan or scope summaries, QA observations), shown with
  a note that says so and never turned into a link or an instruction.

Nothing is inferred: without a recorded reason the mail says there is none;
without a known explanation for an error code the mail shows the code.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.contracts.goals import GoalEventKind
from app.contracts.lifecycle import RunEventKind

from .contracts import NotificationKind
from .links import PublicLinks
from .sanitize import clean_lines, clean_text, github_url, short_id, short_sha
from .templates import Link, Notification, Section, Status, render_html, render_subject, render_text

_K = NotificationKind
MODEL_TEXT_NOTE = (
    "Los resúmenes y observaciones marcados como «del modelo» los redactó Claude; AgentForge "
    "los transmite sin interpretarlos."
)
NO_REASON = "Sin motivo registrado: la decisión se tomó sin comentario."
STAGE_NAMES = {"plan": "plan", "scope": "alcance", "publish": "publicación"}
# Neutral explanations of platform error codes. Unknown codes are shown as such.
ERROR_EXPLANATIONS: dict[str, str] = {
    "correction_limit": "QA no aprobó los cambios tras agotar las rondas de corrección.",
    "development_timeout": "Se agotó el tiempo de pared autorizado para la ejecución.",
    "development_interrupted": (
        "La ejecución se interrumpió (reinicio o caída) y la plataforma no la reanuda sola."
    ),
    "scope_empty": "Ningún archivo propuesto por el Scoper era admisible según la política.",
    "scope_outside_policy": "El alcance aprobado dejó de estar permitido por la política.",
    "scope_context_unavailable": "No se pudo leer el worktree para preparar el alcance.",
    "path_denied": "El modelo intentó escribir fuera del alcance aprobado y se detuvo.",
    "change_too_large": "El cambio superó los límites de tamaño del proyecto.",
    "unsafe_model_context": "Una lectura contenía secretos o estaba truncada; se detuvo.",
    "validation_incomplete": "Una comprobación del sandbox no terminó o truncó su salida.",
    "publication_scan_failed": "El escaneo previo bloqueó la publicación (contenido sensible).",
    "base_moved": "La rama base avanzó desde la aprobación del alcance; no se publica sobre ella.",
    "llm_budget_exceeded": "Se alcanzó el presupuesto de llamadas al modelo de la ejecución.",
    "development_budget_exceeded": "Se agotó el presupuesto de llamadas, tokens o coste.",
    "llm_timeout": "El proveedor del modelo no respondió a tiempo.",
    "llm_unavailable": "El proveedor del modelo no estaba disponible.",
    "llm_request_rejected": "El proveedor del modelo rechazó la petición.",
    "llm_malformed_output": "El modelo devolvió una salida que no cumplía el esquema.",
    "abandoned": "La ejecución agotó sus intentos de recuperación.",
    "runner_busy": "El Runner no tenía capacidad en ese momento.",
    "runner_unreachable": "El Runner no era accesible.",
    "mirror_sync_failed": "No se pudo sincronizar el espejo del repositorio.",
    "decision_invalid": "La decisión recibida no era válida para la etapa.",
    "decision_mismatch": "La decisión no correspondía a la propuesta pendiente.",
    "goal_dependency_cycle": "Las dependencias del plan formaban un ciclo.",
    "goal_limit_reached": "El plan superaba el número máximo de tareas del objetivo.",
    "goal_deadline": "Se venció el plazo del objetivo.",
    "pr_closed": "El Pull Request se cerró sin fusionar.",
    "budget_calls": "Se agotó el presupuesto de llamadas al modelo del objetivo.",
    "budget_usd": "Se agotó el presupuesto en USD del objetivo.",
    "budget_tasks": "Se alcanzó el número máximo de tareas del objetivo.",
}


@dataclass(frozen=True)
class RunContext:
    run_id: UUID
    project_id: str
    task: str
    workflow_kind: str = "development"
    repository_id: str | None = None
    # What the row knows at enqueue time (optional: older callers pass none of it).
    plan: dict[str, Any] | None = None
    publication: dict[str, Any] | None = None
    error_code: str | None = None
    pending_stage: str | None = None


@dataclass(frozen=True)
class GoalContext:
    goal_id: UUID
    project_id: str
    repository_id: str
    objective: str


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

    def goal_dedup_key(self, goal_id: UUID, recipient: str) -> str:
        return f"goal:{goal_id}:{self.kind}:{self.ordinal}:{recipient.lower()}"


def message_id_for(run_id: UUID, kind: NotificationKind, ordinal: int, domain: str) -> str:
    return f"<agentforge.{run_id}.{kind}.{ordinal}@{domain}>"


def thread_root_id(run_id: UUID, domain: str) -> str:
    return f"<agentforge.{run_id}.thread@{domain}>"


def goal_message_id_for(goal_id: UUID, kind: NotificationKind, ordinal: int, domain: str) -> str:
    return f"<agentforge.goal.{goal_id}.{kind}.{ordinal}@{domain}>"


def explain(code: str | None) -> str:
    code = clean_text(code, 64) or "unknown"
    base = code.split(":", 1)[0]
    known = ERROR_EXPLANATIONS.get(code) or ERROR_EXPLANATIONS.get(base)
    if known:
        return known
    return f"La plataforma registró el código «{code}»; no hay más detalle estructurado."


def _stage(value: Any) -> str:
    return STAGE_NAMES.get(str(value or "plan"), clean_text(value, 24) or "plan")


def _deadline(value: Any) -> str:
    text = clean_text(value, 40)
    return text if text and text != "None" else "sin plazo"


def _checks(checks: Any) -> str:
    items = []
    for check in checks if isinstance(checks, list) else []:
        if isinstance(check, dict):
            program = clean_text(check.get("program"), 32)
            code = check.get("exit_code")
            verdict = "ok" if code == 0 else f"código {code}" if code is not None else "sin dato"
            items.append(f"{program}: {verdict}")
    return " · ".join(items) if items else "sin datos"


def _fmt_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return clean_text(value, 24) or "0"
    return str(int(number)) if number.is_integer() else f"{number:.2f}"


class NotificationPolicy:
    def __init__(self, *, links: PublicLinks | None = None, base_url: str | None = None) -> None:
        # ``base_url`` is the historical argument; it is honoured only if it passes the
        # strict validation (an https destination), never for loopback addresses.
        self._links = links or PublicLinks.resolve(None, legacy=base_url)

    @property
    def links(self) -> PublicLinks:
        return self._links

    # -- runs -------------------------------------------------------------------------------

    def draft(
        self,
        run: RunContext,
        *,
        kind: RunEventKind,
        payload: dict[str, Any],
        ordinal: int,
        moment: datetime,
        context: dict[str, Any] | None = None,
    ) -> NotificationDraft | None:
        built = self._run_notification(run, kind, payload or {}, context or {}, ordinal, moment)
        if built is None:
            return None
        nkind, notification, ord_ = built
        return _draft(nkind, ord_, notification)

    def _cli_run(self, run: RunContext, *actions: str) -> tuple[str, ...]:
        return tuple(f"uv run python -m scripts.agentforge {a} {run.run_id}" for a in actions)

    def _run_links(self, run: RunContext, *extra: Link) -> tuple[Link, ...]:
        links: list[Link] = list(extra)
        url = self._links.run_url(run.run_id)
        if url:
            links.append(Link("Ver la ejecución en AgentForge", url))
        return tuple(links)

    def _base(
        self,
        run: RunContext,
        *,
        headline: str,
        status: Status,
        summary: str,
        moment: datetime,
        **extra: Any,
    ) -> Notification:
        return Notification(
            headline=headline,
            project=clean_text(run.project_id, 64),
            status=status,
            summary=summary,
            subject_label="Ejecución",
            subject_id=str(run.run_id),
            moment=moment,
            task=clean_text(run.task, 220) or None,
            **extra,
        )

    def _run_notification(
        self,
        run: RunContext,
        kind: RunEventKind,
        payload: dict[str, Any],
        ctx: dict[str, Any],
        ordinal: int,
        moment: datetime,
    ) -> tuple[NotificationKind, Notification, int] | None:
        K = RunEventKind
        if kind is K.RUN_CREATED:
            planning = run.workflow_kind == "planning"
            return (
                _K.RUN_CREATED,
                self._base(
                    run,
                    headline="Ejecución creada",
                    status=Status.WORKING,
                    summary="AgentForge recibió la solicitud y empezó a trabajar.",
                    moment=moment,
                    facts=(("Flujo", "planificación" if planning else "desarrollo"),),
                    next_steps=(
                        (
                            "Planner y Architect preparan el plan; recibirás un aviso para "
                            "aprobarlo.",
                        )
                        if planning
                        else (
                            "El Smart Router elige la ruta; si hace falta plan lo verás antes "
                            "del alcance.",
                            "Las decisiones de plan, alcance y publicación siguen siendo tuyas.",
                        )
                    ),
                    cli=self._cli_run(run, "show"),
                    links=self._run_links(run),
                ),
                0,
            )
        if kind is K.ROUTE_SELECTED:
            route = clean_text(payload.get("route"), 8) or "?"
            return (
                _K.ROUTE_SELECTED,
                self._base(
                    run,
                    headline=f"Ruta seleccionada: {route}",
                    status=Status.WORKING,
                    summary=(
                        "La ruta completa pasa por Planner y Architect antes del alcance."
                        if route == "full"
                        else "La ruta rápida va directa al Scoper; el plan no se repite."
                    ),
                    moment=moment,
                    facts=(
                        ("Modo pedido", clean_text(payload.get("mode"), 8) or "auto"),
                        ("Motivo", clean_text(payload.get("reason"), 200) or "-"),
                    ),
                    details=(
                        ("Política del router", clean_text(payload.get("policy_version"), 32)),
                    ),
                ),
                0,
            )
        if kind is K.ROUTE_ESCALATED:
            return (
                _K.ROUTE_ESCALATED,
                self._base(
                    run,
                    headline="Ruta escalada a completa",
                    status=Status.WORKING,
                    summary=(
                        "El Scoper encontró más complejidad de la prevista: Planner y Architect "
                        "revisan antes de que apruebes el alcance."
                    ),
                    moment=moment,
                    facts=(("Motivo", clean_text(payload.get("reason"), 200) or "-"),),
                ),
                0,
            )
        if kind is K.ARCHITECT_REVIEWED:
            approved = bool(payload.get("approved"))
            return (
                _K.PLANNING_COMPLETED,
                self._base(
                    run,
                    headline="Plan listo para revisión"
                    if approved
                    else f"Architect pidió revisar el plan (revisión {ordinal})",
                    status=Status.WORKING,
                    summary=(
                        "El Architect aprobó el plan del Planner; enseguida se pedirá tu decisión."
                        if approved
                        else "El Architect devolvió observaciones; el Planner revisa el plan."
                    ),
                    moment=moment,
                    facts=(("Observaciones del Architect", str(payload.get("issues", 0))),),
                ),
                ordinal,
            )
        if kind is K.WAITING_APPROVAL:
            return self._waiting(run, payload, ctx, ordinal, moment)
        if kind is K.APPROVAL_RECORDED:
            return self._decision(run, payload, ordinal, moment)
        if kind in (K.RUN_APPROVED, K.RUN_REJECTED):
            return None  # the decision mail comes from ``approval_recorded``
        if kind is K.DEVELOPER_COMPLETED:
            files = clean_lines(ctx.get("files"), max_items=16)
            summary = clean_text(ctx.get("summary"), 300)
            return (
                _K.DEVELOPER_COMPLETED,
                self._base(
                    run,
                    headline=f"Developer propuso cambios (ronda {ordinal + 1})",
                    status=Status.WORKING,
                    summary=(
                        f"Developer entregó el contenido de {payload.get('files', 0)} archivos "
                        "dentro del alcance aprobado; se aplican y QA los valida en el sandbox."
                    ),
                    moment=moment,
                    facts=(("Resumen del modelo", summary),) if summary else (),
                    sections=(Section("Archivos propuestos", tuple(files)),) if files else (),
                    next_steps=("QA ejecuta ruff y pytest en el Runner y revisa el diff.",),
                    notes=(MODEL_TEXT_NOTE,) if summary else (),
                ),
                ordinal,
            )
        if kind is K.DEVELOPER_APPLIED:
            return (
                _K.QA_STARTED,
                self._base(
                    run,
                    headline=f"QA valida los cambios (ronda {ordinal + 1})",
                    status=Status.WORKING,
                    summary=(
                        f"Se escribieron {payload.get('files', 0)} archivos en el worktree "
                        "aislado; QA ejecuta las comprobaciones técnicas y revisa el resultado."
                    ),
                    moment=moment,
                    next_steps=(
                        "Si QA aprueba, se prepara la publicación; si no, Developer corrige.",
                    ),
                ),
                ordinal,
            )
        if kind is K.QA_REVIEWED:
            return self._qa(run, payload, ctx, ordinal, moment)
        if kind is K.PULL_REQUEST_OPENED:
            return self._pull_request(run, payload, moment)
        if kind is K.RUN_PUBLISHED:
            # Same mail as the pull request one (same kind and ordinal): the outbox dedups it,
            # so a publication recovered without the PR event still gets exactly one mail.
            return self._pull_request(run, payload, moment)
        if kind is K.RUN_COMPLETED:
            if ctx.get("publication_expected") is True:
                # QA approved on a repository with a remote: the run goes on to publication,
                # whose own mails follow. Saying "completed" here would be false.
                return None
            return (
                _K.RUN_COMPLETED,
                self._base(
                    run,
                    headline="Ejecución completada",
                    status=Status.COMPLETED,
                    summary=(
                        "QA aprobó los cambios y la ejecución terminó. No se publicó nada: el "
                        "trabajo queda en el worktree local de la ejecución."
                    ),
                    moment=moment,
                    next_steps=(
                        "Revisa el worktree si quieres conservar el trabajo (retención limitada).",
                    ),
                    cli=self._cli_run(run, "show", "events"),
                    links=self._run_links(run),
                ),
                0,
            )
        if kind is K.RUN_FAILED:
            return self._failed(run, payload, ctx, moment)
        if kind is K.RUN_CANCELLED:
            by = clean_text(payload.get("cancelled_by"), 64)
            return (
                _K.RUN_CANCELLED,
                self._base(
                    run,
                    headline="Ejecución cancelada",
                    status=Status.REJECTED,
                    summary=(
                        f"{by or 'Una persona'} retiró la ejecución mientras esperaba una "
                        "decisión. No se ejecutó nada después."
                    ),
                    moment=moment,
                    facts=(("Cancelada por", by or "-"),),
                    next_steps=("Si el trabajo sigue haciendo falta, crea una nueva solicitud.",),
                    cli=self._cli_run(run, "show"),
                ),
                0,
            )
        if kind is K.RUN_EXPIRED:
            stage = _stage(run.pending_stage) if run.pending_stage else None
            return (
                _K.RUN_EXPIRED,
                self._base(
                    run,
                    headline="Decisión caducada",
                    status=Status.FAILED,
                    summary=(
                        "Nadie decidió "
                        + ("la etapa de " + stage if stage else "la etapa pendiente")
                        + " dentro del plazo, así que la ejecución terminó como caducada. No se "
                        "escribió ni publicó nada después."
                    ),
                    moment=moment,
                    next_steps=("Si el trabajo sigue haciendo falta, crea una nueva solicitud.",),
                    cli=self._cli_run(run, "show"),
                ),
                0,
            )
        return None

    def _waiting(
        self,
        run: RunContext,
        payload: dict[str, Any],
        ctx: dict[str, Any],
        ordinal: int,
        moment: datetime,
    ) -> tuple[NotificationKind, Notification, int] | None:
        stage = str(payload.get("stage") or ctx.get("stage") or "plan")
        deadline = _deadline(payload.get("deadline"))
        approve_cli = self._cli_run(run, "pending", "approve")
        reject_hint = f'uv run python -m scripts.agentforge reject {run.run_id} --reason "…"'
        if stage == "plan":
            plan = ctx if ctx.get("tasks") is not None else {}
            tasks_raw = plan.get("tasks") or []
            plan_summary = clean_text(plan.get("plan_summary"), 300)
            tasks = []
            for t in tasks_raw if isinstance(tasks_raw, list) else []:
                if not isinstance(t, dict):
                    continue
                deps = clean_lines(t.get("dependencies"), max_items=8)
                line = f"{clean_text(t.get('id'), 32)}: {clean_text(t.get('title'), 160)}"
                if deps:
                    line += f" (depende de {', '.join(deps)})"
                tasks.append(line)
            # Without ephemeral context (an event rebuilt from history), the row's plan helps.
            if not tasks and isinstance(run.plan, dict):
                for t in (run.plan.get("tasks") or [])[:8]:
                    if isinstance(t, dict):
                        tasks.append(
                            f"{clean_text(t.get('id'), 32)}: {clean_text(t.get('title'), 160)}"
                        )
                plan_summary = plan_summary or clean_text(run.plan.get("summary"), 300)
            risks = clean_lines(plan.get("risks"), max_items=5)
            issues = clean_lines(plan.get("architecture_issues"), max_items=5)
            approved = plan.get("architect_approved")
            verdict = (
                "aprobó el plan"
                if approved is True
                else "mantiene observaciones"
                if approved is False
                else "sin dato"
            )
            sections = []
            if tasks:
                sections.append(Section(f"Tareas propuestas ({len(tasks)})", tuple(tasks)))
            if risks:
                sections.append(Section("Riesgos señalados por el Planner", tuple(risks)))
            if issues:
                sections.append(Section("Observaciones del Architect", tuple(issues)))
            facts: list[tuple[str, str]] = [("Etapa", "plan"), ("Architect", verdict)]
            if plan_summary:
                facts.append(("Resumen del plan (del modelo)", plan_summary))
            facts.append(("Caduca", deadline))
            return (
                _K.PLAN_PENDING,
                self._base(
                    run,
                    headline="Acción requerida: aprobar plan",
                    status=Status.PENDING,
                    summary=(
                        f"Planner y Architect prepararon un plan de {len(tasks) or '?'} tareas. "
                        "Nada se escribe hasta que lo apruebes; si lo rechazas, la ejecución "
                        "termina sin cambios."
                    ),
                    moment=moment,
                    facts=tuple(facts),
                    sections=tuple(sections),
                    next_steps=(
                        "Revisa las tareas, sus dependencias y los riesgos.",
                        "Aprueba para que continúe con el alcance o rechaza indicando el motivo.",
                    ),
                    cli=(*approve_cli, reject_hint),
                    links=self._run_links(run),
                    details=(
                        ("Ejecución", str(run.run_id)),
                        ("Revisión del plan", str(plan.get("revision", ordinal))),
                    ),
                    notes=(MODEL_TEXT_NOTE,) if (plan_summary or risks or issues) else (),
                ),
                ordinal,
            )
        if stage == "scope":
            files_raw = ctx.get("files") or []
            files = [
                f"{clean_text(f.get('action'), 8)}  {clean_text(f.get('path'), 200)}"
                for f in files_raw
                if isinstance(f, dict)
            ][:16]
            tests = clean_lines(ctx.get("tests"), max_items=8)
            risks = clean_lines(ctx.get("risks"), max_items=8)
            summary = clean_text(ctx.get("summary"), 300)
            digest = clean_text(payload.get("scope_digest"), 64)
            facts = [("Etapa", "alcance")]
            if ctx.get("repository"):
                facts.append(("Repositorio", clean_text(ctx.get("repository"), 120)))
            if ctx.get("base_sha"):
                facts.append(("Base", short_sha(ctx.get("base_sha"))))
            facts.append(("Archivos admitidos", str(len(files)) if files else "ver propuesta"))
            if ctx.get("rejected"):
                facts.append(("Descartados por la política", str(ctx.get("rejected"))))
            if summary:
                facts.append(("Resumen del Scoper (del modelo)", summary))
            facts.append(("Caduca", deadline))
            sections = []
            if files:
                sections.append(Section("Archivos que Developer podrá escribir", tuple(files)))
            if tests:
                sections.append(Section("Pruebas que QA ejecutará", tuple(tests)))
            if risks:
                sections.append(Section("Riesgos señalados por el Scoper", tuple(risks)))
            details = [("Ejecución", str(run.run_id))]
            if digest:
                details.append(("Digest del alcance (8 primeros)", digest[:8]))
            if ctx.get("hidden_paths"):
                details.append(("Rutas ocultas al modelo", str(ctx.get("hidden_paths"))))
            if ctx.get("signals"):
                details.append(("Señales de posible inyección", str(ctx.get("signals"))))
            return (
                _K.SCOPE_PENDING,
                self._base(
                    run,
                    headline="Acción requerida: aprobar alcance",
                    status=Status.PENDING,
                    summary=(
                        "El Scoper propone la lista exacta de archivos que Developer podrá "
                        "escribir. Nada se modifica hasta que apruebes ese alcance por su digest."
                    ),
                    moment=moment,
                    facts=tuple(facts),
                    sections=tuple(sections),
                    next_steps=(
                        "Comprueba que los archivos y las pruebas son los que esperas.",
                        "Aprueba escribiendo los 8 primeros caracteres del digest mostrado, o "
                        "rechaza con un motivo.",
                    ),
                    cli=(*approve_cli, reject_hint),
                    links=self._run_links(run),
                    details=tuple(details),
                    notes=(MODEL_TEXT_NOTE,) if (summary or risks) else (),
                ),
                ordinal,
            )
        if stage == "publish":
            files = [
                f"{clean_text(f.get('change'), 12)}  {clean_text(f.get('path'), 200)}"
                for f in (ctx.get("files") or [])
                if isinstance(f, dict)
            ][:32]
            summary = clean_text(ctx.get("summary"), 300)
            digest = clean_text(payload.get("publish_digest"), 64)
            facts = [("Etapa", "publicación")]
            if ctx.get("repository"):
                facts.append(("Repositorio", clean_text(ctx.get("repository"), 120)))
            if ctx.get("remote_branch"):
                facts.append(("Rama que se crearía", clean_text(ctx.get("remote_branch"), 200)))
            if ctx.get("base_branch") or ctx.get("base_sha"):
                facts.append(
                    (
                        "Base",
                        (
                            f"{clean_text(ctx.get('base_branch'), 64)} @ "
                            f"{short_sha(ctx.get('base_sha'))}"
                        ).strip(),
                    )
                )
            if ctx.get("head_sha"):
                facts.append(("Commit a publicar", short_sha(ctx.get("head_sha"))))
            facts.append(("Resultado de QA", _checks(ctx.get("qa_checks"))))
            facts.append(("Hallazgos del escaneo", str(ctx.get("scan_findings", 0))))
            if summary:
                facts.append(("Resumen de Developer (del modelo)", summary))
            facts.append(("Caduca", deadline))
            details = [("Ejecución", str(run.run_id))]
            if digest:
                details.append(("Digest de publicación (8 primeros)", digest[:8]))
            return (
                _K.PUBLISH_PENDING,
                self._base(
                    run,
                    headline="Acción requerida: aprobar publicación",
                    status=Status.PENDING,
                    summary=(
                        "QA aprobó los cambios. Si apruebas, AgentForge empuja exactamente ese "
                        "commit a una rama propia y abre un Pull Request en borrador; nunca "
                        "fusiona. Hasta entonces nada sale del equipo."
                    ),
                    moment=moment,
                    facts=tuple(facts),
                    sections=(Section(f"Archivos del Pull Request ({len(files)})", tuple(files)),)
                    if files
                    else (),
                    next_steps=(
                        "Revisa los archivos y el resultado de QA.",
                        "Aprueba con el digest de publicación o rechaza con un motivo.",
                    ),
                    cli=(*approve_cli, reject_hint),
                    links=self._run_links(run),
                    details=tuple(details),
                    notes=(MODEL_TEXT_NOTE,) if summary else (),
                ),
                ordinal,
            )
        return None

    def _decision(
        self, run: RunContext, payload: dict[str, Any], ordinal: int, moment: datetime
    ) -> tuple[NotificationKind, Notification, int] | None:
        if "approved" not in payload:
            return None  # an incomplete event (never written by the platform): say nothing
        stage = str(payload.get("stage") or "plan")
        approved = bool(payload.get("approved", False))
        who = clean_text(
            payload.get("approved_by") or payload.get("rejected_by") or payload.get("decided_by"),
            64,
        )
        reason = clean_text(payload.get("reason"), 500)
        if approved:
            if stage == "scope":
                return (
                    _K.DEVELOPER_STARTED,
                    self._base(
                        run,
                        headline="Desarrollo iniciado",
                        status=Status.WORKING,
                        summary=(
                            f"{who or 'Una persona'} aprobó el alcance. Developer escribe solo en "
                            "los archivos aprobados y QA valida en el sandbox."
                        ),
                        moment=moment,
                        facts=(("Aprobado por", who or "-"),)
                        + ((("Motivo registrado", reason),) if reason else ()),
                        next_steps=(
                            "Recibirás aviso si QA pide correcciones y cuando el plan de "
                            "publicación esté listo para tu aprobación.",
                        ),
                        cli=self._cli_run(run, "show"),
                        links=self._run_links(run),
                    ),
                    0,
                )
            if stage == "plan":
                return (
                    _K.PLAN_APPROVED,
                    self._base(
                        run,
                        headline="Plan aprobado",
                        status=Status.WORKING,
                        summary=(
                            f"{who or 'Una persona'} aprobó el plan. El siguiente paso es el "
                            "alcance de archivos, que también requerirá tu aprobación."
                        ),
                        moment=moment,
                        facts=(("Aprobado por", who or "-"),)
                        + ((("Motivo registrado", reason),) if reason else ()),
                    ),
                    0,
                )
            return (
                _K.PUBLISH_APPROVED,
                self._base(
                    run,
                    headline="Publicación aprobada",
                    status=Status.WORKING,
                    summary=(
                        f"{who or 'Una persona'} aprobó la publicación. AgentForge empuja la rama "
                        "y abre el Pull Request en borrador; recibirás el enlace."
                    ),
                    moment=moment,
                    facts=(("Aprobado por", who or "-"),)
                    + ((("Motivo registrado", reason),) if reason else ()),
                ),
                0,
            )
        consequences = {
            "plan": (
                "No se creó ninguna tarea ni se escribió nada.",
                "La ejecución termina como rechazada; el checkpoint y la auditoría se conservan.",
            ),
            "scope": (
                "Developer no escribió nada: el worktree queda sin cambios.",
                "La ejecución termina como rechazada; el checkpoint y la auditoría se conservan.",
            ),
            "publish": (
                "Nada salió del equipo: ni rama remota ni Pull Request.",
                "El trabajo de Developer y QA se conserva en el worktree local durante su "
                "retención; la ejecución termina como rechazada.",
            ),
        }
        headline = {
            "plan": "Plan rechazado",
            "scope": "Alcance rechazado",
            "publish": "Publicación rechazada",
        }.get(stage, "Decisión rechazada")
        return (
            _K.RUN_REJECTED,
            self._base(
                run,
                headline=headline,
                status=Status.REJECTED,
                summary=(
                    f"{who or 'Una persona'} rechazó "
                    + ("la publicación" if stage == "publish" else "el " + _stage(stage))
                    + ". Es una decisión humana registrada, no un fallo de la plataforma."
                ),
                moment=moment,
                facts=(
                    ("Etapa de la decisión", _stage(stage)),
                    ("Decidido por", who or "-"),
                    ("Motivo registrado", reason or NO_REASON),
                ),
                sections=(Section("Consecuencias", consequences.get(stage, ())),),
                next_steps=(
                    "Si el trabajo sigue haciendo falta, crea una nueva solicitud con el ajuste "
                    "que motivó el rechazo.",
                ),
                cli=self._cli_run(run, "show", "events"),
                links=self._run_links(run),
                details=(("Ejecución", str(run.run_id)),),
            ),
            ordinal,
        )

    def _qa(
        self,
        run: RunContext,
        payload: dict[str, Any],
        ctx: dict[str, Any],
        ordinal: int,
        moment: datetime,
    ) -> tuple[NotificationKind, Notification, int]:
        checks = _checks(ctx.get("checks"))
        summary = clean_text(ctx.get("summary"), 300)
        if payload.get("approved"):
            return (
                _K.QA_APPROVED,
                self._base(
                    run,
                    headline="QA aprobó los cambios",
                    status=Status.WORKING,
                    summary=(
                        "Las comprobaciones técnicas pasaron y QA no dejó observaciones abiertas."
                    ),
                    moment=moment,
                    facts=(("Comprobaciones", checks),)
                    + ((("Resumen de QA (del modelo)", summary),) if summary else ()),
                    next_steps=(
                        "Si el repositorio tiene remoto, la plataforma prepara el plan de "
                        "publicación y te pedirá aprobarlo; si no, la ejecución termina "
                        "completada.",
                    ),
                    notes=(MODEL_TEXT_NOTE,) if summary else (),
                ),
                ordinal,
            )
        issues = [
            f"{clean_text(i.get('code'), 80)}"
            + (f" — {clean_text(i.get('path'), 200)}" if i.get("path") else "")
            for i in (ctx.get("issues") or [])
            if isinstance(i, dict)
        ][:6]
        return (
            _K.QA_CHANGES_REQUESTED,
            self._base(
                run,
                headline=f"QA necesita correcciones (ronda {ordinal + 1})",
                status=Status.WORKING,
                summary=(
                    f"QA no aprobó: {payload.get('failed_checks', 0)} comprobaciones fallidas y "
                    f"{payload.get('issues', 0)} observaciones. Developer corrige y QA vuelve a "
                    "validar, dentro del límite de rondas del proyecto."
                ),
                moment=moment,
                facts=(("Comprobaciones", checks),)
                + ((("Resumen de QA (del modelo)", summary),) if summary else ()),
                sections=(Section("Observaciones de QA", tuple(issues)),) if issues else (),
                next_steps=(
                    "No hace falta que hagas nada ahora.",
                    "Si se agotan las rondas, la ejecución terminará con «correction_limit» y "
                    "recibirás un aviso.",
                ),
                notes=(MODEL_TEXT_NOTE,) if (summary or issues) else (),
            ),
            ordinal,
        )

    def _pull_request(
        self, run: RunContext, payload: dict[str, Any], moment: datetime
    ) -> tuple[NotificationKind, Notification, int]:
        publication = run.publication or {}
        number = payload.get("pr_number") or publication.get("pr_number")
        url = github_url(payload.get("pr_url") or publication.get("pr_url"))
        branch = clean_text(payload.get("remote_branch") or publication.get("remote_branch"), 200)
        repository = ""
        if url:
            parts = url.split("github.com/", 1)[1].split("/")
            if len(parts) >= 2:
                repository = f"{parts[0]}/{parts[1]}"
        who = clean_text(payload.get("approved_by") or publication.get("approved_by"), 64)
        facts: list[tuple[str, str]] = []
        if repository:
            facts.append(("Repositorio", repository))
        if branch:
            facts.append(("Rama", branch))
        if publication.get("head_sha"):
            facts.append(("Commit", short_sha(publication.get("head_sha"))))
        if who:
            facts.append(("Publicación aprobada por", who))
        links = self._run_links(
            run, *((Link(f"Abrir el Pull Request #{number} en GitHub", url),) if url else ())
        )
        return (
            _K.PULL_REQUEST_OPENED,
            self._base(
                run,
                headline=f"PR #{number} creado" if number else "Pull Request creado",
                status=Status.COMPLETED,
                summary=(
                    "AgentForge empujó la rama de la ejecución y abrió el Pull Request en "
                    "borrador. No fusiona nada: la revisión y la fusión son tuyas."
                ),
                moment=moment,
                facts=tuple(facts),
                next_steps=(
                    "Revisa el Pull Request en GitHub.",
                    "Si forma parte de un objetivo, la siguiente tarea empezará solo cuando lo "
                    "fusiones y la plataforma lo verifique.",
                ),
                links=links,
                details=(("Ejecución", str(run.run_id)),),
            ),
            0,
        )

    def _failed(
        self, run: RunContext, payload: dict[str, Any], ctx: dict[str, Any], moment: datetime
    ) -> tuple[NotificationKind, Notification, int]:
        code = clean_text(payload.get("code") or run.error_code, 64) or "unknown"
        node = clean_text(payload.get("node"), 32)
        agent = clean_text(payload.get("executed_by"), 48)
        message = clean_text(ctx.get("message"), 300)
        preserved = (
            "El checkpoint, los eventos y las invocaciones de herramientas se conservan para "
            "auditoría."
        )
        if run.workflow_kind == "development":
            preserved += (
                " El worktree de la ejecución se mantiene durante su retención; no se publicó nada."
            )
        facts: list[tuple[str, str]] = [("Código", code), ("Qué significa", explain(code))]
        if node:
            facts.append(("Dónde ocurrió", f"nodo {node}" + (f" ({agent})" if agent else "")))
        return (
            _K.RUN_FAILED,
            self._base(
                run,
                headline="Ejecución fallida",
                status=Status.FAILED,
                summary=(
                    "La ejecución terminó con un error registrado por la plataforma. No es una "
                    "decisión humana."
                ),
                moment=moment,
                facts=tuple(facts),
                sections=(
                    Section(
                        "Consecuencias",
                        (
                            "La ejecución no continúa sola: las ejecuciones de desarrollo nunca se "
                            "reconducen automáticamente.",
                            preserved,
                        ),
                    ),
                ),
                next_steps=(
                    "Revisa los eventos y, si procede, crea una nueva solicitud.",
                    "Si la ejecución pertenece a un objetivo, el objetivo queda bloqueado hasta "
                    "que decidas.",
                ),
                cli=self._cli_run(run, "events"),
                links=self._run_links(run),
                details=((("Mensaje de la plataforma", message),) if message else ())
                + (("Ejecución", str(run.run_id)),),
            ),
            0,
        )

    # -- goals ------------------------------------------------------------------------------

    def draft_goal(
        self,
        goal: GoalContext,
        *,
        kind: GoalEventKind,
        payload: dict[str, Any],
        ordinal: int,
        moment: datetime,
        context: dict[str, Any] | None = None,
    ) -> NotificationDraft | None:
        built = self._goal_notification(goal, kind, payload or {}, context or {}, ordinal, moment)
        if built is None:
            return None
        nkind, notification = built
        return _draft(nkind, ordinal, notification)

    def _goal_base(
        self,
        goal: GoalContext,
        *,
        headline: str,
        status: Status,
        summary: str,
        moment: datetime,
        **extra: Any,
    ) -> Notification:
        facts = tuple(extra.pop("facts", ()))
        return Notification(
            headline=headline,
            project=clean_text(goal.project_id, 64),
            status=status,
            summary=summary,
            subject_label="Objetivo",
            subject_id=str(goal.goal_id),
            moment=moment,
            task=clean_text(goal.objective, 220) or None,
            facts=(("Repositorio", clean_text(goal.repository_id, 64)),) + facts,
            **extra,
        )

    def _cli_goal(self, goal: GoalContext) -> tuple[str, ...]:
        return (f"uv run python -m scripts.agentforge goal-show {goal.goal_id}",)

    def _goal_links(self, goal: GoalContext, *extra: Link) -> tuple[Link, ...]:
        links = list(extra)
        url = self._links.goal_url(goal.goal_id)
        if url:
            links.append(Link("Ver el objetivo en AgentForge", url))
        return tuple(links)

    def _goal_notification(
        self,
        goal: GoalContext,
        kind: GoalEventKind,
        payload: dict[str, Any],
        ctx: dict[str, Any],
        ordinal: int,
        moment: datetime,
    ) -> tuple[NotificationKind, Notification] | None:
        G = GoalEventKind
        task = clean_text(payload.get("task_key"), 32)
        run_id = clean_text(payload.get("run_id"), 64)
        run_cli = (f"uv run python -m scripts.agentforge show {run_id}",) if run_id else ()
        if kind is G.GOAL_CREATED:
            mode = clean_text(payload.get("decomposition"), 16)
            return (
                _K.GOAL_CREATED,
                self._goal_base(
                    goal,
                    headline="Objetivo creado",
                    status=Status.WORKING,
                    summary="El Supervisor recibió el objetivo y empieza a coordinarlo.",
                    moment=moment,
                    facts=(("Descomposición", mode or "-"),),
                    next_steps=(
                        "Con «plan», Planner y Architect proponen las tareas y tú apruebas el "
                        "plan.",
                        "Cada tarea será una ejecución normal con sus propias aprobaciones.",
                    ),
                    cli=self._cli_goal(goal),
                    links=self._goal_links(goal),
                ),
            )
        if kind is G.PLAN_REQUESTED:
            return (
                _K.GOAL_PLAN_REQUESTED,
                self._goal_base(
                    goal,
                    headline="Planificación del objetivo solicitada",
                    status=Status.WORKING,
                    summary=(
                        "Planner y Architect preparan el plan del objetivo; después recibirás la "
                        "petición de aprobarlo."
                    ),
                    moment=moment,
                    facts=(("Ejecución de planificación", short_id(run_id)),) if run_id else (),
                    cli=self._cli_goal(goal),
                ),
            )
        if kind is G.TASK_READY:
            return (
                _K.GOAL_TASK_READY,
                self._goal_base(
                    goal,
                    headline=f"Tarea lista: {task}",
                    status=Status.WORKING,
                    summary=(
                        "Sus dependencias están fusionadas y verificadas; la plataforma creará su "
                        "ejecución cuando haya capacidad y presupuesto."
                    ),
                    moment=moment,
                    facts=(("Dependencias fusionadas", str(payload.get("dependencies", 0))),),
                ),
            )
        if kind is G.CHILD_CREATED:
            return (
                _K.GOAL_TASK_STARTED,
                self._goal_base(
                    goal,
                    headline=f"Tarea iniciada: {task}",
                    status=Status.WORKING,
                    summary=(
                        "Se creó la ejecución de la tarea. Sus decisiones de alcance y publicación "
                        "siguen siendo tuyas; llegarán como avisos de esa ejecución."
                    ),
                    moment=moment,
                    facts=(
                        ("Ejecución", short_id(run_id)),
                        ("Intento", str(payload.get("attempt", 1))),
                    ),
                    cli=run_cli + self._cli_goal(goal),
                ),
            )
        if kind is G.TASK_PUBLISHED:
            number = payload.get("pr_number")
            url = github_url(payload.get("pr_url"))
            dependents = int(payload.get("dependents", 0) or 0)
            return (
                _K.GOAL_TASK_PUBLISHED,
                self._goal_base(
                    goal,
                    headline=f"Tarea publicada: {task}" + (f" (PR #{number})" if number else ""),
                    status=Status.COMPLETED,
                    summary=(
                        "La ejecución de la tarea terminó y su Pull Request en borrador existe."
                        + (
                            " Las tareas que dependen de ella no empezarán hasta que lo fusiones "
                            "y la plataforma lo verifique."
                            if dependents
                            else ""
                        )
                    ),
                    moment=moment,
                    facts=(("Tareas dependientes", str(dependents)),),
                    links=self._goal_links(
                        goal,
                        *(
                            (Link(f"Abrir el Pull Request #{number} en GitHub", url),)
                            if url
                            else ()
                        ),
                    ),
                    cli=self._cli_goal(goal),
                ),
            )
        if kind is G.DEPENDENCY_MERGED:
            return (
                _K.GOAL_DEPENDENCY_MERGED,
                self._goal_base(
                    goal,
                    headline=f"Dependencia fusionada: {task}",
                    status=Status.COMPLETED,
                    summary=(
                        "GitHub declara fusionado el Pull Request y el espejo contiene el "
                        "commit de fusión; las tareas dependientes pueden empezar."
                    ),
                    moment=moment,
                    facts=(
                        ("PR", f"#{payload.get('pr_number')}" if payload.get("pr_number") else "-"),
                        ("Commit de fusión", short_sha(payload.get("merge_commit_sha"))),
                    ),
                ),
            )
        if kind is G.NEEDS_HUMAN:
            return self._needs_human(goal, payload, task, run_id, moment)
        terminal = {
            G.GOAL_COMPLETED: (_K.GOAL_COMPLETED, Status.COMPLETED),
            G.GOAL_PARTIAL: (_K.GOAL_PARTIAL, Status.FAILED),
            G.GOAL_FAILED: (_K.GOAL_FAILED, Status.FAILED),
            G.GOAL_CANCELLED: (_K.GOAL_CANCELLED, Status.REJECTED),
            G.GOAL_EXPIRED: (_K.GOAL_EXPIRED, Status.FAILED),
        }.get(kind)
        if terminal is None:
            return None
        nkind, status = terminal
        return nkind, self._goal_end(goal, kind, status, payload, ctx, moment)

    def _needs_human(
        self,
        goal: GoalContext,
        payload: dict[str, Any],
        task: str,
        run_id: str,
        moment: datetime,
    ) -> tuple[NotificationKind, Notification]:
        reason = clean_text(payload.get("reason"), 32)
        stage = _stage(payload.get("stage")) if payload.get("stage") else ""
        number = payload.get("pr_number")
        url = github_url(payload.get("pr_url"))
        code = clean_text(payload.get("code"), 64)
        human_reason = clean_text(
            payload.get("human_reason") or payload.get("decision_reason"), 500
        )
        deadline = _deadline(payload.get("deadline")) if payload.get("deadline") else ""
        facts: list[tuple[str, str]] = []
        if task:
            facts.append(("Tarea", task))
        if run_id:
            facts.append(("Ejecución", short_id(run_id)))
        if stage:
            facts.append(("Etapa pendiente", stage))
        if deadline:
            facts.append(("Caduca", deadline))
        if number:
            facts.append(("Pull Request", f"#{number}"))
        cli: tuple[str, ...] = self._cli_goal(goal)
        steps: tuple[str, ...]
        links = self._goal_links(
            goal, *((Link(f"Abrir el Pull Request #{number} en GitHub", url),) if url else ())
        )
        if reason == "plan_pending":
            headline = "Acción requerida: aprobar el plan del objetivo"
            summary = (
                "El plan del objetivo lleva esperando tu decisión más de la mitad de su ventana. "
                "Nada avanza hasta que decidas."
            )
            steps = ("Aprueba o rechaza el plan desde la ejecución de planificación.",)
            if run_id:
                cli = (
                    f"uv run python -m scripts.agentforge pending {run_id}",
                    f"uv run python -m scripts.agentforge approve {run_id}",
                ) + cli
        elif reason == "decision_pending":
            headline = f"Acción requerida: decidir {stage or 'la etapa pendiente'}" + (
                f" de {task}" if task else ""
            )
            summary = (
                "Una ejecución del objetivo espera tu decisión desde hace más de la mitad de su "
                "ventana. Si caduca, la tarea falla y el objetivo se bloquea."
            )
            steps = ("Revisa la propuesta y decide; la aprobación se liga al digest mostrado.",)
            if run_id:
                cli = (
                    f"uv run python -m scripts.agentforge pending {run_id}",
                    f"uv run python -m scripts.agentforge approve {run_id}",
                ) + cli
        elif reason == "merge_pending":
            headline = (
                f"Acción requerida: fusionar el PR #{number}"
                if number
                else "Acción requerida: fusionar el Pull Request"
            )
            summary = (
                f"La tarea {task or 'publicada'} tiene su Pull Request abierto desde hace más "
                "de la ventana de recordatorio. Las tareas que dependen de ella no empiezan "
                "hasta que lo fusiones y la plataforma lo verifique en GitHub y en el espejo."
            )
            steps = (
                "Revisa y fusiona el Pull Request en GitHub (cualquier modalidad de fusión sirve).",
                "Si ya no procede, cancela el objetivo.",
            )
        elif reason == "merge_unverified":
            headline = "Fusión declarada por GitHub pero ausente en el espejo"
            summary = (
                "GitHub indica que el Pull Request está fusionado, pero el espejo local todavía no "
                "contiene el commit de fusión. La plataforma volverá a comprobarlo; si persiste, "
                "revisa el remoto."
            )
            steps = ("Comprueba la rama base en GitHub y el estado del espejo.",)
        elif reason == "task_failed":
            headline = f"La tarea {task} falló" if task else "Una tarea del objetivo falló"
            summary = (
                "La ejecución de la tarea terminó sin éxito. No se vuelve a crear sola: el "
                "objetivo queda bloqueado hasta que decidas."
            )
            facts.append(("Código", code or "-"))
            facts.append(("Qué significa", explain(code)))
            if human_reason:
                facts.append(("Motivo registrado", human_reason))
            steps = (
                "Revisa los eventos de la ejecución.",
                "Si el trabajo sigue haciendo falta, crea un objetivo o una tarea nueva; si no, "
                "cancela el objetivo.",
            )
            if run_id:
                cli = (f"uv run python -m scripts.agentforge events {run_id}",) + cli
        elif reason == "pr_closed":
            headline = (
                f"PR #{number} cerrado sin fusionar"
                if number
                else "Pull Request cerrado sin fusionar"
            )
            summary = (
                "El Pull Request de la tarea se cerró sin fusionarse, así que las tareas que "
                "dependían de ella se omiten. El objetivo terminará como parcial o fallido."
            )
            steps = ("Si el trabajo sigue haciendo falta, crea un objetivo nuevo.",)
        elif reason == "budget_exhausted":
            headline = "Presupuesto del objetivo agotado"
            summary = (
                "No queda presupuesto para crear más tareas. Las que faltan no se ejecutarán y el "
                "objetivo terminará con lo conseguido."
            )
            facts.append(("Código", code or "-"))
            steps = ("Si hace falta continuar, crea un objetivo nuevo con un presupuesto mayor.",)
        else:
            headline = "El objetivo necesita una decisión humana"
            summary = "La plataforma detectó una situación que requiere tu intervención."
            facts.append(("Motivo", reason or "-"))
            steps = ("Revisa el objetivo.",)
        return (
            _K.GOAL_NEEDS_HUMAN,
            self._goal_base(
                goal,
                headline=headline,
                status=Status.PENDING,
                summary=summary,
                moment=moment,
                facts=tuple(facts),
                next_steps=steps,
                cli=cli,
                links=links,
                details=(("Motivo interno", reason or "-"),),
            ),
        )

    def _goal_end(
        self,
        goal: GoalContext,
        kind: GoalEventKind,
        status: Status,
        payload: dict[str, Any],
        ctx: dict[str, Any],
        moment: datetime,
    ) -> Notification:
        G = GoalEventKind
        code = clean_text(payload.get("code"), 64)
        who = clean_text(payload.get("decided_by"), 64)
        reason = clean_text(payload.get("reason"), 500)
        total = payload.get("tasks", 0)
        succeeded = payload.get("succeeded", 0)
        failed = payload.get("failed", 0)
        roster = []
        for t in ctx.get("tasks") or []:
            if not isinstance(t, dict):
                continue
            line = f"{clean_text(t.get('task_key'), 32)}: {clean_text(t.get('status'), 16)}"
            if t.get("pr_number"):
                line += f" · PR #{t.get('pr_number')}" + (
                    " fusionado" if t.get("merged") else " sin fusionar"
                )
            if t.get("code"):
                line += f" · {clean_text(t.get('code'), 64)}"
            roster.append(line)
        usage = ctx.get("usage") if isinstance(ctx.get("usage"), dict) else {}
        facts: list[tuple[str, str]] = [
            ("Tareas", f"{succeeded} con éxito, {failed} fallidas u omitidas, {total} en total")
        ]
        if kind is G.GOAL_FAILED and code.startswith("plan_"):
            outcome = code.split("_", 1)[1]
            label = {
                "rejected:plan": "plan rechazado",
                "expired:plan": "plan caducado sin decisión",
                "cancelled": "planificación cancelada",
            }.get(outcome, f"planificación no aprobada ({outcome})")
            headline = f"Objetivo cerrado: {label}"
            summary = (
                "Consecuencia de la decisión humana sobre la planificación: el objetivo cierra "
                "sin crear ninguna tarea ni ejecutar nada más. Solo se consumieron las llamadas "
                "de Planner y Architect."
            )
            facts.append(("Decidido por", who or "-"))
            facts.append(("Motivo registrado", reason or NO_REASON))
        elif kind is G.GOAL_COMPLETED:
            headline = "Objetivo completado"
            summary = (
                "Todas las tareas terminaron: las que tenían dependientes están fusionadas y "
                "verificadas, y las últimas tienen su Pull Request en borrador listo para revisar."
            )
        elif kind is G.GOAL_PARTIAL:
            headline = "Objetivo terminado parcialmente"
            summary = (
                "Algunas tareas se publicaron o fusionaron y otras fallaron o se omitieron por "
                "depender de una fallida. Lo conseguido se conserva en sus Pull Requests."
            )
            if code:
                facts.append(("Primer código de fallo", code))
                facts.append(("Qué significa", explain(code)))
        elif kind is G.GOAL_FAILED:
            headline = "Objetivo fallido"
            summary = "Ninguna tarea llegó a publicarse."
            if code:
                facts.append(("Código", code))
                facts.append(("Qué significa", explain(code)))
            if reason:
                facts.append(("Motivo registrado", reason))
        elif kind is G.GOAL_CANCELLED:
            headline = "Objetivo cancelado"
            summary = (
                f"{who or 'Una persona'} canceló el objetivo. Las ejecuciones que esperaban una "
                "decisión se cancelaron; las que estaban en curso terminan por su cuenta y nada "
                "nuevo se crea."
            )
            facts.append(("Cancelado por", who or "-"))
        else:
            headline = "Objetivo caducado"
            summary = (
                "Se venció el plazo del objetivo. No se crean más tareas; las ejecuciones que "
                "esperaban una decisión se cancelaron."
            )
        if usage:
            facts.append(("Llamadas al modelo usadas", _fmt_number(usage.get("llm_calls_used", 0))))
            facts.append(
                (
                    "Reserva máxima (no es el coste real)",
                    f"{_fmt_number(usage.get('reserved_model_calls', 0))} llamadas · "
                    f"{_fmt_number(usage.get('reserved_usd', 0))} USD",
                )
            )
        sections = (Section("Tareas", tuple(roster)),) if roster else ()
        steps = (
            ("Revisa y fusiona los Pull Requests pendientes en GitHub.",)
            if kind is G.GOAL_COMPLETED
            else (
                "Si el trabajo sigue haciendo falta, crea un objetivo nuevo con el ajuste "
                "necesario.",
            )
        )
        return self._goal_base(
            goal,
            headline=headline,
            status=status,
            summary=summary,
            moment=moment,
            facts=tuple(facts),
            sections=sections,
            next_steps=steps,
            cli=self._cli_goal(goal),
            links=self._goal_links(goal),
            details=(("Objetivo", str(goal.goal_id)),) + ((("Código", code),) if code else ()),
        )


def _draft(kind: NotificationKind, ordinal: int, notification: Notification) -> NotificationDraft:
    return NotificationDraft(
        kind=kind,
        ordinal=ordinal,
        subject=render_subject(notification),
        text=render_text(notification),
        html=render_html(notification),
        headline=notification.headline,
    )

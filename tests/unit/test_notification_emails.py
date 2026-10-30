"""The redesigned mails (text and HTML) over the synthetic fixtures of every kind: clear
subjects without hashes, truthful content per event, escaping, redaction, links only to
safe destinations, plain-text parity, missing reasons, legacy events and UTF-8."""

import re
from datetime import UTC, datetime
from email.message import EmailMessage as MimeMessage
from uuid import uuid4

import pytest

from app.contracts.goals import GoalEventKind
from app.contracts.lifecycle import RunEventKind
from app.notifications import (
    NotificationEnqueuer,
    NotificationKind,
    NotificationPolicy,
    PublicLinks,
    validate_public_app_url,
)
from app.notifications.policy import NO_REASON, GoalContext
from app.notifications.samples import (
    GOAL_ID,
    MOMENT,
    PLAN_RUN_ID,
    RUN_ID,
    goal_context,
    goal_samples,
    run_context,
    run_samples,
)
from app.notifications.sanitize import clean_text, github_url, safe_https_url
from app.orchestration.ports import RunEvent

LONG_HEX = re.compile(r"[0-9a-f]{40,}")
SCRIPT = "<script>alert('x')</script>"
TOKEN = "ghs_" + "Q" * 36


def all_drafts(policy: NotificationPolicy):  # type: ignore[no-untyped-def]
    for sample in run_samples():
        yield (
            sample.name,
            policy.draft(
                sample.run or run_context(),
                kind=sample.kind,
                payload=sample.payload,
                ordinal=sample.ordinal,
                moment=MOMENT,
                context=sample.context,
            ),
        )
    goal = goal_context()
    for goal_sample in goal_samples():
        yield (
            goal_sample.name,
            policy.draft_goal(
                goal,
                kind=goal_sample.kind,
                payload=goal_sample.payload,
                ordinal=goal_sample.ordinal,
                moment=MOMENT,
                context=goal_sample.context,
            ),
        )


def test_every_sample_renders_a_clear_subject_and_a_complete_text_alternative():
    policy = NotificationPolicy()
    seen: set[str] = set()
    for name, draft in all_drafts(policy):
        assert draft is not None, name
        seen.add(name)
        # Subjects: prefix, headline, project; no long hashes, no run ids.
        assert draft.subject.startswith("[AgentForge] "), name
        assert draft.subject.endswith("— sandbox-github") or "— sandbox-github (" in draft.subject
        assert not LONG_HEX.search(draft.subject) and "de8845f3" not in draft.subject, name
        assert len(draft.subject) <= 200
        # HTML: one 600 px table, inline styles, nothing remote or active.
        html = draft.html
        assert html.startswith("<!doctype html>") and 'lang="es"' in html
        assert "max-width:600px" in html and 'role="presentation"' in html
        for forbidden in ("<script", "<img", "<link", "@import", "http://", "javascript:"):
            assert forbidden not in html, (name, forbidden)
        # Every mail says what it is, what happened and what to do or expect.
        assert draft.headline in draft.text and draft.headline in html
        assert "Estado:" in draft.text and "Proyecto: sandbox-github" in draft.text
        assert "Qué sigue" in draft.text or "Enlaces" in draft.text or "Estado:" in draft.text
        assert draft.text.endswith("ningún enlace aprueba nada por sí mismo.\n")
    assert {
        "plan_pending",
        "scope_pending",
        "publish_pending",
        "run_failed",
        "goal_completed",
    } <= seen


def test_plain_text_carries_every_fact_section_step_and_link_of_the_html():
    policy = NotificationPolicy(links=PublicLinks("https://agentforge.example.test"))
    import html as html_lib

    for name, draft in all_drafts(policy):
        assert draft is not None
        text = draft.text
        # Each table row of the HTML (label/value) appears in the text as "label: value".
        for label, value in re.findall(
            r'white-space:nowrap">([^<]+)</td><td[^>]*>([^<]*)</td>', draft.html
        ):
            label, value = html_lib.unescape(label), html_lib.unescape(value)
            assert f"{label}: {value}" in text, (name, label, value)
        # Each list item of the HTML appears in the text as a bullet.
        for item in re.findall(r'line-height:20px">([^<]+)</li>', draft.html):
            assert f"- {html_lib.unescape(item)}" in text, (name, item)
        # Each link of the HTML appears in the text verbatim.
        for url in re.findall(r'href="([^"]+)"', draft.html):
            assert html_lib.unescape(url) in text, (name, url)


def test_pending_approval_mails_say_stage_proposal_risks_files_deadline_and_action():
    policy = NotificationPolicy()
    by_name = {name: draft for name, draft in all_drafts(policy)}
    plan = by_name["plan_pending"]
    assert plan.subject == "[AgentForge] Acción requerida: aprobar plan — sandbox-github"
    for needle in (
        "Etapa: plan",
        "T1: Crear normalize_email con pruebas",
        "T2: Integrar normalize_email en validate_email (depende de T1)",
        "Riesgos señalados por el Planner",
        "Architect: aprobó el plan",
        f"scripts.agentforge approve {PLAN_RUN_ID}",
        "--reason",
        "Caduca: sin plazo",
    ):
        assert needle in plan.text, needle
    assert "Nota: Los resúmenes y observaciones marcados" in plan.text
    scope = by_name["scope_pending"]
    assert scope.subject == "[AgentForge] Acción requerida: aprobar alcance — sandbox-github"
    for needle in (
        "Etapa: alcance",
        "edit  src/mailcheck/validator.py",
        "create  tests/test_validator.py",
        "Pruebas que QA ejecutará",
        "Caduca: 2026-10-31T18:25:30+00:00",
        "Digest del alcance (8 primeros): 23567a52",
        "Descartados por la política: 1",
        "8 primeros caracteres del digest",
    ):
        assert needle in scope.text, needle
    assert "23567a52e1989a5cc380fe822d9f3b405bdf1cc2cf9ed0f4fc45a51b5b371179" not in scope.text
    publish = by_name["publish_pending"]
    assert publish.subject == "[AgentForge] Acción requerida: aprobar publicación — sandbox-github"
    for needle in (
        "Repositorio: example-org/sandbox-demo",
        "Base: main @ b04ba76475f2",
        "Commit a publicar: e4c101fb4073",
        "Resultado de QA: ruff_check: ok · ruff_format_check: ok · pytest: ok",
        "modified  src/mailcheck/validator.py",
        "nunca fusiona",
    ):
        assert needle in publish.text, needle


def test_developer_qa_and_publication_mails_state_work_tests_and_next_steps():
    by_name = {name: draft for name, draft in all_drafts(NotificationPolicy())}
    qa = by_name["qa_changes_requested"]
    assert qa.subject == "[AgentForge] QA necesita correcciones (ronda 1) — sandbox-github"
    assert "pytest: código 1" in qa.text and "technical_checks_failed" in qa.text
    assert "Developer corrige y QA vuelve a validar" in qa.text
    started = by_name["developer_started"]
    assert started.subject == "[AgentForge] Desarrollo iniciado — sandbox-github"
    assert "Aprobado por: omar" in started.text
    pr = by_name["pull_request_opened"]
    assert pr.subject == "[AgentForge] PR #12 creado — sandbox-github"
    assert "https://github.com/example-org/sandbox-demo/pull/12" in pr.text
    assert "Repositorio: example-org/sandbox-demo" in pr.text and "No fusiona nada" in pr.text
    assert 'href="https://github.com/example-org/sandbox-demo/pull/12"' in pr.html


def test_rejections_name_their_stage_consequences_and_the_recorded_reason_or_its_absence():
    by_name = {name: draft for name, draft in all_drafts(NotificationPolicy())}
    plan = by_name["plan_rejected_with_reason"]
    assert plan.subject == "[AgentForge] Plan rechazado — sandbox-github"
    assert plan.kind is NotificationKind.RUN_REJECTED
    assert "Motivo registrado: El plan recorta espacios" in plan.text
    assert "Decidido por: martin" in plan.text and "No se creó ninguna tarea" in plan.text
    assert "Etapa: fin" not in plan.text  # one stage per mail, named once
    scope = by_name["scope_rejected_without_reason"]
    assert scope.subject == "[AgentForge] Alcance rechazado — sandbox-github"
    assert f"Motivo registrado: {NO_REASON}" in scope.text
    assert "Developer no escribió nada" in scope.text
    publish = by_name["publish_rejected"]
    assert publish.subject == "[AgentForge] Publicación rechazada — sandbox-github"
    assert "ni rama remota ni Pull Request" in publish.text
    assert "se conserva en el worktree" in publish.text
    failed = by_name["run_failed"]
    assert failed.subject == "[AgentForge] Ejecución fallida — sandbox-github"
    assert "Código: correction_limit" in failed.text
    assert "Qué significa: QA no aprobó los cambios tras agotar" in failed.text
    assert "No es una decisión humana" in failed.text
    goal = by_name["goal_failed_plan_rejected"]
    assert goal.subject == "[AgentForge] Objetivo cerrado: plan rechazado — sandbox-github"
    assert "Consecuencia de la decisión humana" in goal.text
    assert "Motivo registrado: El plan contradice" in goal.text and "Objetivo:" in goal.text


def test_goal_outcome_mails_list_tasks_pull_requests_integration_and_reliable_budget():
    by_name = {name: draft for name, draft in all_drafts(NotificationPolicy())}
    done = by_name["goal_completed"]
    assert done.subject == "[AgentForge] Objetivo completado — sandbox-github"
    assert "T1: merged · PR #12 fusionado" in done.text
    assert "T2: published · PR #13 sin fusionar" in done.text
    assert "Llamadas al modelo usadas: 11" in done.text
    assert "Reserva máxima (no es el coste real): 28 llamadas · 20 USD" in done.text
    partial = by_name["goal_partial"]
    assert "T2: skipped" in partial.text and "pr_closed" in partial.text
    merge = by_name["goal_needs_human_merge"]
    assert merge.subject == "[AgentForge] Acción requerida: fusionar el PR #12 — sandbox-github"
    assert "no empiezan hasta que lo fusiones" in merge.text


def test_untrusted_text_is_escaped_bounded_and_redacted_everywhere():
    policy = NotificationPolicy()
    nasty = run_context(task=f"{SCRIPT} usa {TOKEN} y password=hunter2secret {'x' * 3000}")
    draft = policy.draft(
        nasty,
        kind=RunEventKind.APPROVAL_RECORDED,
        payload={
            "approved": False,
            "stage": "scope",
            "rejected_by": "<b>eve</b>",
            "reason": f"{SCRIPT} token={TOKEN}",
        },
        ordinal=1,
        moment=MOMENT,
    )
    assert draft is not None
    for body in (draft.text, draft.html):
        assert TOKEN not in body and "hunter2secret" not in body
        assert "[REDACTED" in body
    assert "<script>" not in draft.html and "&lt;script&gt;" in draft.html
    assert "<b>eve</b>" not in draft.html and "&lt;b&gt;eve&lt;/b&gt;" in draft.html
    assert "x" * 300 not in draft.text  # bounded
    # Model text in a pending-scope mail: escaped, bounded, never a link.
    scope = policy.draft(
        run_context(),
        kind=RunEventKind.WAITING_APPROVAL,
        payload={"stage": "scope", "scope_digest": "a" * 64},
        ordinal=1,
        moment=MOMENT,
        context={
            "stage": "scope",
            "summary": f"{SCRIPT} visita https://evil.example/{TOKEN}",
            "files": [{"action": "edit", "path": "src/<x>.py"}],
            "risks": ["r" * 1000],
        },
    )
    assert scope is not None
    assert "<script>" not in scope.html and 'href="https://evil.example' not in scope.html
    assert "src/&lt;x&gt;.py" in scope.html and "r" * 300 not in scope.text
    assert clean_text("a\x00b\x07c  d\n\ne") == "abc d e"  # control chars dropped, spaces folded


def test_links_only_when_a_public_https_destination_exists_otherwise_cli_instructions():
    for bad in (
        "http://127.0.0.1:8011",
        "http://agentforge.example",
        "https://localhost",
        "https://user:pw@agentforge.example",
        "https://agentforge.example/?token=x",
        "https://agentforge.example/#frag",
        "ftp://agentforge.example",
    ):
        with pytest.raises(ValueError):
            validate_public_app_url(bad)
        assert (
            safe_https_url(bad) is None or "?" in bad or "#" in bad
        )  # query/fragment: strict only
    assert (
        validate_public_app_url("https://agentforge.example/app/")
        == "https://agentforge.example/app"
    )
    legacy = PublicLinks.resolve(None, legacy="http://127.0.0.1:8011")
    assert not legacy.configured and legacy.run_url(RUN_ID) is None
    strict = PublicLinks.resolve("https://agentforge.example.test", legacy="http://127.0.0.1:1")
    assert strict.run_url(RUN_ID) == f"https://agentforge.example.test/api/v1/runs/{RUN_ID}"
    assert strict.goal_url(GOAL_ID) == f"https://agentforge.example.test/api/v1/goals/{GOAL_ID}"
    without = NotificationPolicy(base_url="http://127.0.0.1:8011")
    draft = without.draft(
        run_context(), kind=RunEventKind.RUN_FAILED, payload={"code": "x"}, ordinal=0, moment=MOMENT
    )
    assert draft is not None and "Enlaces:" not in draft.text
    assert f"uv run python -m scripts.agentforge events {RUN_ID}" in draft.text
    with_links = NotificationPolicy(links=strict)
    linked = with_links.draft(
        run_context(), kind=RunEventKind.RUN_FAILED, payload={"code": "x"}, ordinal=0, moment=MOMENT
    )
    assert linked is not None
    assert f"https://agentforge.example.test/api/v1/runs/{RUN_ID}" in linked.text
    assert "token" not in linked.text.lower().split("enlaces:")[1].split("detalles")[0]
    assert github_url("https://github.com/o/r/pull/1") == "https://github.com/o/r/pull/1"
    assert github_url("https://github.com.evil/o/r/pull/1") is None
    assert github_url("http://github.com/o/r/pull/1") is None


def test_legacy_events_without_the_new_fields_still_render():
    policy = NotificationPolicy()
    legacy_run = run_context()  # no plan, no publication, no error, no pending stage
    cases = [
        (RunEventKind.WAITING_APPROVAL, {"stage": "plan"}, "aprobar plan"),
        (
            RunEventKind.WAITING_APPROVAL,
            {"stage": "scope", "scope_digest": "b" * 64},
            "aprobar alcance",
        ),
        (RunEventKind.WAITING_APPROVAL, {"stage": "publish"}, "aprobar publicación"),
        (RunEventKind.APPROVAL_RECORDED, {"approved": False, "stage": "plan"}, "Plan rechazado"),
        (RunEventKind.QA_REVIEWED, {"approved": False}, "QA necesita correcciones"),
        (RunEventKind.RUN_FAILED, {"code": "weird_new_code"}, "código «weird_new_code»"),
        (RunEventKind.PULL_REQUEST_OPENED, {}, "Pull Request creado"),
        (RunEventKind.RUN_EXPIRED, {}, "Decisión caducada"),
    ]
    for kind, payload, expected in cases:
        draft = policy.draft(legacy_run, kind=kind, payload=payload, ordinal=0, moment=MOMENT)
        assert draft is not None and expected in draft.subject + draft.text, (kind, expected)
    # The row's plan fills in for a plan-pending mail rebuilt from history.
    with_plan = run_context(
        plan={"summary": "plan guardado", "tasks": [{"id": "A", "title": "Hacer A"}]}
    )
    draft = policy.draft(
        with_plan,
        kind=RunEventKind.WAITING_APPROVAL,
        payload={"stage": "plan"},
        ordinal=0,
        moment=MOMENT,
    )
    assert draft is not None and "A: Hacer A" in draft.text and "plan guardado" in draft.text
    # Goal events from before the context existed.
    goal = policy.draft_goal(
        goal_context(),
        kind=GoalEventKind.GOAL_COMPLETED,
        payload={"tasks": 1},
        ordinal=0,
        moment=MOMENT,
    )
    assert goal is not None and "Tareas: 0 con éxito" in goal.text


def test_one_mail_per_outcome_and_utf8_safe_messages():
    policy = NotificationPolicy()
    enqueuer = NotificationEnqueuer(policy, recipients=["ops@example.test"], level="priority")
    pr_payload = {"pr_number": 12, "pr_url": "https://github.com/example-org/sandbox-demo/pull/12"}
    events = [
        (1, RunEvent(kind=RunEventKind.PULL_REQUEST_OPENED, payload=pr_payload)),
        (2, RunEvent(kind=RunEventKind.RUN_PUBLISHED, payload={"pr_number": 12})),
        (3, RunEvent(kind=RunEventKind.RUN_COMPLETED, context={"publication_expected": True})),
    ]
    drafts = enqueuer.drafts(run_context(), events, moment=MOMENT)
    assert [d.kind for d in drafts] == [NotificationKind.PULL_REQUEST_OPENED]
    # Verbose adds stages but never a mail per model call: node events only.
    verbose = NotificationEnqueuer(policy, recipients=["ops@example.test"], level="verbose")
    assert len(verbose.drafts(run_context(), events, moment=MOMENT)) == 1
    # Spanish text survives the MIME round trip.
    draft = drafts[0]
    mime = MimeMessage()
    mime["Subject"] = draft.subject
    mime.set_content(draft.text_body)
    mime.add_alternative(draft.html_body, subtype="html")
    raw = mime.as_bytes()
    assert b'Content-Type: text/plain; charset="utf-8"' in raw
    assert "Acción" in draft.text_body or "Pull Request" in draft.text_body
    assert draft.subject.encode("utf-8").decode("utf-8") == draft.subject


def test_goal_context_and_run_context_default_fields_keep_older_callers_working():
    ctx = GoalContext(goal_id=uuid4(), project_id="p", repository_id="r", objective="o")
    assert ctx.objective == "o"
    run = run_context()
    assert run.plan is None and run.publication is None and run.pending_stage is None
    assert MOMENT == datetime(2026, 10, 30, 18, 25, 30, tzinfo=UTC)

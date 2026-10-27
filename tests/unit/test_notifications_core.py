"""Notification policy, templates, worker, outbox and SMTP adapter, with fakes only."""

import smtplib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from app.contracts.lifecycle import RunEventKind
from app.infrastructure.db import InMemoryNotificationOutbox
from app.infrastructure.notifications import FakeEmailSender, SmtpEmailSender
from app.notifications import (
    BACKOFF_SECONDS,
    EmailMessage,
    NotificationEnqueuer,
    NotificationKind,
    NotificationPolicy,
    NotificationWorker,
    OutboxStatus,
    RunContext,
    SendOutcome,
)
from app.orchestration.ports import RunEvent

pytestmark = pytest.mark.anyio
T0 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
RUN = uuid.UUID("de8845f3-bd98-48cc-bdd4-1fe67d55ea75")
CTX = RunContext(
    run_id=RUN, project_id="sandbox-github", task="Implementa validate_email <b>x</b> @bob"
)
SECRET = "ghs_" + "A" * 36


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def event(kind: RunEventKind, payload: dict | None = None, ordinal: int = 0) -> RunEvent:
    return RunEvent(kind=kind, payload=payload or {}, ordinal=ordinal)


def test_policy_maps_each_important_transition_and_ignores_noise():
    policy = NotificationPolicy(base_url="http://127.0.0.1:8011")
    cases = [
        ((RunEventKind.RUN_CREATED, {}), NotificationKind.RUN_CREATED),
        (
            (RunEventKind.ROUTE_SELECTED, {"route": "full", "mode": "auto"}),
            NotificationKind.ROUTE_SELECTED,
        ),
        ((RunEventKind.ROUTE_ESCALATED, {"reason": "x"}), NotificationKind.ROUTE_ESCALATED),
        (
            (RunEventKind.ARCHITECT_REVIEWED, {"approved": True}),
            NotificationKind.PLANNING_COMPLETED,
        ),
        ((RunEventKind.WAITING_APPROVAL, {"stage": "plan"}), NotificationKind.PLAN_PENDING),
        ((RunEventKind.WAITING_APPROVAL, {"stage": "scope"}), NotificationKind.SCOPE_PENDING),
        ((RunEventKind.WAITING_APPROVAL, {"stage": "publish"}), NotificationKind.PUBLISH_PENDING),
        ((RunEventKind.RUN_APPROVED, {"stage": "scope"}), NotificationKind.DEVELOPER_STARTED),
        (
            (RunEventKind.APPROVAL_RECORDED, {"approved": True, "stage": "scope"}),
            NotificationKind.DEVELOPER_STARTED,
        ),
        ((RunEventKind.DEVELOPER_COMPLETED, {"files": 2}), NotificationKind.DEVELOPER_COMPLETED),
        ((RunEventKind.DEVELOPER_APPLIED, {"files": 2}), NotificationKind.QA_STARTED),
        ((RunEventKind.QA_REVIEWED, {"approved": False}), NotificationKind.QA_CHANGES_REQUESTED),
        ((RunEventKind.QA_REVIEWED, {"approved": True}), NotificationKind.QA_APPROVED),
        (
            (RunEventKind.PULL_REQUEST_OPENED, {"pr_number": 7, "pr_url": "https://x/pull/7"}),
            NotificationKind.PULL_REQUEST_OPENED,
        ),
        ((RunEventKind.RUN_COMPLETED, {}), NotificationKind.RUN_COMPLETED),
        ((RunEventKind.RUN_PUBLISHED, {}), NotificationKind.RUN_PUBLISHED),
        ((RunEventKind.RUN_FAILED, {"code": "x"}), NotificationKind.RUN_FAILED),
        ((RunEventKind.RUN_CANCELLED, {}), NotificationKind.RUN_CANCELLED),
        ((RunEventKind.RUN_EXPIRED, {}), NotificationKind.RUN_EXPIRED),
    ]
    for (kind, payload), expected in cases:
        draft = policy.draft(CTX, kind=kind, payload=payload, ordinal=0, moment=T0)
        assert draft is not None and draft.kind is expected, kind
        assert "[AgentForge] sandbox-github · de8845f3 ·" in draft.subject
        for required in ("Proyecto:", "Ejecución:", "Etapa:", "Estado:", "Fecha:", "Tarea:"):
            assert required in draft.text
        assert (
            "http://127.0.0.1:8011/api/v1/runs/de8845f3-bd98-48cc-bdd4-1fe67d55ea75" in draft.text
        )
        assert "<html" in draft.html and "&lt;b&gt;x&lt;/b&gt;" in draft.html  # escaped task
    # Noise: per-node bookkeeping events produce no mail.
    for kind in (
        RunEventKind.APPROVAL_RECORDED,
        RunEventKind.APPROVAL_APPLIED,
        RunEventKind.RUN_RESUMED,
        RunEventKind.MIRROR_REFRESHED,
        RunEventKind.PLANNER_COMPLETED,
        RunEventKind.SCOPE_PROPOSED,
    ):
        assert policy.draft(CTX, kind=kind, payload={}, ordinal=0, moment=T0) is None
    # The publish approval itself is silent: the outcome (PR / published) notifies.
    assert (
        policy.draft(
            CTX, kind=RunEventKind.RUN_APPROVED, payload={"stage": "publish"}, ordinal=0, moment=T0
        )
        is None
    )
    pr = policy.draft(
        CTX,
        kind=RunEventKind.PULL_REQUEST_OPENED,
        payload={"pr_number": 7, "pr_url": "https://x/pull/7"},
        ordinal=0,
        moment=T0,
    )
    assert pr is not None and "https://x/pull/7" in pr.text and "Pull Request #7" in pr.subject


def test_enqueuer_dedups_per_event_and_recipient_and_threads_a_run():
    enqueuer = NotificationEnqueuer(
        NotificationPolicy(),
        recipients=["a@example.com", "b@example.com"],
        domain="agentforge.local",
        level="verbose",
    )
    events = [
        (1, event(RunEventKind.RUN_CREATED)),
        (2, event(RunEventKind.QA_REVIEWED, {"approved": False}, 0)),
        (3, event(RunEventKind.QA_REVIEWED, {"approved": False}, 1)),
        (4, event(RunEventKind.APPROVAL_RECORDED)),
    ]
    drafts = enqueuer.drafts(CTX, events, moment=T0)
    assert len(drafts) == 6  # 3 notifiable events x 2 recipients
    keys = {d.dedup_key for d in drafts}
    assert len(keys) == 6 and all(str(RUN) in k for k in keys)
    assert {d.message_id for d in drafts} == {
        f"<agentforge.{RUN}.run_created.0@agentforge.local>",
        f"<agentforge.{RUN}.qa_changes_requested.0@agentforge.local>",
        f"<agentforge.{RUN}.qa_changes_requested.1@agentforge.local>",
    }
    outbox = InMemoryNotificationOutbox()
    assert outbox.insert(drafts) == 6 and outbox.insert(drafts) == 0  # replay is a no-op
    assert (
        NotificationEnqueuer(NotificationPolicy(), recipients=[]).drafts(CTX, events, moment=T0)
        == []
    )


async def test_worker_sends_retries_with_backoff_and_gives_up():
    clock = Clock()
    outbox = InMemoryNotificationOutbox(clock)
    enqueuer = NotificationEnqueuer(
        NotificationPolicy(), recipients=["ops@example.com"], level="verbose"
    )
    outbox.insert(
        enqueuer.drafts(
            CTX,
            [
                (1, event(RunEventKind.RUN_CREATED)),
                (2, event(RunEventKind.RUN_FAILED, {"code": "x"})),
            ],
            moment=T0,
        )
    )
    sender = FakeEmailSender()
    sender.script(
        SendOutcome(
            ok=False, code="smtp_unreachable", detail="ConnectError", retryable=True
        ),  # item 1 attempt 1
        SendOutcome(ok=True),  # item 2
    )
    worker = NotificationWorker(outbox, sender, clock=clock, max_attempts=3)
    report = await worker.run_once()
    assert (report.claimed, report.sent, report.retried, report.dead) == (2, 1, 1, 0)
    items = {i.kind: i for i in await outbox.list_items()}
    failed = items[NotificationKind.RUN_CREATED]
    assert failed.status is OutboxStatus.FAILED and failed.attempts == 1
    assert failed.next_attempt_at == T0 + timedelta(seconds=BACKOFF_SECONDS[0])
    assert failed.last_error.startswith("smtp_unreachable")
    assert items[NotificationKind.RUN_FAILED].status is OutboxStatus.SENT
    # Not due yet: nothing claimed. After the backoff: retried; attempts exhaust into dead.
    assert (await worker.run_once()).claimed == 0
    clock.now += timedelta(seconds=BACKOFF_SECONDS[0] + 1)
    sender.script(SendOutcome(ok=False, code="smtp_unreachable", retryable=True))
    assert (await worker.run_once()).retried == 1
    clock.now += timedelta(seconds=BACKOFF_SECONDS[1] + 1)
    sender.script(RuntimeError("boom"))  # a crashing sender counts as a retryable failure
    report = await worker.run_once()
    assert report.dead == 1 and "sender_crashed" in report.errors[0]
    dead = (await outbox.list_items(statuses=[OutboxStatus.DEAD]))[0]
    assert dead.attempts == 3 and dead.kind is NotificationKind.RUN_CREATED
    # Operator requeue brings it back once credentials/SMTP are fixed.
    assert await outbox.requeue(dead.id, now=clock.now)
    assert (await worker.run_once()).sent == 1
    assert await outbox.counts() == {"sent": 2}


async def test_non_retryable_errors_die_immediately_and_uncertain_answers_retry_same_message_id():
    clock = Clock()
    outbox = InMemoryNotificationOutbox(clock)
    enqueuer = NotificationEnqueuer(
        NotificationPolicy(), recipients=["ops@example.com"], level="verbose"
    )
    outbox.insert(
        enqueuer.drafts(
            CTX,
            [(1, event(RunEventKind.RUN_CREATED)), (2, event(RunEventKind.RUN_COMPLETED))],
            moment=T0,
        )
    )
    sender = FakeEmailSender()
    sender.script(
        SendOutcome(ok=False, code="smtp_auth_failed", detail="autenticación rechazada (535)"),
        SendOutcome(ok=False, code="smtp_disconnected", retryable=True, uncertain=True),
    )
    worker = NotificationWorker(outbox, sender, clock=clock)
    report = await worker.run_once()
    assert report.dead == 1 and report.retried == 1
    by_kind = {i.kind: i for i in await outbox.list_items()}
    assert (
        by_kind[NotificationKind.RUN_CREATED].status is OutboxStatus.DEAD
    )  # bad credentials: no retries
    uncertain = by_kind[NotificationKind.RUN_COMPLETED]
    assert uncertain.status is OutboxStatus.FAILED
    first_id = sender.sent[0].message_id  # the fake kept the uncertain message, like a server might
    clock.now += timedelta(seconds=BACKOFF_SECONDS[0] + 1)
    await worker.run_once()
    assert [m.message_id for m in sender.sent] == [
        first_id,
        first_id,
    ]  # a duplicate the client can fold


async def test_a_stale_sending_lease_is_reclaimed_after_a_worker_crash():
    clock = Clock()
    outbox = InMemoryNotificationOutbox(clock)
    enqueuer = NotificationEnqueuer(
        NotificationPolicy(), recipients=["ops@example.com"], level="verbose"
    )
    outbox.insert(enqueuer.drafts(CTX, [(1, event(RunEventKind.RUN_CREATED))], moment=T0))
    claimed = await outbox.claim(now=clock.now, limit=10, lease_seconds=120)
    assert len(claimed) == 1 and claimed[0].status is OutboxStatus.SENDING  # worker died here
    assert (
        await outbox.claim(now=clock.now + timedelta(seconds=60), limit=10, lease_seconds=120) == []
    )
    again = await outbox.claim(now=clock.now + timedelta(seconds=121), limit=10, lease_seconds=120)
    assert len(again) == 1 and again[0].attempts == 2


def test_rendered_mails_never_carry_secrets_or_file_contents():
    policy = NotificationPolicy()
    leaky = RunContext(
        run_id=RUN,
        project_id="p",
        task=f"usa el token {SECRET} y la clave -----BEGIN PRIVATE KEY-----",
    )
    draft = policy.draft(
        leaky, kind=RunEventKind.RUN_CREATED, payload={"token": SECRET}, ordinal=0, moment=T0
    )
    assert draft is not None
    # The task text is the operator's own words (shown, truncated); payload values never are.
    assert SECRET not in draft.subject
    assert (
        draft.text.count(SECRET) <= 1 and "BEGIN PRIVATE KEY" in draft.text
    )  # operator-typed text only
    pr = policy.draft(
        CTX,
        kind=RunEventKind.PULL_REQUEST_OPENED,
        payload={
            "pr_number": 1,
            "pr_url": "https://x/pull/1",
            "token": SECRET,
            "body": "contenido",
        },
        ordinal=0,
        moment=T0,
    )
    assert pr is not None and SECRET not in pr.text and "contenido" not in pr.text
    failed = policy.draft(
        CTX,
        kind=RunEventKind.RUN_FAILED,
        payload={"code": "x", "secret": SECRET},
        ordinal=0,
        moment=T0,
    )
    assert failed is not None and SECRET not in failed.text and SECRET not in failed.html


def test_smtp_adapter_requires_starttls_verifies_and_maps_errors(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    class FakeSMTP:
        failure: Exception | None = None
        refused: dict = {}

        def __init__(self, host: str, port: int, timeout: float) -> None:
            calls.append(f"connect:{host}:{port}")

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            calls.append("quit")

        def ehlo(self) -> None:
            calls.append("ehlo")

        def starttls(self, context=None) -> None:
            assert context is not None and context.verify_mode.name == "CERT_REQUIRED"
            calls.append("starttls")

        def login(self, user: str, password: str) -> None:
            calls.append(f"login:{user}")
            if isinstance(FakeSMTP.failure, smtplib.SMTPAuthenticationError):
                raise FakeSMTP.failure

        def send_message(self, msg):
            calls.append("send")
            if FakeSMTP.failure is not None and not isinstance(
                FakeSMTP.failure, smtplib.SMTPAuthenticationError
            ):
                raise FakeSMTP.failure
            FakeSMTP.last = msg
            self.data(msg.as_bytes())
            return FakeSMTP.refused

        def data(self, msg):
            calls.append("data")
            return (250, b"2.0.0 OK 1730000000 abc.123 - gsmtp")

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    with pytest.raises(ValueError):
        SmtpEmailSender(
            host="h", port=587, username="u", password=SecretStr("p"), sender="f@x", starttls=False
        )
    sender = SmtpEmailSender(
        host="smtp.example",
        port=587,
        username="u@x",
        password=SecretStr("app-pass"),
        sender="notif@x",
    )
    message = EmailMessage(
        to=["ops@x"],
        subject="s",
        text="t",
        html="<p>t</p>",
        message_id="<m@x>",
        references=["<r@x>"],
    )
    first = sender.send(message)
    assert (first.ok, first.code) == (True, "smtp_accepted")
    assert first.detail == "250 2.0.0 OK 1730000000 abc.123 - gsmtp"
    assert calls == [
        "connect:smtp.example:587",
        "ehlo",
        "starttls",
        "ehlo",
        "login:u@x",
        "send",
        "data",
        "quit",
    ]
    mime = FakeSMTP.last
    assert (
        mime["Message-ID"] == "<m@x>"
        and mime["In-Reply-To"] == "<r@x>"
        and mime["From"] == "notif@x"
    )
    assert mime.get_content_type() == "multipart/alternative"
    assert "app-pass" not in mime.as_string()
    FakeSMTP.failure = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    outcome = sender.send(message)
    assert (outcome.ok, outcome.code, outcome.retryable) == (False, "smtp_auth_failed", False)
    assert "app-pass" not in (outcome.detail or "")
    FakeSMTP.failure = smtplib.SMTPServerDisconnected("lost")
    outcome = sender.send(message)
    assert outcome.code == "smtp_disconnected" and outcome.retryable and outcome.uncertain
    FakeSMTP.failure = ConnectionRefusedError()
    outcome = sender.send(message)
    assert outcome.code == "smtp_unreachable" and outcome.retryable and not outcome.uncertain
    FakeSMTP.failure = smtplib.SMTPDataError(452, b"quota")
    assert sender.send(message).retryable is True
    FakeSMTP.failure = smtplib.SMTPDataError(552, b"too big")
    assert sender.send(message).retryable is False
    FakeSMTP.failure = None
    FakeSMTP.refused = {"ops@x": (550, b"no")}
    assert sender.send(message).code == "smtp_rejected"


def test_priority_level_keeps_approvals_failures_and_outcomes_only():
    from app.notifications import PRIORITY_KINDS, NotificationEnqueuer

    run = RunContext(run_id=RUN, project_id="p", task="t", workflow_kind="development")
    noisy = [
        (1, event(RunEventKind.RUN_CREATED)),
        (2, event(RunEventKind.ROUTE_SELECTED, {"route": "fast"})),
        (3, event(RunEventKind.WAITING_APPROVAL, {"stage": "scope"})),
        (4, event(RunEventKind.APPROVAL_RECORDED, {"approved": True, "stage": "scope"})),
        (5, event(RunEventKind.DEVELOPER_COMPLETED, {"files": 1})),
        (6, event(RunEventKind.QA_REVIEWED, {"approved": True})),
        (7, event(RunEventKind.RUN_COMPLETED)),
        (8, event(RunEventKind.WAITING_APPROVAL, {"stage": "publish"})),
        (9, event(RunEventKind.PULL_REQUEST_OPENED, {"pr_number": 1, "pr_url": "https://x/1"})),
        (10, event(RunEventKind.RUN_PUBLISHED)),
        (11, event(RunEventKind.RUN_FAILED, {"code": "x"})),
    ]
    priority = NotificationEnqueuer(NotificationPolicy(), recipients=["a@x"])
    verbose = NotificationEnqueuer(NotificationPolicy(), recipients=["a@x"], level="verbose")
    assert [d.kind for d in priority.drafts(run, noisy, moment=T0)] == [
        NotificationKind.SCOPE_PENDING,
        NotificationKind.RUN_COMPLETED,
        NotificationKind.PUBLISH_PENDING,
        NotificationKind.PULL_REQUEST_OPENED,
        NotificationKind.RUN_PUBLISHED,
        NotificationKind.RUN_FAILED,
    ]
    assert len(verbose.drafts(run, noisy, moment=T0)) == 11
    assert {NotificationKind.PLAN_PENDING, NotificationKind.RUN_CANCELLED} <= PRIORITY_KINDS
    assert NotificationKind.DEVELOPER_STARTED not in PRIORITY_KINDS

"""Migration 0009, the PostgreSQL outbox (transactional enqueue, claim with locks,
retries, requeue) and the route-decision audit."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.contracts.lifecycle import RunEventKind, WorkflowKind
from app.contracts.routing import Route, RouteMode
from app.infrastructure.db import (
    Database,
    SqlAlchemyNotificationOutbox,
    SqlAlchemyRouteDecisionRepository,
    SqlAlchemyRunRepository,
)
from app.infrastructure.db.schema import expected_head_revision
from app.infrastructure.notifications import FakeEmailSender
from app.notifications import (
    NotificationEnqueuer,
    NotificationKind,
    NotificationPolicy,
    NotificationWorker,
    OutboxStatus,
    SendOutcome,
)
from app.orchestration.ports import RunEvent
from app.routing import RouteDecision
from tests.integration.conftest import downgrade, migrate

pytestmark = pytest.mark.anyio
T0 = datetime(2026, 10, 27, tzinfo=UTC)


def test_0009_adds_outbox_route_decisions_and_route_columns(empty_database_url: str):
    assert expected_head_revision() == "0011"
    migrate(empty_database_url, "0008")
    engine = create_engine(empty_database_url)
    try:
        migrate(empty_database_url, "0009")
        inspector = inspect(engine)
        assert {"notification_outbox", "route_decisions"} <= set(inspector.get_table_names())
        assert {"route", "route_mode"} <= {c["name"] for c in inspector.get_columns("runs")}
        run_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO runs (id, task, status, owner_subject, project_id, workflow_kind)"
                    " VALUES (:id, 't', 'running', 'a', 'p', 'development')"
                ),
                {"id": run_id},
            )
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO notification_outbox (run_id, kind, recipient, subject, text_body,"
                    " dedup_key, message_id, status)"
                    " VALUES (:id, 'k', 'r', 's', 't', 'd', 'm', 'weird')"
                ),
                {"id": run_id},
            )
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO route_decisions (run_id, route, mode, reason, policy_version)"
                    " VALUES (:id, 'medium', 'auto', 'r', 'v')"
                ),
                {"id": run_id},
            )
        downgrade(empty_database_url, "0008")
        assert "notification_outbox" not in inspect(engine).get_table_names()
        migrate(empty_database_url, "head")
    finally:
        engine.dispose()


async def test_events_and_their_notifications_are_written_in_one_transaction(
    migrated_database_url: str,
):
    database = Database(migrated_database_url)
    try:
        enqueuer = NotificationEnqueuer(
            NotificationPolicy(), recipients=["ops@example.com", "dev@example.com"], level="verbose"
        )
        runs = SqlAlchemyRunRepository(database.session_factory, notifications=enqueuer)
        outbox = SqlAlchemyNotificationOutbox(database.session_factory)
        run_id = uuid.uuid4()
        await runs.create_run(
            run_id,
            "Implementa greet",
            owner="p",
            ttl_seconds=60,
            owner_subject="olivia",
            project_id="sandbox",
            workflow_kind=WorkflowKind.DEVELOPMENT,
        )
        pending = await outbox.list_items(statuses=[OutboxStatus.PENDING])
        assert {(i.kind, i.recipient) for i in pending} == {
            (NotificationKind.RUN_CREATED, "ops@example.com"),
            (NotificationKind.RUN_CREATED, "dev@example.com"),
        }
        events = [
            RunEvent(
                kind=RunEventKind.WAITING_APPROVAL,
                payload={"stage": "scope", "deadline": "2026-10-27T12:00:00+00:00"},
            ),
            RunEvent(kind=RunEventKind.APPROVAL_RECORDED, payload={"stage": "scope"}),  # no mail
        ]
        assert await runs.append_events(run_id, events) == 2
        assert await runs.append_events(run_id, events) == 0  # replay: no new events, no new mails
        items = await outbox.list_items(limit=50)
        assert sorted(i.kind for i in items) == sorted(
            [NotificationKind.RUN_CREATED] * 2 + [NotificationKind.SCOPE_PENDING] * 2
        )
        assert all(i.event_id is not None for i in items)
        # Claim with SKIP LOCKED semantics: two concurrent claimers never share an item.
        now = datetime.now(UTC) + timedelta(seconds=1)
        a, b = await asyncio.gather(
            outbox.claim(now=now, limit=10, lease_seconds=60),
            outbox.claim(now=now, limit=10, lease_seconds=60),
        )
        assert len(a) + len(b) == 4 and not ({i.id for i in a} & {i.id for i in b})
        claimed = a + b
        # Deliver with a scripted sender: one success, one retryable, one dead, one uncertain.
        sender = FakeEmailSender()
        sender.script(
            SendOutcome(ok=True),
            SendOutcome(ok=False, code="smtp_unreachable", retryable=True),
            SendOutcome(ok=False, code="smtp_auth_failed"),
            SendOutcome(ok=False, code="smtp_disconnected", retryable=True, uncertain=True),
        )
        worker = NotificationWorker(outbox, sender, clock=lambda: now)
        # Items are already in "sending" (claimed above): release them by expiring the lease.
        await asyncio.gather(
            *(
                outbox.mark_failed(i.id, now=now, error="reset", next_attempt_at=now, dead=False)
                for i in claimed
            )
        )
        report = await worker.run_once()
        assert (report.claimed, report.sent, report.retried, report.dead) == (4, 1, 2, 1)
        counts = await outbox.counts()
        assert counts == {"sent": 1, "failed": 2, "dead": 1}
        dead = (await outbox.list_items(statuses=[OutboxStatus.DEAD]))[0]
        assert dead.last_error.startswith("smtp_auth_failed")
        assert await outbox.requeue(dead.id, now=now)
        assert (await outbox.counts())["pending"] == 1
        # Nothing in the outbox resembles a credential.
        assert all("ghs_" not in (i.text_body + (i.html_body or "")) for i in items)
    finally:
        await database.dispose()


async def test_route_decisions_are_recorded_and_listed_in_order(migrated_database_url: str):
    database = Database(migrated_database_url)
    try:
        runs = SqlAlchemyRunRepository(database.session_factory)
        repo = SqlAlchemyRouteDecisionRepository(database.session_factory)
        run_id = uuid.uuid4()
        await runs.create_run(
            run_id,
            "t",
            owner="p",
            ttl_seconds=60,
            owner_subject="o",
            project_id="s",
            workflow_kind=WorkflowKind.DEVELOPMENT,
        )
        first = RouteDecision(
            route=Route.FAST,
            mode=RouteMode.AUTO,
            reason="classified:feature/low/few_files",
            confidence=0.9,
            classifier_used=True,
            usage={"calls": 1},
            decided_at=T0,
        )
        second = RouteDecision(
            route=Route.FULL,
            mode=RouteMode.AUTO,
            reason="escalated:scoper_complexity_high",
            confidence=0.9,
            classifier_used=True,
            escalated_from=Route.FAST,
            decided_at=T0 + timedelta(seconds=5),
        )
        await repo.record(run_id, first)
        await repo.record(run_id, second)
        rows = await repo.list_for_run(run_id)
        assert [(r.route, r.reason, r.escalated_from, r.usage) for r in rows] == [
            (Route.FAST, first.reason, None, {"calls": 1}),
            (Route.FULL, second.reason, Route.FAST, {}),
        ]
        assert rows[0].policy_version == "router-v1"
    finally:
        await database.dispose()

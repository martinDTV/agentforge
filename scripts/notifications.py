"""Operator view of the notification outbox: counts, pending/failed/dead items, requeue.

uv run python -m scripts.notifications status
uv run python -m scripts.notifications list --status failed --status dead
uv run python -m scripts.notifications requeue 42
uv run python -m scripts.notifications deliver        # one worker pass, right now
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from app.infrastructure.db import Database, SqlAlchemyNotificationOutbox
from app.infrastructure.notifications import SmtpEmailSender
from app.notifications import NotificationWorker, OutboxStatus
from app.settings import Settings, get_settings


def settings_error_summary(exc: BaseException) -> str:
    """A settings/validation error as one line, never echoing input values (they may
    carry secrets such as the SMTP app password)."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'settings'}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        )
    return f"{exc.__class__.__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"


def sender_from_settings(settings: Settings) -> SmtpEmailSender:
    if not settings.agentforge_email_enabled:
        raise SystemExit("AGENTFORGE_EMAIL_ENABLED=false: no hay remitente configurado")
    assert settings.agentforge_smtp_username and settings.agentforge_smtp_app_password
    assert settings.agentforge_email_from
    return SmtpEmailSender(
        host=settings.agentforge_smtp_host,
        port=settings.agentforge_smtp_port,
        username=settings.agentforge_smtp_username,
        password=settings.agentforge_smtp_app_password,
        sender=settings.agentforge_email_from,
        starttls=settings.agentforge_smtp_starttls,
    )


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    database = Database.from_settings(settings)
    outbox = SqlAlchemyNotificationOutbox(database.session_factory)
    try:
        if args.command == "status":
            print(json.dumps(await outbox.counts(), indent=1))
        elif args.command == "list":
            statuses = [OutboxStatus(s) for s in args.status] if args.status else None
            items = await outbox.list_items(statuses=statuses, limit=args.limit)
            for item in items:
                print(
                    json.dumps(
                        {
                            "id": item.id,
                            "run_id": str(item.run_id)[:8] if item.run_id else None,
                            "goal_id": str(item.goal_id)[:8] if item.goal_id else None,
                            "kind": item.kind.value,
                            "status": item.status.value,
                            "attempts": item.attempts,
                            "next_attempt_at": item.next_attempt_at.isoformat(),
                            "last_error": item.last_error,
                            "subject": item.subject,
                        },
                        ensure_ascii=False,
                    )
                )
        elif args.command == "requeue":
            print(json.dumps({"requeued": await outbox.requeue(args.id, now=datetime.now(UTC))}))
        elif args.command == "deliver":
            worker = NotificationWorker(
                outbox,
                sender_from_settings(settings),
                max_attempts=settings.notifications_max_attempts,
            )
            report = await worker.run_once()
            print(report.model_dump_json(indent=1))
            return 0 if not report.dead else 1
    finally:
        await database.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    listing = sub.add_parser("list")
    listing.add_argument("--status", action="append", choices=[s.value for s in OutboxStatus])
    listing.add_argument("--limit", type=int, default=50)
    requeue = sub.add_parser("requeue")
    requeue.add_argument("id", type=int)
    sub.add_parser("deliver")
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

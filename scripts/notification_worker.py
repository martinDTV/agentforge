"""Standalone delivery worker (the API runs the same loop when e-mail is enabled).

Use it when the API is down or to drain the outbox from another process:

    uv run python -m scripts.notification_worker            # loop until Ctrl-C
    uv run python -m scripts.notification_worker --once     # one pass
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from app.infrastructure.db import Database, SqlAlchemyNotificationOutbox
from app.notifications import NotificationWorker
from app.settings import get_settings
from scripts.notifications import sender_from_settings


async def run(once: bool) -> int:
    settings = get_settings()
    database = Database.from_settings(settings)
    worker = NotificationWorker(
        SqlAlchemyNotificationOutbox(database.session_factory),
        sender_from_settings(settings),
        max_attempts=settings.notifications_max_attempts,
    )
    try:
        if once:
            print((await worker.run_once()).model_dump_json())
            return 0
        await worker.run_forever(settings.notifications_worker_interval_seconds)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        await database.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(run(args.once))


if __name__ == "__main__":
    sys.exit(main())

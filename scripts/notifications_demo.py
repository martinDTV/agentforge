"""Demonstration with FakeEmailSender: the notifications a run produces, rendered.

Replays the durable events of a typical governed run through the policy, the
in-memory outbox and the worker, then prints each subject and plain-text body.
No model, no network, no SMTP.

    uv run python -m scripts.notifications_demo
"""

import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.contracts.lifecycle import RunEventKind
from app.infrastructure.db import InMemoryNotificationOutbox
from app.infrastructure.notifications import FakeEmailSender
from app.notifications import (
    NotificationEnqueuer,
    NotificationPolicy,
    NotificationWorker,
    RunContext,
)
from app.orchestration.ports import RunEvent

K = RunEventKind
SCRIPT: list[tuple[RunEventKind, dict[str, Any], int]] = [
    (K.RUN_CREATED, {"initiated_by": "martin"}, 0),
    (
        K.ROUTE_SELECTED,
        {
            "route": "fast",
            "mode": "auto",
            "reason": "classified:feature/low/few_files",
            "confidence": 0.86,
        },
        0,
    ),
    (K.WAITING_APPROVAL, {"stage": "scope", "deadline": "2026-10-27T12:00:00+00:00"}, 0),
    (K.RUN_APPROVED, {"stage": "scope", "approved_by": "martin"}, 0),
    (K.DEVELOPER_COMPLETED, {"files": 2}, 0),
    (K.DEVELOPER_APPLIED, {"files": 2}, 0),
    (K.QA_REVIEWED, {"approved": False, "issues": 2, "checks": 3, "failed_checks": 1}, 0),
    (K.DEVELOPER_COMPLETED, {"files": 2}, 1),
    (K.DEVELOPER_APPLIED, {"files": 2}, 1),
    (K.QA_REVIEWED, {"approved": True, "issues": 0, "checks": 3, "failed_checks": 0}, 1),
    (K.WAITING_APPROVAL, {"stage": "publish", "deadline": "2026-10-30T12:00:00+00:00"}, 1),
    (
        K.PULL_REQUEST_OPENED,
        {
            "pr_number": 7,
            "pr_url": "https://github.com/martinDTV/agentforge-sandbox-demo/pull/7",
            "remote_branch": "agentforge/...",
        },
        0,
    ),
    (K.RUN_PUBLISHED, {"pr_number": 7}, 0),
]


async def demo(recipient: str) -> None:
    run_id = uuid.uuid4()
    context = RunContext(
        run_id=run_id,
        project_id="sandbox-github",
        task="Implementa validate_email según README y añade pruebas",
    )
    enqueuer = NotificationEnqueuer(
        NotificationPolicy(base_url="http://127.0.0.1:8011"),
        recipients=[recipient],
        level="verbose",
    )
    outbox = InMemoryNotificationOutbox()
    events = [
        (i + 1, RunEvent(kind=kind, payload=payload, ordinal=ordinal))
        for i, (kind, payload, ordinal) in enumerate(SCRIPT)
    ]
    outbox.insert(enqueuer.drafts(context, events, moment=datetime.now(UTC)))
    sender = FakeEmailSender()
    report = await NotificationWorker(outbox, sender).run_once()
    print(json.dumps({"events": len(SCRIPT), "notifications": report.claimed, "sent": report.sent}))
    for message in sender.sent:
        print("=" * 78)
        print("Subject:", message.subject)
        print("Message-ID:", message.message_id)
        print("-" * 78)
        print(message.text)


def main(argv: Sequence[str] | None = None) -> int:
    recipient = argv[0] if argv else "operador@example.com"
    asyncio.run(demo(recipient))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Send ONE real test e-mail through the configured SMTP (operator, explicit).

Refuses unless AGENTFORGE_EMAIL_ENABLED=true and ``--send`` is given; never
prints the password; the message contains no secrets.

    uv run python -m scripts.email_smoke            # validates configuration only
    uv run python -m scripts.email_smoke --send     # sends the test message
"""

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError

from app.notifications import EmailMessage
from app.settings import get_settings
from scripts.notifications import sender_from_settings, settings_error_summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
    except (ValidationError, SettingsError) as exc:
        print(json.dumps({"sent": False, "error": settings_error_summary(exc)}))
        return 2
    summary = {
        "enabled": settings.agentforge_email_enabled,
        "host": settings.agentforge_smtp_host,
        "port": settings.agentforge_smtp_port,
        "starttls": settings.agentforge_smtp_starttls,
        "username_set": bool(settings.agentforge_smtp_username),
        "password_set": settings.agentforge_smtp_app_password is not None,
        "from": settings.agentforge_email_from,
        "to": settings.agentforge_email_to,
    }
    if not settings.agentforge_email_enabled or not args.send:
        print(json.dumps({**summary, "sent": False, "note": "sin --send o sin habilitar"}))
        return 0 if settings.agentforge_email_enabled or not args.send else 2
    sender = sender_from_settings(settings)
    moment = datetime.now(UTC).isoformat(timespec="seconds")
    outcome = sender.send(
        EmailMessage(
            to=settings.agentforge_email_to,
            subject="[AgentForge] Prueba de notificaciones",
            text=f"AgentForge puede enviar notificaciones. Fecha: {moment}\n",
            html=f"<p>AgentForge puede enviar notificaciones.</p><p>Fecha: {moment}</p>",
            message_id=f"<agentforge.smoke.{moment}@agentforge.local>",
        )
    )
    print(
        json.dumps({**summary, "sent": outcome.ok, "code": outcome.code, "detail": outcome.detail})
    )
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    sys.exit(main())

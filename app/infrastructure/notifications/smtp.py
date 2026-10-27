"""SMTP adapter with mandatory STARTTLS and certificate verification.

The credential is read from ``Settings`` once and kept only in this object;
it never appears in messages, logs, outcomes or exceptions surfaced upwards.
"""

import logging
import smtplib
import ssl
from collections.abc import Buffer
from email.message import EmailMessage as MimeMessage
from email.utils import formatdate

from pydantic import SecretStr

from app.notifications import EmailMessage, SendOutcome

logger = logging.getLogger(__name__)


class SmtpEmailSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: SecretStr,
        sender: str,
        starttls: bool = True,
        timeout_seconds: float = 20.0,
    ) -> None:
        if not starttls:
            raise ValueError("STARTTLS es obligatorio para el envío de notificaciones")
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from = sender
        self._timeout = timeout_seconds
        self._context = ssl.create_default_context()  # verifies the server certificate

    def send(self, message: EmailMessage) -> SendOutcome:
        mime = MimeMessage()
        mime["From"] = self._from
        mime["To"] = ", ".join(message.to)
        mime["Subject"] = message.subject
        mime["Date"] = formatdate(localtime=False)
        mime["Message-ID"] = message.message_id
        if message.references:
            mime["References"] = " ".join(message.references)
            mime["In-Reply-To"] = message.references[-1]
        mime["X-AgentForge"] = "notification"
        mime.set_content(message.text)
        if message.html:
            mime.add_alternative(message.html, subtype="html")
        sent_data = False
        accepted: list[tuple[int, bytes]] = []
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
                smtp.ehlo()
                smtp.starttls(context=self._context)
                smtp.ehlo()
                smtp.login(self._username, self._password.get_secret_value())
                original_data = getattr(smtp, "data", None)
                if original_data is not None:
                    # Keep the server's answer to DATA (e.g. "250 2.0.0 OK <id> - gsmtp"):
                    # it is the evidence that the message was accepted for delivery.
                    def recording_data(msg: Buffer | str) -> tuple[int, bytes]:
                        result: tuple[int, bytes] = original_data(msg)
                        accepted.append(result)
                        return result

                    smtp.data = recording_data  # type: ignore[method-assign]
                sent_data = True
                refused = smtp.send_message(mime)
            if refused:
                return SendOutcome(
                    ok=False,
                    code="smtp_rejected",
                    detail=f"{len(refused)} destinatario(s) rechazados",
                )
            return SendOutcome(ok=True, code="smtp_accepted", detail=self._acceptance(accepted))
        except smtplib.SMTPAuthenticationError as exc:
            return SendOutcome(
                ok=False,
                code="smtp_auth_failed",
                detail=f"autenticación rechazada ({exc.smtp_code})",
            )
        except smtplib.SMTPRecipientsRefused:
            return SendOutcome(ok=False, code="smtp_rejected", detail="destinatarios rechazados")
        except smtplib.SMTPSenderRefused:
            return SendOutcome(ok=False, code="smtp_rejected", detail="remitente rechazado")
        except smtplib.SMTPDataError as exc:
            # 4xx = transient (greylisting, quota window); 5xx = permanent for this message.
            return SendOutcome(
                ok=False,
                code="smtp_data_error",
                detail=f"servidor respondió {exc.smtp_code}",
                retryable=exc.smtp_code < 500,
            )
        except smtplib.SMTPServerDisconnected as exc:
            return SendOutcome(
                ok=False,
                code="smtp_disconnected",
                detail=type(exc).__name__,
                retryable=True,
                uncertain=sent_data,  # the data may have been accepted before the drop
            )
        except (smtplib.SMTPException, ssl.SSLError, TimeoutError, OSError) as exc:
            return SendOutcome(
                ok=False, code="smtp_unreachable", detail=type(exc).__name__, retryable=True
            )

    def _acceptance(self, answers: list[tuple[int, bytes]]) -> str | None:
        if not answers:
            return None
        code, response = answers[-1]
        text = f"{code} {response.decode('utf-8', 'replace')}".strip()[:160]
        secret = self._password.get_secret_value()
        return text.replace(secret, "***") if secret else text

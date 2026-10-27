"""In-memory sender for tests and demos: records messages, scripts failures."""

from collections import deque

from app.notifications import EmailMessage, SendOutcome


class FakeEmailSender:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.outcomes: deque[SendOutcome | Exception] = deque()

    def script(self, *outcomes: SendOutcome | Exception) -> None:
        self.outcomes.extend(outcomes)

    def send(self, message: EmailMessage) -> SendOutcome:
        if self.outcomes:
            scripted = self.outcomes.popleft()
            if isinstance(scripted, Exception):
                raise scripted
            if scripted.uncertain:
                self.sent.append(message)  # the server kept it although the answer was lost
            return scripted
        self.sent.append(message)
        return SendOutcome(ok=True)

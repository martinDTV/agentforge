"""Errors of the tool layer. None of them carries HTTP knowledge."""

from app.exceptions import DomainError


class ToolRegistrationError(ValueError):
    """A tool cannot be registered: bad name, duplicate, or unsafe input schema."""


class ToolExecutionError(DomainError):
    """An expected failure reported by a tool, with a stable machine code.

    The gateway turns it into a ``failed`` result; it never reaches the model
    as an exception and never aborts the run by itself.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code

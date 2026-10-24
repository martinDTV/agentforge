"""Base classes for expected business errors.

Domain exceptions carry no HTTP knowledge: ``app.api.errors`` is the single
place that maps them to status codes and RFC 9457 Problem Details responses.
"""


class DomainError(Exception):
    code: str = "domain_error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class UnavailableError(DomainError):
    """The service cannot serve the request right now; the client should retry."""

    code = "unavailable"

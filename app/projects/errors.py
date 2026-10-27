from app.exceptions import DomainError


class PolicyError(DomainError):
    """A repository policy is missing, invalid or refuses the request. ``code`` says why."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code

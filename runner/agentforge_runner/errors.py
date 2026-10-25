class RunnerError(Exception):
    """A refused request or a failed operation, with a stable code and HTTP status."""

    def __init__(self, code: str, detail: str, *, status: int = 400) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status

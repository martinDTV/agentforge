from app.exceptions import DomainError


class SandboxError(DomainError):
    """A refused, failed or unreachable sandbox execution, with a stable code.

    Request-side codes: ``program_unknown``, ``path_invalid``,
    ``path_not_authorized``, ``path_not_found``, ``keyword_invalid``,
    ``archive_too_large``. Runner-side codes: ``sandbox_unconfigured``,
    ``runner_unverified``, ``runner_unreachable``, ``runner_auth_failed``,
    ``runner_timeout``, ``runner_rejected``, ``runner_failed``,
    ``runner_protocol_error``, ``execution_in_progress``.
    """

    def __init__(self, code: str, detail: str, *, retryable: bool = False) -> None:
        super().__init__(detail)
        self.code = code
        self.retryable = retryable

from app.exceptions import DomainError


class GitHubError(DomainError):
    """A refused or failed GitHub operation, with a stable code and no payload.

    Codes: ``github_unconfigured``, ``app_auth_failed``, ``token_invalid``,
    ``installation_forbidden``, ``installation_not_found``, ``repository_not_found``,
    ``permission_not_allowed``, ``token_request_rejected``, ``github_failed``,
    ``github_unreachable``,
    ``github_protocol_error``, ``repository_substituted``, ``repository_transferred``,
    ``repository_identity_mismatch``, ``default_branch_changed``,
    ``repository_archived``, ``repository_not_in_installation``.
    """

    def __init__(self, code: str, detail: str, *, retryable: bool = False) -> None:
        super().__init__(detail)
        self.code = code
        self.retryable = retryable

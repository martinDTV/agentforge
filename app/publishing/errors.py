from app.exceptions import DomainError


class PublicationError(DomainError):
    """A refused or failed publication step, with a stable code.

    ``retryable`` separates transient causes (network, 5xx, timeouts, a token
    GitHub revoked meanwhile) from content or policy causes that no retry can
    fix. Codes: ``publish_unavailable``, ``publish_not_allowed``,
    ``publication_scan_failed``, ``base_moved``, ``base_invalid``,
    ``head_invalid``, ``publish_digest_stale``, ``remote_branch_invalid``,
    ``remote_branch_conflict``, ``publication_timeout``, ``publication_failed``,
    ``publication_state_conflict`` plus GitHub and Git codes propagated as is.
    """

    def __init__(self, code: str, detail: str, *, retryable: bool = False) -> None:
        super().__init__(detail)
        self.code = code
        self.retryable = retryable

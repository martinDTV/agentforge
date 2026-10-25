from app.exceptions import DomainError


class GitOperationError(DomainError):
    """A refused or failed Git operation, with a stable machine code.

    Codes: ``ref_invalid``, ``ref_protected``, ``pathspec_invalid``,
    ``repository_unknown``, ``repository_mismatch``, ``repository_invalid``,
    ``path_outside_root``, ``git_unavailable``, ``git_failed``, ``git_timeout``,
    ``push_disabled``, ``not_a_run_branch``, ``no_commits``.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code

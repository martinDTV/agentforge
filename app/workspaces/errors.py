from app.exceptions import DomainError


class WorkspaceError(DomainError):
    """Codes: ``invalid_identifier``, ``path_outside_storage``, ``worktree_corrupt``,
    ``worktree_quota_exceeded``, ``disk_space_low``, ``storage_invalid``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code

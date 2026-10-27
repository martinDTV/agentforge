"""Publication progress persisted in ``runs.publication`` (ADR 0011 §7).

Each transition is written with compare-and-set on the previous state, so a
second publisher (another process, a replayed approval, a recovery sweep) can
never move the same run twice through the same step.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .errors import PublicationError


class PublicationState(StrEnum):
    UNAVAILABLE = "unavailable"  # remote repository but no publisher configured
    PREPARED = "prepared"  # plan built and shown; waiting for the publish decision
    APPROVED = "approved"  # decision recorded; nothing remote happened yet
    PUSHED = "pushed"  # the branch exists at GitHub with head_sha
    OPENED = "opened"  # the draft pull request exists
    PUBLISHED = "published"  # tokens revoked, audit complete (terminal)
    REJECTED = "rejected"  # the human declined (terminal)
    FAILED = "failed"  # see ``retryable``: transient failures resume, others end
    CANCELLED_AFTER_PUSH = "cancelled_after_push"  # withdrawn once the branch existed


_S = PublicationState
ALLOWED: dict[PublicationState, frozenset[PublicationState]] = {
    _S.UNAVAILABLE: frozenset(),
    _S.PREPARED: frozenset({_S.APPROVED, _S.REJECTED, _S.FAILED}),
    _S.APPROVED: frozenset({_S.PUSHED, _S.FAILED}),
    _S.PUSHED: frozenset({_S.OPENED, _S.FAILED, _S.CANCELLED_AFTER_PUSH}),
    _S.OPENED: frozenset({_S.PUBLISHED, _S.FAILED}),
    _S.PUBLISHED: frozenset(),
    _S.REJECTED: frozenset(),
    # A retryable failure resumes exactly where it stopped (see ``resume_from``).
    _S.FAILED: frozenset({_S.APPROVED, _S.PUSHED, _S.OPENED}),
    _S.CANCELLED_AFTER_PUSH: frozenset(),
}
IN_PROGRESS: frozenset[PublicationState] = frozenset({_S.APPROVED, _S.PUSHED, _S.OPENED})


class PublicationProgress(BaseModel):
    """Current publication state of a run, as stored in ``runs.publication``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    state: PublicationState
    publish_digest: str | None = None
    remote_branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    pr_number: int | None = Field(default=None, ge=1)
    pr_url: str | None = None
    code: str | None = None
    detail: str | None = None
    retryable: bool = False
    # The state a retryable failure must resume from (the last durable step).
    resume_from: PublicationState | None = None
    approved_by: str | None = None
    attempts: int = Field(default=0, ge=0)
    updated_at: datetime | None = None

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    @classmethod
    def from_json(cls, value: dict[str, Any] | None) -> "PublicationProgress | None":
        if not value or "state" not in value:
            return None
        return cls.model_validate(value)

    @property
    def terminal(self) -> bool:
        return self.state in (_S.PUBLISHED, _S.REJECTED, _S.UNAVAILABLE, _S.CANCELLED_AFTER_PUSH)


def advance(
    progress: PublicationProgress, new_state: PublicationState, **fields: Any
) -> PublicationProgress:
    """A new progress record, or ``publication_state_conflict`` when the step is not legal."""
    if new_state not in ALLOWED[progress.state]:
        raise PublicationError(
            "publication_state_conflict",
            f"transición de publicación no permitida: {progress.state} -> {new_state}",
        )
    if new_state is _S.FAILED and "resume_from" not in fields:
        fields["resume_from"] = progress.state if progress.state in IN_PROGRESS else None
    if new_state is not _S.FAILED:
        fields.setdefault("code", None)
        fields.setdefault("detail", None)
        fields.setdefault("retryable", False)
        fields.setdefault("resume_from", None)
    return progress.model_copy(update={"state": new_state, **fields})

"""Filter a Scoper proposal against a repository policy. Deny by default.

Every rejected path is reported with its reason so the human sees what the
model asked for and what the platform refused; nothing rejected can be
approved, because the digest covers only the accepted files.
"""

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from app.contracts.scope import (
    ApprovedScope,
    RejectedPath,
    ScopeAction,
    ScopeFile,
    ScopeProposal,
    is_canonical_relative_path,
    scope_digest,
)
from app.workspaces.sensitive import DENIED_DIRECTORIES, is_sensitive_name

from .globs import matches_any
from .policy import RepositoryPolicy


class ScopeDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: ApprovedScope | None  # None when nothing survived the policy
    rejected: list[RejectedPath]


def _reject(path: str, reason: str) -> RejectedPath:
    return RejectedPath(path=path[:1024], reason=reason)


def check_against_policy(paths: list[str], policy: RepositoryPolicy) -> list[RejectedPath]:
    """Paths of an already approved scope that the *current* policy no longer allows.

    Used before every write round: an approval never outlives a policy that was
    tightened after it (ADR 0009). Empty list means everything is still allowed.
    """
    rejected: list[RejectedPath] = []
    for path in paths:
        if not is_canonical_relative_path(path):
            rejected.append(_reject(path, "path_not_canonical"))
        elif any(p in DENIED_DIRECTORIES for p in path.split("/")) or is_sensitive_name(
            path.split("/")[-1]
        ):
            rejected.append(_reject(path, "sensitive_or_reserved"))
        elif matches_any(path, policy.all_denied_globs):
            rejected.append(_reject(path, "denied_by_policy"))
        elif not matches_any(path, policy.writable_globs):
            rejected.append(_reject(path, "not_writable_by_policy"))
    return rejected


def filter_scope(
    proposal: ScopeProposal,
    policy: RepositoryPolicy,
    *,
    exists: Callable[[str], bool],
) -> ScopeDecision:
    """``exists`` answers whether a path is a regular file in the run's worktree."""
    accepted: list[ScopeFile] = []
    rejected: list[RejectedPath] = []
    seen: set[str] = set()
    for file in proposal.files:
        path = file.path
        if not is_canonical_relative_path(path):
            rejected.append(_reject(path, "path_not_canonical"))
            continue
        if path in seen:
            rejected.append(_reject(path, "duplicate"))
            continue
        seen.add(path)
        parts = path.split("/")
        if any(part in DENIED_DIRECTORIES for part in parts) or is_sensitive_name(parts[-1]):
            rejected.append(_reject(path, "sensitive_or_reserved"))
            continue
        if matches_any(path, policy.all_denied_globs):
            rejected.append(_reject(path, "denied_by_policy"))
            continue
        if not matches_any(path, policy.writable_globs):
            rejected.append(_reject(path, "not_writable_by_policy"))
            continue
        present = exists(path)
        if file.action is ScopeAction.EDIT and not present:
            rejected.append(_reject(path, "edit_of_missing_file"))
            continue
        if file.action is ScopeAction.CREATE and present:
            rejected.append(_reject(path, "create_of_existing_file"))
            continue
        if len(accepted) >= policy.max_files_per_run:
            rejected.append(_reject(path, "too_many_files"))
            continue
        accepted.append(file)
    tests: list[str] = []
    for test in dict.fromkeys(proposal.tests):
        if is_canonical_relative_path(test) and any(
            test == root or test.startswith(root.rstrip("/") + "/") for root in policy.test_paths
        ):
            tests.append(test)
        else:
            rejected.append(_reject(test, "test_path_not_authorized"))
    if not accepted:
        return ScopeDecision(scope=None, rejected=rejected)
    scope = ApprovedScope(
        repository_id=policy.repository_id,
        files=accepted,
        tests=tests,
        digest=scope_digest(policy.repository_id, accepted, tests),
    )
    return ScopeDecision(scope=scope, rejected=rejected)

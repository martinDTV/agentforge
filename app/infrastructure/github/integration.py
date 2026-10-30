"""Integration evidence for the goal Supervisor (Fase 8, ADR 0012 §4.3). Platform-only.

Answers one question with two independent sources: *was the pull request of a
dependency merged, and is that merge already part of the mirrored base?*

- GitHub says whether the PR is open, closed or merged and, when merged, the
  ``merge_commit_sha``. With "squash" or "rebase" merges the run's ``head_sha``
  never appears in the base history, so the head is never used as evidence.
- The mirror is refreshed and asked whether the merge commit is an ancestor of
  ``refs/remotes/origin/<base>`` (``git merge-base --is-ancestor``, read-only).

The token is narrowed to ``pull_requests: read`` and lives only in this
process; agents never see this class, the client or the token.
"""

import asyncio
import logging
from pathlib import Path

from app.contracts.goals import MergeEvidence
from app.git import GitOperationError
from app.git.mirrors import MirrorError, MirrorSyncRepository
from app.git.remote import RemoteSpec
from app.goals.ports import IntegrationError, MergeProbe
from app.infrastructure.git.local import LocalGitAdapter
from app.infrastructure.git.mirror import MirrorService, audited
from app.projects import PolicyError, ProjectPolicyRegistry

from .client import GitHubClient, PullRequestInfo
from .credentials import PULLS_READ_PERMISSIONS, InstallationTokenProvider
from .errors import GitHubError

logger = logging.getLogger(__name__)


class GitHubIntegrationProbe:
    def __init__(
        self,
        *,
        github: GitHubClient,
        tokens: InstallationTokenProvider,
        mirrors: MirrorService,
        git: LocalGitAdapter,
        policies: ProjectPolicyRegistry,
        mirror_syncs: MirrorSyncRepository | None = None,
    ) -> None:
        self._github = github
        self._tokens = tokens
        self._mirrors = mirrors
        self._git = git
        self._policies = policies
        self._syncs = mirror_syncs

    def _spec(self, repository_id: str, project_id: str) -> RemoteSpec:
        try:
            policy = self._policies.require(repository_id, project_id)
        except PolicyError as exc:
            raise IntegrationError(exc.code, exc.detail, retryable=False) from exc
        if policy.remote is None:
            raise IntegrationError(
                "integration_unverifiable",
                f"el repositorio '{repository_id}' no tiene remoto: no hay PR que verificar",
                retryable=False,
            )
        return policy.remote

    def _pull_request(self, spec: RemoteSpec, number: int) -> PullRequestInfo:
        token = self._tokens.token_for(
            spec.installation_id, spec.name, permissions=PULLS_READ_PERMISSIONS
        )
        try:
            return self._github.get_pull_request(token.reveal(), spec.owner, spec.name, number)
        except GitHubError as exc:
            if exc.code != "token_invalid":
                raise
            self._tokens.invalidate(token)
            token = self._tokens.token_for(
                spec.installation_id, spec.name, permissions=PULLS_READ_PERMISSIONS
            )
            return self._github.get_pull_request(token.reveal(), spec.owner, spec.name, number)

    async def probe(self, repository_id: str, project_id: str, pr_number: int) -> MergeProbe:
        spec = self._spec(repository_id, project_id)
        try:
            pr = await asyncio.to_thread(self._pull_request, spec, pr_number)
        except GitHubError as exc:
            raise IntegrationError(exc.code, exc.detail, retryable=exc.retryable) from exc
        if pr.merged_at is None:
            return MergeProbe(state="open" if pr.state == "open" else "closed")
        if not pr.merge_commit_sha:
            raise IntegrationError(
                "merge_commit_unknown",
                f"GitHub declara fusionado el PR #{pr_number} sin merge_commit_sha",
            )
        try:
            outcome = await audited(
                self._syncs, lambda: self._mirrors.refresh(repository_id, project_id)
            )
        except MirrorError as exc:
            raise IntegrationError(exc.code, exc.detail) from exc
        path: Path = self._mirrors.path_for(spec, project_id)
        try:
            verified = await asyncio.to_thread(
                self._git.is_ancestor,
                path,
                pr.merge_commit_sha,
                f"refs/remotes/origin/{spec.default_branch}",
            )
        except GitOperationError as exc:
            # An unknown object (fetch not yet containing the merge) is "not verified",
            # not an error: the next tick refreshes again.
            if exc.code == "git_failed":
                verified = False
            else:
                raise IntegrationError(exc.code, exc.detail) from exc
        return MergeProbe(
            state="merged",
            evidence=MergeEvidence(
                pr_number=pr.number,
                merged_at=pr.merged_at,
                merge_commit_sha=pr.merge_commit_sha,
                verified_in_mirror=verified,
                base_after=outcome.record.base_sha,
            ),
        )

    async def contains(
        self, repository_id: str, project_id: str, *, ancestor: str, descendant: str
    ) -> bool:
        spec = self._spec(repository_id, project_id)
        path = self._mirrors.path_for(spec, project_id)
        try:
            return await asyncio.to_thread(self._git.is_ancestor, path, ancestor, descendant)
        except GitOperationError as exc:
            if exc.code == "git_failed":
                return False
            raise IntegrationError(exc.code, exc.detail) from exc

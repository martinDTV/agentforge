"""Draft pull requests through the GitHub App (ADR 0011 §6, §7). Platform only.

The only write the platform performs against the GitHub REST API: create a
*draft* pull request for a branch that already exists, after looking it up.
Never merges, never updates, never comments. Uses a token narrowed to
``pull_requests: write`` + ``metadata: read`` and revokes it when done.
"""

import logging

from app.git.remote import RemoteSpec

from .client import GitHubClient, PullRequestInfo
from .credentials import PULLS_PERMISSIONS, InstallationToken, InstallationTokenProvider
from .errors import GitHubError

logger = logging.getLogger(__name__)


class GitHubPublisher:
    def __init__(self, client: GitHubClient, tokens: InstallationTokenProvider) -> None:
        self._client = client
        self._tokens = tokens

    def _token(self, spec: RemoteSpec) -> InstallationToken:
        return self._tokens.token_for(
            spec.installation_id, spec.name, permissions=PULLS_PERMISSIONS
        )

    def find(self, spec: RemoteSpec, head_branch: str) -> PullRequestInfo | None:
        """The pull request already opened for ``head_branch``, if any (open first)."""
        token = self._token(spec)
        found = self._client.find_pull_requests(
            token.reveal(), spec.owner, spec.name, head_branch=head_branch
        )
        if not found:
            return None
        open_ones = [pr for pr in found if pr.state == "open"]
        return open_ones[0] if open_ones else found[0]

    def open_draft(
        self, spec: RemoteSpec, *, head_branch: str, title: str, body: str
    ) -> tuple[PullRequestInfo, bool]:
        """Find-or-create: ``(pull_request, created)``.

        A lost response after a successful ``POST`` is recovered by the lookup on
        the next attempt; a 422 "already exists" is resolved the same way. A
        pull request whose head is not ``head_branch`` is never touched.
        """
        existing = self.find(spec, head_branch)
        if existing is not None:
            return existing, False
        token = self._token(spec)
        try:
            created = self._client.create_draft_pull_request(
                token.reveal(),
                spec.owner,
                spec.name,
                title=title,
                body=body,
                head_branch=head_branch,
                base_branch=spec.default_branch,
            )
        except GitHubError as exc:
            if exc.code != "pull_request_rejected":
                raise
            again = self.find(spec, head_branch)  # created meanwhile (or by a lost response)
            if again is None:
                raise
            return again, False
        logger.info(
            "Pull request en borrador #%s creado para %s (%s)",
            created.number,
            spec.full_name,
            head_branch,
        )
        return created, True

    def revoke(self, spec: RemoteSpec) -> None:
        """Kill the pull-request token of this repository once the publication ended."""
        try:
            token = self._tokens.token_for(
                spec.installation_id, spec.name, permissions=PULLS_PERMISSIONS
            )
        except GitHubError:
            return
        self._tokens.revoke(token)

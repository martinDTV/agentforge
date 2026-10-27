"""Draft pull requests through the simulated GitHub API: find-or-create, lost
responses, duplicates, permission gating and token revocation (ADR 0011 §4, §6, §7)."""

import json

import pytest

from app.git.remote import RemoteSpec
from app.infrastructure.github import (
    PULLS_PERMISSIONS,
    PUSH_PERMISSIONS,
    GitHubAppCredentials,
    GitHubClient,
    GitHubError,
    GitHubPublisher,
    InstallationTokenProvider,
)
from tests.unit.github_fixtures import (
    APP_ID,
    INSTALLATION_ID,
    NAME,
    OWNER,
    REPOSITORY_ID,
    FakeGitHub,
)

BRANCH = "agentforge/11111111-2222-4333-8444-555555555555"
SPEC = RemoteSpec(
    owner=OWNER, name=NAME, github_repository_id=REPOSITORY_ID, installation_id=INSTALLATION_ID
)


def setup(*, write_enabled: bool = True, installation_grants_writes: bool = True):
    fake = FakeGitHub()
    if installation_grants_writes:
        fake.app_permissions = {"contents": "write", "pull_requests": "write", "metadata": "read"}
    fake.branches[BRANCH] = "b" * 40
    client = GitHubClient(
        "https://api.github.test", transport=fake.transport(), sleep=lambda s: None
    )
    provider = InstallationTokenProvider(
        client, GitHubAppCredentials(APP_ID, fake.private_key), write_enabled=write_enabled
    )
    return fake, client, provider, GitHubPublisher(client, provider)


def test_open_draft_creates_once_then_finds_the_same_pull_request():
    fake, _client, provider, publisher = setup()
    assert publisher.find(SPEC, BRANCH) is None
    pr, created = publisher.open_draft(
        SPEC, head_branch=BRANCH, title="[AgentForge] x", body="cuerpo"
    )
    assert (
        created and pr.draft and pr.number == 1 and pr.head_ref == BRANCH and pr.base_ref == "main"
    )
    assert pr.url == f"https://github.com/{OWNER}/{NAME}/pull/1"
    posted = [r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/pulls")]
    assert json.loads(posted[0].read()) == {
        "title": "[AgentForge] x",
        "body": "cuerpo",
        "head": BRANCH,
        "base": "main",
        "draft": True,
        "maintainer_can_modify": True,
    }
    again, created_again = publisher.open_draft(SPEC, head_branch=BRANCH, title="t", body="b")
    assert not created_again and again.number == 1 and len(fake.pulls) == 1
    # The token used for pull requests carries exactly pull_requests:write + metadata:read.
    token = provider.token_for(INSTALLATION_ID, NAME, permissions=PULLS_PERMISSIONS)
    assert fake.tokens[token.reveal()]["permissions"] == dict(PULLS_PERMISSIONS)


def test_a_lost_response_after_creation_never_duplicates_the_pull_request():
    fake, _client, _provider, publisher = setup()
    fake.lose_create_response = 1  # GitHub creates the PR, the answer never arrives
    pr, created = publisher.open_draft(SPEC, head_branch=BRANCH, title="t", body="b")
    assert pr.number == 1 and len(fake.pulls) == 1
    assert created is False  # recovered through the lookup, not through a second creation
    # Transient server errors on creation are retried once by the client; a persisting
    # error surfaces as retryable and still leaves no duplicate.
    fake.pulls.clear()
    fake.pulls_fail_next = [502, 503]
    with pytest.raises(GitHubError) as exc:
        publisher.open_draft(SPEC, head_branch=BRANCH, title="t", body="b")
    assert exc.value.code == "github_failed" and exc.value.retryable and fake.pulls == {}
    fake.pulls_fail_next = [502]
    pr, created = publisher.open_draft(SPEC, head_branch=BRANCH, title="t", body="b")
    assert created and len(fake.pulls) == 1


def test_creation_against_an_unknown_branch_is_rejected_and_nothing_is_merged():
    fake, _client, _provider, publisher = setup()
    with pytest.raises(GitHubError) as exc:
        publisher.open_draft(SPEC, head_branch="agentforge/missing", title="t", body="b")
    assert exc.value.code == "pull_request_rejected"
    assert all(r.method in ("GET", "POST") for r in fake.requests)
    assert not any("merge" in r.url.path for r in fake.requests)


def test_write_tokens_need_the_flag_locally_and_the_grant_at_github():
    fake, _client, provider, publisher = setup(write_enabled=False)
    with pytest.raises(GitHubError) as exc:
        publisher.find(SPEC, BRANCH)
    assert exc.value.code == "permission_not_allowed"
    with pytest.raises(GitHubError) as exc:
        provider.token_for(INSTALLATION_ID, NAME, permissions=PUSH_PERMISSIONS)
    assert exc.value.code == "permission_not_allowed"
    assert not any(
        r.url.path.endswith("/access_tokens") for r in fake.requests
    )  # no request at all
    fake2, _client2, provider2, publisher2 = setup(installation_grants_writes=False)
    with pytest.raises(GitHubError) as exc:
        publisher2.open_draft(SPEC, head_branch=BRANCH, title="t", body="b")
    assert exc.value.code == "token_request_rejected"  # GitHub: installation is read-only
    # Read tokens keep working with the flag on: levels are per scope.
    _fake3, _client3, provider3, _ = setup()
    read = provider3.token_for(INSTALLATION_ID, NAME)
    assert read.permissions == {"contents": "read", "metadata": "read"}


def test_tokens_are_revoked_at_github_after_publication():
    fake, client, provider, publisher = setup()
    publisher.open_draft(SPEC, head_branch=BRANCH, title="t", body="b")
    token = provider.token_for(INSTALLATION_ID, NAME, permissions=PULLS_PERMISSIONS)
    secret = token.reveal()
    publisher.revoke(SPEC)
    assert secret in fake.revoked_by_api
    with pytest.raises(GitHubError) as exc:
        client.find_pull_requests(secret, OWNER, NAME, head_branch=BRANCH)
    assert exc.value.code == "token_invalid"
    # Revoking twice (or a token GitHub already forgot) is harmless.
    assert provider.revoke(token) is True

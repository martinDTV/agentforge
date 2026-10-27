"""Publication through the API (ADR 0011): the pending ``publish`` decision with
its digest, who may decide it, stale digests, and the published run. Fakes only."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.git import GitIdentity, RepositoryConfig, RepositoryRegistry
from app.infrastructure.db import (
    InMemoryPublicationRepository,
    InMemoryRunRepository,
    InMemoryToolInvocationRepository,
)
from app.infrastructure.git.publish import GitPublisher
from app.infrastructure.github import GitHubPublisher, InstallationTokenProvider
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.infrastructure.sandbox import FakeSandbox
from app.main import create_app
from app.orchestration.limits import RunLimits
from app.orchestration.publication import PublicationRuntime
from app.security import AuthorizationPolicy, JwtVerifier, VerificationRules
from app.security.dev_keys import DevIssuer
from app.security.tokens import StaticKeys
from app.workspaces import WorktreeManager
from tests.unit.git_fixtures import git
from tests.unit.test_api_development import scoped_llm
from tests.unit.test_mirror_service import PROJECT, REPO, Lab

RUNS = "/api/v1/runs"
BODY = {
    "task": "Añade greet(name) en src/app.py y pruebas en tests/test_greet.py",
    "project_id": PROJECT,
    "workflow_kind": "development",
    "repository_id": REPO,
}


class ApiLab:
    def __init__(self, tmp_path: Path) -> None:
        self.lab = Lab(tmp_path / "lab")
        self.lab.service.create(REPO, PROJECT)
        fake = self.lab.github
        fake.app_permissions = {"contents": "write", "pull_requests": "write", "metadata": "read"}
        fake.branch_resolver = lambda name: self.remote_heads().get(name)
        self.lab.tokens = InstallationTokenProvider(
            self.lab.client, self.lab.credentials, write_enabled=True, clock=self.lab.clock
        )
        self.lab.service = self.lab.make_service()
        registry = RepositoryRegistry(
            self.lab.root,
            [
                RepositoryConfig(
                    id=REPO,
                    project_id=PROJECT,
                    path=self.lab.mirror_path,
                    identity=GitIdentity(name="AgentForge", email="agentforge@noreply.local"),
                )
            ],
        )
        self.manager = WorktreeManager(tmp_path / "worktrees", self.lab.adapter, registry)
        self.runs = InMemoryRunRepository()
        self.publisher = PublicationRuntime(
            git=GitPublisher(self.lab.adapter, allow_file_remotes=True),
            github=GitHubPublisher(self.lab.client, self.lab.tokens),
            tokens=self.lab.tokens,
            audit=InMemoryPublicationRepository(),
            runs=self.runs,
            askpass_dir=self.lab.askpass,
        )

    def remote_heads(self) -> dict[str, str]:
        out = git(
            "for-each-ref",
            "--format=%(refname:short) %(objectname)",
            "refs/heads/",
            cwd=self.lab.remote,
            home=self.lab.home,
        )
        return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())


@pytest.fixture
def issuer() -> DevIssuer:
    return DevIssuer()


@pytest.fixture
def api(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer, tmp_path: Path
) -> Iterator[tuple[TestClient, ApiLab]]:
    lab = ApiLab(tmp_path)
    verifier = JwtVerifier(
        StaticKeys(issuer.jwks), VerificationRules(issuer=issuer.issuer, audience=issuer.audience)
    )
    app = create_app(
        scoped_llm(fake_llm),
        RunLimits(),
        repository=lab.runs,
        token_verifier=verifier,
        authorization=AuthorizationPolicy(separation_of_duties=True),
        tool_audit=InMemoryToolInvocationRepository(),
        worktrees=lab.manager,
        sandbox=FakeSandbox(),
        projects=lab.lab.policies,
        mirrors=lab.lab.service,
        publisher=lab.publisher,
    )
    with TestClient(app) as client:
        yield client, lab


def bearer(issuer: DevIssuer, subject: str, roles: list[str]) -> dict[str, str]:
    return {"Authorization": "Bearer " + issuer.token(subject, roles=roles, projects=[PROJECT])}


def test_publication_needs_its_own_decision_digest_and_a_distinct_human(
    api: tuple[TestClient, ApiLab], issuer: DevIssuer
):
    client, lab = api
    olivia = bearer(issuer, "olivia", ["project_operator"])
    omar = bearer(issuer, "omar", ["project_operator"])
    paula = bearer(issuer, "paula", ["project_operator"])
    alice = bearer(issuer, "alice", ["approver"])
    victor = bearer(issuer, "victor", ["viewer"])
    run = client.post(RUNS, json=BODY, headers=olivia).json()
    run_id = run["run_id"]
    scope = client.get(f"{RUNS}/{run_id}/decisions", headers=victor).json()["pending"]
    approved = client.post(
        f"{RUNS}/{run_id}/approve",
        json={"approved": True, "stage": "scope", "scope_digest": scope["scope_digest"]},
        headers=omar,
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "waiting_approval" and body["pending_stage"] == "publish"
    assert body["publication"]["state"] == "prepared"
    decisions = client.get(f"{RUNS}/{run_id}/decisions", headers=victor).json()
    pending = decisions["pending"]
    assert pending["stage"] == "publish" and pending["scope_digest"] is None
    digest = pending["publish_digest"]
    assert digest and len(digest) == 64
    proposal = pending["proposal"]
    assert proposal["remote_branch"] == f"agentforge/{run_id}"
    assert proposal["base_sha"] == lab.lab.remote_head()
    assert {f["path"] for f in proposal["files"]} == {"src/app.py", "tests/test_greet.py"}
    assert decisions["decisions"][0]["stage"] == "scope"
    assert lab.remote_heads().keys() == {"main"} and lab.lab.github.pulls == {}
    # Denials: viewer, approver without repos:publish, creator, scope approver, stale digest.
    for who, code in (
        (victor, "permission_denied"),
        (alice, "permission_denied"),
        (olivia, "self_approval_forbidden"),
        (omar, "permission_denied"),
    ):
        denied = client.post(
            f"{RUNS}/{run_id}/approve",
            json={"approved": True, "stage": "publish", "publish_digest": digest},
            headers=who,
        )
        assert denied.status_code == 403 and denied.json()["code"] == code, denied.text
    stale = client.post(
        f"{RUNS}/{run_id}/approve",
        json={"approved": True, "stage": "publish", "publish_digest": "0" * 64},
        headers=paula,
    )
    assert stale.status_code == 409 and stale.json()["code"] == "decision_stale"
    missing = client.post(
        f"{RUNS}/{run_id}/approve", json={"approved": True, "stage": "publish"}, headers=paula
    )
    assert missing.status_code == 409 and missing.json()["code"] == "decision_stale"
    wrong_stage = client.post(
        f"{RUNS}/{run_id}/approve",
        json={"approved": True, "stage": "scope", "scope_digest": scope["scope_digest"]},
        headers=paula,
    )
    assert wrong_stage.status_code == 409 and wrong_stage.json()["code"] == (
        "decision_stage_mismatch"
    )
    assert lab.remote_heads().keys() == {"main"} and lab.lab.github.pulls == {}
    # The right human with the exact digest publishes: one branch, one draft PR.
    published = client.post(
        f"{RUNS}/{run_id}/approve",
        json={"approved": True, "stage": "publish", "publish_digest": digest},
        headers=paula,
    )
    assert published.status_code == 200, published.text
    final = published.json()
    assert final["status"] == "published" and final["pending_stage"] is None
    publication = final["publication"]
    assert publication["state"] == "published" and publication["pr_number"] == 1
    assert publication["remote_branch"] == f"agentforge/{run_id}"
    heads = lab.remote_heads()
    assert heads[f"agentforge/{run_id}"] == publication["head_sha"]
    assert heads["main"] == publication["base_sha"]
    assert len(lab.lab.github.pulls) == 1 and lab.lab.github.pulls[1]["draft"] is True
    after = client.get(f"{RUNS}/{run_id}/decisions", headers=victor).json()
    stages = [(d["stage"], d["decided_by"], d["publish_digest"]) for d in after["decisions"]]
    assert stages == [("scope", "omar", None), ("publish", "paula", digest)]
    events = client.get(f"{RUNS}/{run_id}/events?limit=100", headers=victor).json()["items"]
    kinds = [e["kind"] for e in events]
    for kind in ("publication_prepared", "branch_pushed", "pull_request_opened", "run_published"):
        assert kind in kinds, kinds
    pushed = next(e for e in events if e["kind"] == "branch_pushed")
    assert pushed["executed_by"] == "agentforge:publisher" and pushed["approved_by"] == "paula"
    shown = client.get(f"{RUNS}/{run_id}", headers=victor).json()
    assert shown["status"] == "published" and shown["publication"]["pr_url"].endswith("/pull/1")
    # No token in anything the API returns.
    for secret in lab.lab.github.tokens:
        assert secret not in published.text and secret not in str(events)

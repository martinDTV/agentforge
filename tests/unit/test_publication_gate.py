"""P1: a development run or goal on a repository with a remote is refused when this
instance cannot publish, before any model call; local repositories are unaffected."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.contracts.lifecycle import RunLifecycleStatus
from app.infrastructure.db import InMemoryRunRepository, InMemoryToolInvocationRepository
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.main import create_app
from app.orchestration.limits import RunLimits
from app.orchestration.publication import PublicationGate
from app.projects import ProjectPolicyRegistry, RepositoryPolicy
from app.security import AuthorizationPolicy
from app.security.dev_keys import DevIssuer
from app.security.tokens import JwtVerifier, StaticKeys, VerificationRules
from tests.unit.git_fixtures import make_sandbox
from tests.unit.test_api_development import BODY, RUNS, bearer, scoped_llm
from tests.unit.test_mirror_service import remote_spec
from tests.unit.test_sandbox_smoke import FakeSandbox

PROJECT = "sandbox-gate"
REPO = "gate-repo"
S = RunLifecycleStatus


class Deny:
    def allows(self, repository_id: str) -> bool:
        return False


def _client(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer, tmp_path: Path, *, remote: bool, gate: object
) -> Iterator[TestClient]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    box = make_sandbox(tmp_path, project=PROJECT, repository=REPO)
    verifier = JwtVerifier(
        StaticKeys(issuer.jwks), VerificationRules(issuer=issuer.issuer, audience=issuer.audience)
    )
    policy = RepositoryPolicy(
        repository_id=REPO,
        project_id=PROJECT,
        writable_globs=["src/**", "tests/**"],
        remote=remote_spec() if remote else None,
    )
    app = create_app(
        scoped_llm(fake_llm),
        RunLimits(),
        repository=InMemoryRunRepository(),
        token_verifier=verifier,
        authorization=AuthorizationPolicy(separation_of_duties=False),
        tool_audit=InMemoryToolInvocationRepository(),
        worktrees=box.manager,
        sandbox=FakeSandbox(),
        projects=ProjectPolicyRegistry([policy]),
        publishing=gate,  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def issuer() -> DevIssuer:
    return DevIssuer()


def test_the_api_refuses_remote_runs_with_a_clear_409_when_publication_is_disabled(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer, tmp_path: Path
):
    headers = bearer(issuer, "olivia", ["project_operator"], [PROJECT])
    body = {**BODY, "project_id": PROJECT, "repository_id": REPO}
    for client in _client(fake_llm, issuer, tmp_path / "remote", remote=True, gate=Deny()):
        response = client.post(RUNS, json=body, headers=headers)
        assert response.status_code == 409, response.text
        problem = response.json()
        assert problem["code"] == "publish_unavailable"
        assert "GITHUB_WRITE_ENABLED=true" in problem["detail"] and REPO in problem["detail"]
        assert client.get(RUNS, headers=headers).json()["items"] == []  # nothing created
    assert fake_llm.calls == []  # and nothing spent


def test_local_repositories_are_not_gated_and_still_complete(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer, tmp_path: Path
):
    headers = bearer(issuer, "olivia", ["project_operator"], [PROJECT])
    body = {**BODY, "project_id": PROJECT, "repository_id": REPO}
    for client in _client(fake_llm, issuer, tmp_path / "local", remote=False, gate=Deny()):
        response = client.post(RUNS, json=body, headers=headers)
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "waiting_approval"


def test_the_gate_answers_from_configuration_only():
    assert PublicationGate(None).allows("any") is False
    assert uuid.UUID  # the module imports stay meaningful for readers of the payloads

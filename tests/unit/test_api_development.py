"""Governed development runs through the API: authentication, authorization per
stage, stale and duplicate decisions, cancellation. Fakes only."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.development import DeveloperChanges, FileEdit, QAReview
from app.contracts.scope import ScopeAction, ScopeFile, ScopeProposal
from app.infrastructure.db import InMemoryRunRepository, InMemoryToolInvocationRepository
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.infrastructure.sandbox import FakeSandbox
from app.main import create_app
from app.orchestration.limits import RunLimits
from app.projects import ProjectPolicyRegistry, RepositoryPolicy
from app.security import AuthorizationPolicy, JwtVerifier, VerificationRules
from app.security.dev_keys import DevIssuer
from app.security.tokens import StaticKeys
from tests.unit.git_fixtures import make_sandbox

RUNS = "/api/v1/runs"
PROJECT = "sandbox-api"
REPO = "sandbox-api"
BODY = {
    "task": "Añade greet(name) en src/app.py y pruebas en tests/test_greet.py",
    "project_id": PROJECT,
    "workflow_kind": "development",
    "repository_id": REPO,
}


def scoped_llm(fake_llm: FakeLLMAdapter) -> FakeLLMAdapter:
    fake_llm.register(
        ScopeProposal,
        ScopeProposal(
            summary="dos archivos",
            files=[
                ScopeFile(path="src/app.py", action=ScopeAction.EDIT, reason="implementar"),
                ScopeFile(path="tests/test_greet.py", action=ScopeAction.CREATE, reason="probar"),
            ],
            tests=["tests"],
        ),
    )
    fake_llm.register(
        DeveloperChanges,
        DeveloperChanges(
            summary="hecho",
            edits=[
                FileEdit(path="src/app.py", content="def greet(n):\n    return n\n"),
                FileEdit(path="tests/test_greet.py", content="def test_ok():\n    assert 1\n"),
            ],
        ),
    )
    fake_llm.register(QAReview, QAReview(approved=True, summary="ok", issues=[]))
    return fake_llm


@pytest.fixture
def issuer() -> DevIssuer:
    return DevIssuer()


@pytest.fixture
def client(fake_llm: FakeLLMAdapter, issuer: DevIssuer, tmp_path: Path) -> Iterator[TestClient]:
    box = make_sandbox(tmp_path, project=PROJECT, repository=REPO)
    verifier = JwtVerifier(
        StaticKeys(issuer.jwks), VerificationRules(issuer=issuer.issuer, audience=issuer.audience)
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
        projects=ProjectPolicyRegistry(
            [
                RepositoryPolicy(
                    repository_id=REPO, project_id=PROJECT, writable_globs=["src/**", "tests/**"]
                )
            ]
        ),
    )
    with TestClient(app) as client:
        yield client


def bearer(
    issuer: DevIssuer, subject: str, roles: list[str], projects: list[str] | None = None
) -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + issuer.token(subject, roles=roles, projects=projects or [PROJECT])
    }


def test_development_run_waits_for_scope_and_is_approved_by_digest(
    client: TestClient, issuer: DevIssuer
):
    olivia = bearer(issuer, "olivia", ["project_operator"])
    created = client.post(RUNS, json=BODY, headers=olivia)
    assert created.status_code == 201, created.text
    run = created.json()
    assert run["status"] == "waiting_approval" and run["pending_stage"] == "scope"
    assert run["workflow_kind"] == "development" and run["approval_deadline"]
    decisions = client.get(f"{RUNS}/{run['run_id']}/decisions", headers=olivia).json()
    pending = decisions["pending"]
    assert pending["stage"] == "scope" and pending["proposal"]["files"]
    digest = pending["scope_digest"]
    # Stale / missing digest, wrong stage.
    stale = client.post(
        f"{RUNS}/{run['run_id']}/approve",
        json={"approved": True, "scope_digest": "0" * 64},
        headers=olivia,
    )
    assert stale.status_code == 409 and stale.json()["code"] == "decision_stale"
    missing = client.post(
        f"{RUNS}/{run['run_id']}/approve", json={"approved": True}, headers=olivia
    )
    assert missing.status_code == 409 and missing.json()["code"] == "decision_stale"
    wrong = client.post(
        f"{RUNS}/{run['run_id']}/approve",
        json={"approved": True, "stage": "publish", "scope_digest": digest},
        headers=olivia,
    )
    assert wrong.status_code == 409 and wrong.json()["code"] == "decision_stage_mismatch"
    # Permission per stage: an approver without repos:write cannot approve a scope.
    alice = bearer(issuer, "alice", ["approver"])
    forbidden = client.post(
        f"{RUNS}/{run['run_id']}/approve",
        json={"approved": True, "scope_digest": digest},
        headers=alice,
    )
    assert forbidden.status_code == 403
    victor = bearer(issuer, "victor", ["viewer"])
    seen = client.get(f"{RUNS}/{run['run_id']}/decisions", headers=victor)
    assert seen.status_code == 200
    assert (
        client.post(
            f"{RUNS}/{run['run_id']}/approve",
            json={"approved": True, "scope_digest": digest},
            headers=victor,
        ).status_code
        == 403
    )
    # The right digest by the right human.
    approved = client.post(
        f"{RUNS}/{run['run_id']}/approve",
        json={"approved": True, "stage": "scope", "scope_digest": digest},
        headers=olivia,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "completed" and approved.json()["pending_stage"] is None
    after = client.get(f"{RUNS}/{run['run_id']}/decisions", headers=olivia).json()
    assert after["pending"] is None
    assert [(d["stage"], d["approved"], d["decided_by"]) for d in after["decisions"]] == [
        ("scope", True, "olivia")
    ]
    assert after["decisions"][0]["applied_at"] is not None
    # Replay is idempotent; contradiction is a conflict; cancel is now impossible.
    assert (
        client.post(
            f"{RUNS}/{run['run_id']}/approve",
            json={"approved": True, "scope_digest": digest},
            headers=olivia,
        ).status_code
        == 200
    )
    again = client.post(
        f"{RUNS}/{run['run_id']}/approve",
        json={"approved": False, "scope_digest": digest},
        headers=olivia,
    )
    assert again.status_code == 409 and again.json()["code"] == "run_already_decided"
    cancel = client.post(f"{RUNS}/{run['run_id']}/cancel", headers=olivia)
    assert cancel.status_code == 409 and cancel.json()["code"] == "run_not_cancellable"
    events = client.get(f"{RUNS}/{run['run_id']}/events?limit=100", headers=olivia).json()["items"]
    kinds = [e["kind"] for e in events]
    for kind in (
        "scope_proposed",
        "waiting_approval",
        "approval_recorded",
        "approval_applied",
        "run_completed",
    ):
        assert kind in kinds, kind
    recorded = next(e for e in events if e["kind"] == "approval_recorded")
    assert recorded["actor"] == "olivia" and recorded["payload"]["stage"] == "scope"


def test_creation_requires_repos_write_a_repository_and_a_policy(
    client: TestClient, issuer: DevIssuer
):
    oscar = bearer(issuer, "oscar", ["operator"])  # runs:create without repos:write
    assert client.post(RUNS, json=BODY, headers=oscar).status_code == 403
    olivia = bearer(issuer, "olivia", ["project_operator"])
    no_repo = client.post(RUNS, json={**BODY, "repository_id": None}, headers=olivia)
    assert no_repo.status_code == 400 and no_repo.json()["code"] == "repository_required"
    unknown = client.post(RUNS, json={**BODY, "repository_id": "other"}, headers=olivia)
    assert unknown.status_code == 403 and unknown.json()["code"] == "repository_not_authorized"
    eve = bearer(issuer, "eve", ["project_operator"], ["other-project"])
    assert client.post(RUNS, json=BODY, headers=eve).status_code == 403
    # Planning runs keep working for the plain operator.
    planning = client.post(RUNS, json={"task": BODY["task"], "project_id": PROJECT}, headers=oscar)
    assert planning.status_code == 201 and planning.json()["workflow_kind"] == "planning"


def test_waiting_run_can_be_cancelled_or_rejected_by_an_authorised_human(
    client: TestClient, issuer: DevIssuer
):
    olivia = bearer(issuer, "olivia", ["project_operator"])
    run = client.post(RUNS, json=BODY, headers=olivia).json()
    victor = bearer(issuer, "victor", ["viewer"])
    assert client.post(f"{RUNS}/{run['run_id']}/cancel", headers=victor).status_code == 403
    cancelled = client.post(f"{RUNS}/{run['run_id']}/cancel", headers=olivia)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    other = client.post(RUNS, json=BODY, headers=olivia).json()
    digest = client.get(f"{RUNS}/{other['run_id']}/decisions", headers=olivia).json()["pending"][
        "scope_digest"
    ]
    rejected = client.post(
        f"{RUNS}/{other['run_id']}/approve",
        json={"approved": False, "scope_digest": digest},
        headers=olivia,
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    listing = client.get(f"{RUNS}?status=cancelled", headers=olivia).json()["items"]
    assert [r["run_id"] for r in listing] == [run["run_id"]]


def test_development_workflow_is_absent_without_policies(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer
):
    verifier = JwtVerifier(
        StaticKeys(issuer.jwks), VerificationRules(issuer=issuer.issuer, audience=issuer.audience)
    )
    with TestClient(
        create_app(
            scoped_llm(fake_llm),
            RunLimits(),
            token_verifier=verifier,
            authorization=AuthorizationPolicy(),
        )
    ) as client:
        response = client.post(
            RUNS, json=BODY, headers=bearer(issuer, "olivia", ["project_operator"])
        )
    assert response.status_code == 503 and response.json()["code"] == "workflow_unavailable"
    assert uuid.UUID  # imported for readers of the payloads

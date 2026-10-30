"""Goals through the API (Fase 8): creation rules, visibility, cancellation, idempotency and
the 503 of an instance without the development workflow. Fakes only; one child at a time."""

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
from tests.unit.test_api_development import scoped_llm

GOALS = "/api/v1/goals"
RUNS = "/api/v1/runs"
PROJECT = "sandbox-api"
REPO = "sandbox-api"
OBJECTIVE = "Añade greet(name) en src/app.py y pruebas en tests/test_greet.py para el saludo"


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


def bearer(issuer: DevIssuer, subject: str, roles: list[str]) -> dict[str, str]:
    return {"Authorization": "Bearer " + issuer.token(subject, roles=roles, projects=[PROJECT])}


def wait_for(client: TestClient, goal_id: str, headers: dict[str, str], predicate, timeout=20.0):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while True:
        goal = client.get(f"{GOALS}/{goal_id}", headers=headers).json()
        if predicate(goal):
            return goal
        assert time.monotonic() < deadline, goal
        time.sleep(0.05)


def test_a_single_goal_runs_one_child_whose_approval_stays_human(
    client: TestClient, issuer: DevIssuer
):
    olivia = bearer(issuer, "olivia", ["project_operator"])
    body = {
        "objective": OBJECTIVE,
        "project_id": PROJECT,
        "repository_id": REPO,
        "decomposition": "single",
    }
    created = client.post(GOALS, json=body, headers={**olivia, "Idempotency-Key": "g1"})
    assert created.status_code == 201, created.text
    goal = created.json()
    assert goal["status"] == "active" and goal["owner"] == "olivia"
    assert goal["limits"]["max_concurrency"] == 1 and goal["limits"]["max_total_model_calls"]
    assert [t["task_key"] for t in goal["tasks"]] == ["single"]
    goal_id = goal["goal_id"]
    # The child is created in the background and stops at the scope decision.
    goal = wait_for(client, goal_id, olivia, lambda g: g["tasks"][0]["status"] == "waiting_human")
    task = goal["tasks"][0]
    assert task["run_status"] == "waiting_approval" and task["pending_stage"] == "scope"
    assert task["pending_digest"] and task["run_id"]
    run = client.get(f"{RUNS}/{task['run_id']}", headers=olivia).json()
    assert run["owner"] == "olivia" and run["workflow_kind"] == "development"
    # Idempotent replay of the creation.
    again = client.post(GOALS, json=body, headers={**olivia, "Idempotency-Key": "g1"})
    assert again.status_code == 200 and again.headers["Idempotency-Replayed"] == "true"
    assert again.json()["goal_id"] == goal_id
    # Visibility and listing follow the run rules.
    victor = bearer(issuer, "victor", ["viewer"])
    assert client.get(f"{GOALS}/{goal_id}", headers=victor).status_code == 200
    listed = client.get(GOALS, params={"project_id": PROJECT}, headers=victor).json()
    assert [g["goal_id"] for g in listed["items"]] == [goal_id]
    assert listed["items"][0]["tasks_total"] == 1
    events = client.get(f"{GOALS}/{goal_id}/events", headers=victor).json()
    kinds = [e["kind"] for e in events["items"]]
    assert kinds[0] == "goal_created" and "child_created" in kinds
    # The human approves the child's scope through the run API; the goal follows the run.
    decisions = client.get(f"{RUNS}/{task['run_id']}/decisions", headers=olivia).json()
    digest = decisions["pending"]["scope_digest"]
    approved = client.post(
        f"{RUNS}/{task['run_id']}/approve",
        json={"approved": True, "stage": "scope", "scope_digest": digest},
        headers=olivia,
    )
    assert approved.status_code == 200 and approved.json()["status"] == "completed"
    goal = wait_for(client, goal_id, olivia, lambda g: g["status"] == "completed")
    assert goal["tasks"][0]["status"] == "published"
    assert goal["usage"]["tasks_created"] == 1 and goal["usage"]["llm_calls_used"] >= 3
    done = client.post(f"{GOALS}/{goal_id}/cancel", headers=olivia)
    assert done.status_code == 409 and done.json()["code"] == "goal_not_cancellable"


def test_creation_rules_are_enforced_before_anything_is_written(
    client: TestClient, issuer: DevIssuer
):
    olivia = bearer(issuer, "olivia", ["project_operator"])
    base = {"objective": OBJECTIVE, "project_id": PROJECT, "repository_id": REPO}
    # D2: a repository without remote only accepts ``single``.
    for decomposition in ("auto", "plan"):
        refused = client.post(GOALS, json={**base, "decomposition": decomposition}, headers=olivia)
        assert refused.status_code == 400, refused.text
        assert refused.json()["code"] == "goal_dependencies_unsupported"
    # repos:write is required.
    ops = bearer(issuer, "ops", ["operator"])
    refused_role = client.post(GOALS, json={**base, "decomposition": "single"}, headers=ops)
    assert refused_role.status_code == 403
    # Unknown repository.
    unknown = client.post(
        GOALS, json={**base, "repository_id": "nope", "decomposition": "single"}, headers=olivia
    )
    assert unknown.status_code == 403 and unknown.json()["code"] == "repository_not_authorized"
    # Validation: objective too short, concurrency above one.
    short = client.post(GOALS, json={**base, "objective": "corto"}, headers=olivia)
    assert short.status_code == 422
    wide = client.post(
        GOALS,
        json={**base, "decomposition": "single", "limits": {"max_concurrency": 2}},
        headers=olivia,
    )
    assert wide.status_code == 422
    assert client.get(GOALS, headers=olivia).json()["items"] == []
    missing = client.get(f"{GOALS}/00000000-0000-0000-0000-000000000000", headers=olivia)
    assert missing.status_code == 404 and missing.json()["code"] == "goal_not_found"


def test_a_human_cancels_a_goal_and_its_waiting_child(client: TestClient, issuer: DevIssuer):
    olivia = bearer(issuer, "olivia", ["project_operator"])
    body = {
        "objective": OBJECTIVE,
        "project_id": PROJECT,
        "repository_id": REPO,
        "decomposition": "single",
    }
    goal_id = client.post(GOALS, json=body, headers=olivia).json()["goal_id"]
    goal = wait_for(client, goal_id, olivia, lambda g: g["tasks"][0]["status"] == "waiting_human")
    victor = bearer(issuer, "victor", ["viewer"])
    assert client.post(f"{GOALS}/{goal_id}/cancel", headers=victor).status_code == 403
    cancelled = client.post(f"{GOALS}/{goal_id}/cancel", headers=olivia)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    run = client.get(f"{RUNS}/{goal['tasks'][0]['run_id']}", headers=olivia).json()
    assert run["status"] == "cancelled"
    events = client.get(f"{GOALS}/{goal_id}/events", headers=olivia).json()["items"]
    cancelled_event = next(e for e in events if e["kind"] == "child_cancelled")
    assert cancelled_event["payload"]["cancelled_by"] == "olivia"
    # Operators may force a reconciliation; viewers may not.
    assert client.post(f"{GOALS}/{goal_id}/reconcile", headers=victor).status_code == 403
    admin = bearer(issuer, "root", ["admin"])
    forced = client.post(f"{GOALS}/{goal_id}/reconcile", headers=admin)
    assert forced.status_code == 200 and forced.json()["status"] == "cancelled"


def test_an_instance_without_the_development_workflow_has_no_supervisor(
    fake_llm: FakeLLMAdapter,
):
    app = create_app(fake_llm, RunLimits())
    with TestClient(app) as client:
        response = client.post(
            GOALS,
            json={"objective": OBJECTIVE, "project_id": PROJECT, "repository_id": REPO},
        )
        assert response.status_code == 503
        assert response.json()["code"] == "supervisor_unavailable"

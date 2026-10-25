"""Authorization through the API: projects, ownership, roles, self-approval, actors."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.infrastructure.db import InMemoryRunRepository
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.main import create_app
from app.orchestration.limits import RunLimits
from app.security import AuthorizationPolicy, JwtVerifier, VerificationRules
from app.security.dev_keys import DevIssuer
from app.security.tokens import StaticKeys

RUNS = "/api/v1/runs"
TASK = {"task": "Crear módulo de pacientes con pruebas"}


@pytest.fixture
def issuer() -> DevIssuer:
    return DevIssuer()


@pytest.fixture
def repository() -> InMemoryRunRepository:
    return InMemoryRunRepository()


def make_client(
    fake_llm: FakeLLMAdapter,
    issuer: DevIssuer,
    repository: InMemoryRunRepository,
    *,
    separation_of_duties: bool = True,
) -> TestClient:
    verifier = JwtVerifier(
        StaticKeys(issuer.jwks), VerificationRules(issuer=issuer.issuer, audience=issuer.audience)
    )
    return TestClient(
        create_app(
            fake_llm,
            RunLimits(),
            repository=repository,
            token_verifier=verifier,
            authorization=AuthorizationPolicy(separation_of_duties=separation_of_duties),
        )
    )


@pytest.fixture
def client(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer, repository: InMemoryRunRepository
) -> Iterator[TestClient]:
    with make_client(fake_llm, issuer, repository) as client:
        yield client


def bearer(
    issuer: DevIssuer, subject: str, roles: list[str], projects: list[str]
) -> dict[str, str]:
    return {"Authorization": f"Bearer {issuer.token(subject, roles=roles, projects=projects)}"}


def create(client: TestClient, headers: dict[str, str], **body: object) -> dict[str, object]:
    response = client.post(RUNS, json={**TASK, **body}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


# 1 ---------------------------------------------------------------------------


def test_authenticated_principal_without_permissions_can_do_nothing(
    client: TestClient, issuer: DevIssuer
):
    nobody = bearer(issuer, "nobody", roles=[], projects=["alpha"])
    stranger_run = create(client, bearer(issuer, "alice", ["operator"], ["alpha"]))
    assert client.post(RUNS, json=TASK, headers=nobody).status_code == 403
    assert client.post(RUNS, json=TASK, headers=nobody).json()["code"] == "permission_denied"
    assert client.get(f"{RUNS}/{stranger_run['run_id']}", headers=nobody).status_code == 404
    approve = client.post(
        f"{RUNS}/{stranger_run['run_id']}/approve", json={"approved": True}, headers=nobody
    )
    assert approve.status_code == 404  # not even visible: hidden, not forbidden


# 2 ---------------------------------------------------------------------------


def test_operator_works_inside_its_own_project(client: TestClient, issuer: DevIssuer):
    alice = bearer(issuer, "alice", ["operator"], ["alpha"])
    created = create(client, alice)
    assert created["owner"] == "alice" and created["project_id"] == "alpha"
    assert client.get(f"{RUNS}/{created['run_id']}", headers=alice).status_code == 200
    explicit = create(client, alice, project_id="alpha")
    assert explicit["project_id"] == "alpha"


# 3 ---------------------------------------------------------------------------


def test_runs_of_another_project_do_not_exist_for_the_caller(client: TestClient, issuer: DevIssuer):
    alpha_run = create(client, bearer(issuer, "alice", ["operator"], ["alpha"]))
    bob = bearer(issuer, "bob", ["admin"], ["beta"])
    missing = client.get(f"{RUNS}/{alpha_run['run_id']}", headers=bob)
    assert missing.status_code == 404 and missing.json()["code"] == "run_not_found"
    assert (
        client.post(
            f"{RUNS}/{alpha_run['run_id']}/approve", json={"approved": True}, headers=bob
        ).status_code
        == 404
    )
    # Naming the project in the body does not open a door either.
    foreign = client.post(RUNS, json={**TASK, "project_id": "alpha"}, headers=bob)
    assert foreign.status_code == 403 and foreign.json()["code"] == "permission_denied"


# 4 ---------------------------------------------------------------------------


def test_approving_needs_the_permission_on_a_visible_run(client: TestClient, issuer: DevIssuer):
    alpha_run = create(client, bearer(issuer, "alice", ["operator"], ["alpha"]))
    viewer = bearer(issuer, "vic", ["viewer"], ["alpha"])
    assert client.get(f"{RUNS}/{alpha_run['run_id']}", headers=viewer).status_code == 200
    denied = client.post(
        f"{RUNS}/{alpha_run['run_id']}/approve", json={"approved": True}, headers=viewer
    )
    assert denied.status_code == 403 and denied.json()["code"] == "permission_denied"
    assert "runs:approve" in denied.json()["detail"]
    approver = bearer(issuer, "ana", ["approver"], ["alpha"])
    approved = client.post(
        f"{RUNS}/{alpha_run['run_id']}/approve", json={"approved": True}, headers=approver
    )
    assert approved.status_code == 200 and approved.json()["status"] == "approved"


# 5 ---------------------------------------------------------------------------


def test_admin_scope_is_limited_to_its_projects_unless_platform_admin(
    client: TestClient, issuer: DevIssuer
):
    alpha_run = create(client, bearer(issuer, "alice", ["operator"], ["alpha"]))
    beta_admin = bearer(issuer, "badmin", ["admin"], ["beta"])
    assert client.get(f"{RUNS}/{alpha_run['run_id']}", headers=beta_admin).status_code == 404
    platform = bearer(issuer, "root", ["platform_admin"], [])
    assert client.get(f"{RUNS}/{alpha_run['run_id']}", headers=platform).status_code == 200
    assert (
        client.post(
            f"{RUNS}/{alpha_run['run_id']}/approve", json={"approved": False}, headers=platform
        ).json()["status"]
        == "rejected"
    )
    # Global scope still has to name a project to create.
    assert client.post(RUNS, json=TASK, headers=platform).status_code == 403
    assert create(client, platform, project_id="gamma")["project_id"] == "gamma"


# 6 ---------------------------------------------------------------------------


def test_valid_token_without_project_claims(client: TestClient, issuer: DevIssuer):
    alpha_run = create(client, bearer(issuer, "alice", ["operator"], ["alpha"]))
    homeless = bearer(issuer, "nomad", ["operator", "approver"], [])
    no_project = client.post(RUNS, json=TASK, headers=homeless)
    assert no_project.status_code == 403 and "ningún proyecto" in no_project.json()["detail"]
    assert (
        client.post(RUNS, json={**TASK, "project_id": "alpha"}, headers=homeless).status_code == 403
    )
    assert client.get(f"{RUNS}/{alpha_run['run_id']}", headers=homeless).status_code == 404
    several = bearer(issuer, "multi", ["operator"], ["alpha", "beta"])
    ambiguous = client.post(RUNS, json=TASK, headers=several)
    assert ambiguous.status_code == 400 and ambiguous.json()["code"] == "project_required"
    assert create(client, several, project_id="beta")["project_id"] == "beta"


# 7 ---------------------------------------------------------------------------


def test_self_approval_follows_the_separation_of_duties_policy(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer, repository: InMemoryRunRepository
):
    both = bearer(issuer, "alice", ["operator", "approver"], ["alpha"])
    with make_client(fake_llm, issuer, repository, separation_of_duties=True) as strict:
        created = create(strict, both)
        denied = strict.post(
            f"{RUNS}/{created['run_id']}/approve", json={"approved": True}, headers=both
        )
        assert denied.status_code == 403 and denied.json()["code"] == "self_approval_forbidden"
        other = bearer(issuer, "ana", ["approver"], ["alpha"])
        assert (
            strict.post(
                f"{RUNS}/{created['run_id']}/approve", json={"approved": True}, headers=other
            ).status_code
            == 200
        )
    with make_client(
        fake_llm, issuer, InMemoryRunRepository(), separation_of_duties=False
    ) as lenient:
        created = create(lenient, both)
        assert (
            lenient.post(
                f"{RUNS}/{created['run_id']}/approve", json={"approved": True}, headers=both
            ).status_code
            == 200
        )


# 8 ---------------------------------------------------------------------------


def test_actors_are_recorded_on_creation_and_approval(
    client: TestClient, issuer: DevIssuer, repository: InMemoryRunRepository
):
    created = create(client, bearer(issuer, "alice", ["operator"], ["alpha"]))
    rid = uuid.UUID(created["run_id"])
    client.post(
        f"{RUNS}/{rid}/approve",
        json={"approved": True},
        headers=bearer(issuer, "ana", ["approver"], ["alpha"]),
    )
    decision = repository.approvals[rid]
    assert decision.decided_by == "ana" and decision.applied_at is not None
    actors = {e.kind.value: e.actor_subject for e in repository.events[rid]}
    assert actors["run_created"] == "alice"
    assert actors["approval_recorded"] == "ana"
    assert actors["run_approved"] == "ana" and actors["approval_applied"] == "ana"
    assert actors["planner_completed"] is None and actors["architect_reviewed"] is None  # agents
    assert repository.runs[rid].architect_approved is True  # the architect's verdict is separate


# 9 ---------------------------------------------------------------------------


def test_idempotency_keys_are_scoped_to_principal_and_project(
    client: TestClient, issuer: DevIssuer
):
    alice = bearer(issuer, "alice", ["operator"], ["alpha"])
    bob = bearer(issuer, "bob", ["operator"], ["alpha"])
    alice_beta = bearer(issuer, "alice", ["operator"], ["beta"])
    key = {"Idempotency-Key": "order-7"}
    first = client.post(RUNS, json=TASK, headers={**alice, **key})
    replay = client.post(RUNS, json=TASK, headers={**alice, **key})
    bobs = client.post(RUNS, json=TASK, headers={**bob, **key})
    other_project = client.post(RUNS, json=TASK, headers={**alice_beta, **key})
    assert (first.status_code, replay.status_code, bobs.status_code, other_project.status_code) == (
        201,
        200,
        201,
        201,
    )
    ids = {first.json()["run_id"], bobs.json()["run_id"], other_project.json()["run_id"]}
    assert len(ids) == 3 and replay.json()["run_id"] == first.json()["run_id"]
    assert bobs.json()["owner"] == "bob" and other_project.json()["project_id"] == "beta"
    # Bob never sees Alice's run through the key, nor directly.
    assert (
        client.get(f"{RUNS}/{first.json()['run_id']}", headers=bob).status_code == 200
    )  # same project, read
    assert client.get(f"{RUNS}/{first.json()['run_id']}", headers=alice_beta).status_code == 404


# 11 --------------------------------------------------------------------------


def test_no_implicit_privileges_once_a_verifier_is_injected(client: TestClient):
    assert client.post(RUNS, json=TASK).status_code == 401
    assert client.get(f"{RUNS}/{uuid.uuid4()}").status_code == 401


def test_production_policy_defaults_to_separation_of_duties():
    from app.settings import Settings

    def settings(environment: str, **extra: object) -> Settings:
        return Settings(  # type: ignore[call-arg]
            _env_file=None,
            anthropic_vertex_project_id="p",
            database_url="postgresql+psycopg://localhost/agentforge",
            environment=environment,
            database_ssl_required=False,
            auth_mode="oidc",
            oidc_issuer="https://issuer.example",
            oidc_audience="agentforge",
            oidc_jwks_url="https://issuer.example/jwks.json",
            **extra,
        )

    assert settings("production").separation_of_duties is True
    assert settings("local").separation_of_duties is False
    assert settings("production", authz_separation_of_duties=False).separation_of_duties is False
    assert AuthorizationPolicy().separation_of_duties is True  # the code default is the strict one

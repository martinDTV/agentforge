"""route_mode through the API and the route in the response (fakes only)."""

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
from app.routing import SmartRouter, TaskClassification
from app.security import AuthorizationPolicy, JwtVerifier, VerificationRules
from app.security.dev_keys import DevIssuer
from app.security.tokens import StaticKeys
from tests.unit.git_fixtures import make_sandbox
from tests.unit.test_api_development import scoped_llm
from tests.unit.test_smart_router_flow import low_risk, plan, review

RUNS = "/api/v1/runs"
PROJECT = "sandbox-api"
REPO = "sandbox-api"
BODY = {
    "task": "Añade greet(name) en src/app.py y pruebas en tests/test_greet.py",
    "project_id": PROJECT,
    "workflow_kind": "development",
    "repository_id": REPO,
}


@pytest.fixture
def issuer() -> DevIssuer:
    return DevIssuer()


@pytest.fixture
def client(fake_llm: FakeLLMAdapter, issuer: DevIssuer, tmp_path: Path) -> Iterator[TestClient]:
    from app.contracts.planning import ArchitectureReview, DevelopmentPlan

    box = make_sandbox(tmp_path, project=PROJECT, repository=REPO)
    llm = scoped_llm(fake_llm)
    llm.register(TaskClassification, low_risk())
    llm.register(DevelopmentPlan, plan())
    llm.register(ArchitectureReview, review(True))
    verifier = JwtVerifier(
        StaticKeys(issuer.jwks), VerificationRules(issuer=issuer.issuer, audience=issuer.audience)
    )
    app = create_app(
        llm,
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
        router=SmartRouter(llm),
    )
    with TestClient(app) as client:
        yield client


def bearer(issuer: DevIssuer, subject: str, roles: list[str]) -> dict[str, str]:
    return {"Authorization": "Bearer " + issuer.token(subject, roles=roles, projects=[PROJECT])}


def test_route_mode_is_accepted_bounded_and_reported(client: TestClient, issuer: DevIssuer):
    olivia = bearer(issuer, "olivia", ["project_operator"])
    auto = client.post(RUNS, json=BODY, headers=olivia).json()
    assert (
        auto["route"] == "fast"
        and auto["route_mode"] == "auto"
        and auto["pending_stage"] == "scope"
    )
    full = client.post(RUNS, json={**BODY, "route_mode": "full"}, headers=olivia).json()
    assert (
        full["route"] == "full" and full["route_mode"] == "full" and full["pending_stage"] == "plan"
    )
    pending = client.get(f"{RUNS}/{full['run_id']}/decisions", headers=olivia).json()["pending"]
    assert pending["stage"] == "plan" and pending["proposal"]["plan"]["summary"] == "plan"
    risky = client.post(
        RUNS,
        json={
            **BODY,
            "task": "Añade una migración Alembic de la tabla usuarios y cambia los permisos",
            "route_mode": "fast",
        },
        headers=olivia,
    ).json()
    assert risky["route"] == "full" and risky["route_mode"] == "fast"  # mandatory rules win
    bad = client.post(RUNS, json={**BODY, "route_mode": "turbo"}, headers=olivia)
    assert bad.status_code == 422
    shown = client.get(f"{RUNS}/{auto['run_id']}", headers=olivia).json()
    assert shown["route"] == "fast" and shown["route_mode"] == "auto"

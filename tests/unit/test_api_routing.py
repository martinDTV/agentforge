"""route_mode through the API and the route in the response (fakes only)."""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

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
from tests.conftest import settle
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
    auto = settle(client, auto["run_id"], headers=olivia)
    assert (
        auto["route"] == "fast"
        and auto["route_mode"] == "auto"
        and auto["pending_stage"] == "scope"
    )
    full = client.post(RUNS, json={**BODY, "route_mode": "full"}, headers=olivia).json()
    full = settle(client, full["run_id"], headers=olivia)
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
    risky = settle(client, risky["run_id"], headers=olivia)
    assert risky["route"] == "full" and risky["route_mode"] == "fast"  # mandatory rules win
    bad = client.post(RUNS, json={**BODY, "route_mode": "turbo"}, headers=olivia)
    assert bad.status_code == 422
    shown = client.get(f"{RUNS}/{auto['run_id']}", headers=olivia).json()
    assert shown["route"] == "fast" and shown["route_mode"] == "auto"


class GatedClassifier:
    """Blocks the classification call until released; signals entry first."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate_structured(self, system_prompt: str, user_prompt: str, schema: Any) -> Any:
        if schema is TaskClassification:
            self.entered.set()
            assert self.release.wait(10), "la prueba no liberó al clasificador"
        return self.inner.generate_structured(system_prompt, user_prompt, schema)


def test_the_api_answers_while_the_classifier_is_thinking(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer, tmp_path: Path
):
    """I0.2: a synchronous model call inside the router must not hold the event loop;
    liveness and run reads are served while it runs (bounded, no fixed sleeps)."""
    from app.contracts.planning import ArchitectureReview, DevelopmentPlan

    box = make_sandbox(tmp_path, project=PROJECT, repository=REPO)
    llm = scoped_llm(fake_llm)
    llm.register(TaskClassification, low_risk())
    llm.register(DevelopmentPlan, plan())
    llm.register(ArchitectureReview, review(True))
    gated = GatedClassifier(llm)
    verifier = JwtVerifier(
        StaticKeys(issuer.jwks), VerificationRules(issuer=issuer.issuer, audience=issuer.audience)
    )
    app = create_app(
        gated,
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
        router=SmartRouter(gated),
    )
    headers = bearer(issuer, "olivia", ["project_operator"])
    body = {
        "task": "Añade greet en src/app.py y pruebas",
        "project_id": PROJECT,
        "workflow_kind": "development",
        "repository_id": REPO,
    }
    with TestClient(app) as client:
        created = client.post(RUNS, json=body, headers=headers)
        assert created.status_code == 201 and created.json()["status"] == "running"
        run_id = created.json()["run_id"]
        assert gated.entered.wait(10)
        with ThreadPoolExecutor(max_workers=1) as pool:
            live = pool.submit(lambda: client.get("/health/live")).result(timeout=5)
            detail = pool.submit(lambda: client.get(f"{RUNS}/{run_id}", headers=headers)).result(
                timeout=5
            )
        assert live.status_code == 200 and detail.json()["status"] == "running"
        assert not gated.release.is_set()
        gated.release.set()
        assert settle(client, run_id, headers=headers)["status"] == "waiting_approval"

"""Closing review of Fase 4: every business route is protected, authorization lives
outside routers, access revocation and cursors cannot leak, docs are off in
production, security headers are present, sweeps never stack."""

import asyncio
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.contracts.planning import DevelopmentPlan
from app.infrastructure.db import InMemoryRunRepository
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.main import create_app
from app.orchestration.exceptions import SweepInProgressError
from app.orchestration.limits import RunLimits
from app.orchestration.policy import RecoveryPolicy
from app.orchestration.recovery import RecoveryService
from app.security import AuthorizationPolicy, JwtVerifier, VerificationRules
from app.security.dev_keys import DevIssuer
from app.security.tokens import StaticKeys
from app.settings import get_environment
from tests.unit.test_api_control import abandoned_run, bearer

T = TypeVar("T", bound=BaseModel)
RUNS = "/api/v1/runs"
TASK = {"task": "Crear módulo de pacientes con pruebas"}
APP_DIR = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture
def issuer() -> DevIssuer:
    return DevIssuer()


@pytest.fixture
def repository() -> InMemoryRunRepository:
    return InMemoryRunRepository()


def make_app(
    fake_llm: Any,
    issuer: DevIssuer,
    repository: InMemoryRunRepository,
    *,
    policy: RecoveryPolicy | None = None,
) -> Any:
    verifier = JwtVerifier(
        StaticKeys(issuer.jwks), VerificationRules(issuer=issuer.issuer, audience=issuer.audience)
    )
    return create_app(
        fake_llm,
        RunLimits(),
        repository=repository,
        token_verifier=verifier,
        authorization=AuthorizationPolicy(separation_of_duties=False),
        policy=policy or RecoveryPolicy(lease_ttl_seconds=5, heartbeat_seconds=1),
    )


# -- every business route requires authentication ------------------------------


def test_every_api_route_rejects_anonymous_requests(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer, repository: InMemoryRunRepository
):
    app = make_app(fake_llm, issuer, repository)
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()
        business = [
            (m.upper(), p) for p, ops in spec["paths"].items() if p.startswith("/api/") for m in ops
        ]
        assert len(business) >= 6
        for method, path in business:
            url = path.replace("{run_id}", str(uuid.uuid4()))
            response = client.request(method, url, json=TASK if method == "POST" else None)
            assert response.status_code == 401, (method, path, response.text)
            assert response.headers["WWW-Authenticate"].startswith("Bearer")
            assert spec["paths"][path][method.lower()].get("security"), (method, path)
        assert not any(
            p.startswith("/health") for p in spec["paths"]
        )  # probes are not business routes


def test_routers_never_decide_authorization():
    """Permissions are evaluated in services; routers only pass the principal."""
    forbidden = re.compile(
        r"AuthorizationPolicy|Permission\.|\.decide\(|can_see|listing_scope|sweep_scope"
    )
    for module in ("runs.py", "dependencies.py", "health.py", "errors.py"):
        source = (APP_DIR / "api" / module).read_text()
        assert not forbidden.search(source), module
    admin = (APP_DIR / "api" / "admin.py").read_text()
    assert (
        "sweep_scope" in admin and ".decide(" not in admin
    )  # scope resolution delegated to the policy
    service = (APP_DIR / "orchestration" / "runs.py").read_text()
    assert service.count("self._authz.") >= 6


# -- revocation and cursors -------------------------------------------------------


def test_owner_loses_visibility_when_project_access_is_revoked(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer, repository: InMemoryRunRepository
):
    with TestClient(make_app(fake_llm, issuer, repository)) as client:
        alice = bearer(issuer, "alice", ["operator"], ["alpha"])
        run_id = client.post(RUNS, json=TASK, headers=alice).json()["run_id"]
        revoked = bearer(issuer, "alice", ["operator"], [])  # same subject, membership gone
        assert client.get(f"{RUNS}/{run_id}", headers=revoked).status_code == 404
        assert client.get(RUNS, headers=revoked).json()["items"] == []
        assert (
            client.post(
                f"{RUNS}/{run_id}/approve", json={"approved": True}, headers=revoked
            ).status_code
            == 404
        )
        moved = bearer(issuer, "alice", ["operator"], ["beta"])
        assert client.get(f"{RUNS}/{run_id}", headers=moved).status_code == 404


def test_permissions_changing_between_two_pages_never_leak_rows(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer, repository: InMemoryRunRepository
):
    with TestClient(make_app(fake_llm, issuer, repository)) as client:
        alice = bearer(issuer, "alice", ["operator"], ["alpha"])
        for _ in range(5):
            client.post(RUNS, json=TASK, headers=alice)
        first = client.get(RUNS, headers=alice, params={"limit": 2}).json()
        assert len(first["items"]) == 2 and first["next_cursor"]
        # Between pages the token loses alpha: the same cursor now yields nothing.
        demoted = bearer(issuer, "alice", ["operator"], [])
        second = client.get(
            RUNS, headers=demoted, params={"limit": 2, "cursor": first["next_cursor"]}
        )
        assert second.status_code == 200 and second.json()["items"] == []
        # Losing runs:read keeps only own rows (they are all Alice's here, so still 2).
        owner_only = bearer(issuer, "alice", [], ["alpha"])
        page = client.get(
            RUNS, headers=owner_only, params={"limit": 2, "cursor": first["next_cursor"]}
        ).json()
        assert len(page["items"]) == 2 and all(i["owner"] == "alice" for i in page["items"])


def test_cursors_cannot_cross_subjects_or_projects(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer, repository: InMemoryRunRepository
):
    with TestClient(make_app(fake_llm, issuer, repository)) as client:
        alice = bearer(issuer, "alice", ["operator"], ["alpha"])
        carol = bearer(issuer, "carol", ["operator"], ["beta"])
        for _ in range(3):
            client.post(RUNS, json=TASK, headers=alice)
            client.post(RUNS, json=TASK, headers=carol)
        root = bearer(issuer, "root", ["platform_admin"], [])
        global_cursor = client.get(RUNS, headers=root, params={"limit": 2}).json()["next_cursor"]
        assert global_cursor
        # A member of alpha replaying the platform admin's cursor: bound to the subject -> 400.
        replay = client.get(RUNS, headers=alice, params={"limit": 2, "cursor": global_cursor})
        assert replay.status_code == 400 and replay.json()["code"] == "invalid_cursor"
        # Alice's own cursor with a foreign project filter: different filters -> 400,
        # and a fresh query for beta returns nothing at all.
        own = client.get(RUNS, headers=alice, params={"limit": 2}).json()["next_cursor"]
        assert (
            client.get(
                RUNS, headers=alice, params={"limit": 2, "cursor": own, "project_id": "beta"}
            ).status_code
            == 400
        )
        assert client.get(RUNS, headers=alice, params={"project_id": "beta"}).json()["items"] == []


# -- docs and headers -----------------------------------------------------------


def test_docs_are_disabled_in_production_like_environments(monkeypatch: pytest.MonkeyPatch):
    for environment, expected in (("production", None), ("staging", None), ("local", "/docs")):
        monkeypatch.setenv("ENVIRONMENT", environment)
        get_environment.cache_clear()
        app = create_app()  # production wiring; lifespan (and Settings) never run here
        assert app.docs_url == expected and (app.openapi_url is None) == (expected is None)
    get_environment.cache_clear()


def test_security_headers_are_present_without_breaking_docs(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer, repository: InMemoryRunRepository
):
    with TestClient(make_app(fake_llm, issuer, repository)) as client:
        api = client.get(
            f"{RUNS}/{uuid.uuid4()}", headers=bearer(issuer, "alice", ["viewer"], ["alpha"])
        )
        assert api.status_code == 404
        assert api.headers["x-content-type-options"] == "nosniff"
        assert api.headers["cache-control"] == "no-store"
        assert api.headers["referrer-policy"] == "no-referrer"
        assert api.headers["x-frame-options"] == "DENY"
        assert "strict-transport-security" not in api.headers  # local: no TLS assumption
        docs = client.get("/docs")
        assert docs.status_code == 200 and "swagger" in docs.text.lower()
        assert docs.headers["x-content-type-options"] == "nosniff"
        assert "cache-control" not in docs.headers or docs.headers["cache-control"] != "no-store"
        health = client.get("/health/live")
        assert health.headers["x-content-type-options"] == "nosniff"


def test_hsts_only_in_production_like_environments(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_environment.cache_clear()
    app = create_app()
    get_environment.cache_clear()
    middleware = next(
        m for m in app.user_middleware if m.cls.__name__ == "SecurityHeadersMiddleware"
    )
    assert middleware.kwargs == {"hsts": True}


# -- sweeps never stack ---------------------------------------------------------------


class GatedLLM:
    def __init__(self, inner: FakeLLMAdapter) -> None:
        self.inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate_structured(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        if schema is not DevelopmentPlan:  # the resumed run continues at the architect
            self.entered.set()
            assert self.release.wait(15)
        return self.inner.generate_structured(system_prompt, user_prompt, schema)


def test_manual_sweeps_fail_fast_while_another_sweep_runs(
    fake_llm: FakeLLMAdapter, issuer: DevIssuer, repository: InMemoryRunRepository
):
    gated = GatedLLM(fake_llm)
    policy = RecoveryPolicy(
        lease_ttl_seconds=5, heartbeat_seconds=1, auto_resume=True, max_attempts=3
    )
    app = make_app(gated, issuer, repository, policy=policy)
    root = bearer(issuer, "root", ["platform_admin"], [])
    with TestClient(app) as client:
        abandoned_run(app, repository, fake_llm, owner="alice", project="alpha")
        with ThreadPoolExecutor(max_workers=1) as pool:
            long_sweep = pool.submit(
                lambda: client.post("/api/v1/admin/recovery/sweeps", headers=root)
            )
            assert gated.entered.wait(15)  # the first sweep is re-driving a run
            assert app.state.recovery.sweeping is True
            busy = client.post("/api/v1/admin/recovery/sweeps", headers=root)
            assert busy.status_code == 409 and busy.json()["code"] == "sweep_in_progress"
            gated.release.set()
            done = long_sweep.result(timeout=15)
        assert done.status_code == 200 and done.json()["outcomes"] == {"resumed": 1}
        assert app.state.recovery.sweeping is False
        assert client.post("/api/v1/admin/recovery/sweeps", headers=root).json()["claimed"] == 0


@pytest.mark.anyio
async def test_recovery_service_lock_semantics(fake_llm: FakeLLMAdapter):
    from app.orchestration.graph import build_graph
    from app.orchestration.runs import RunService

    service = RunService(
        build_graph(fake_llm), InMemoryRunRepository(), authorization=AuthorizationPolicy()
    )
    recovery = RecoveryService(service)
    async with recovery._lock:
        with pytest.raises(SweepInProgressError):
            await recovery.sweep(wait=False)
        waiter = asyncio.create_task(recovery.sweep())  # wait=True queues behind the lock
        await asyncio.sleep(0.01)
        assert not waiter.done()
    assert (await waiter).claimed == 0

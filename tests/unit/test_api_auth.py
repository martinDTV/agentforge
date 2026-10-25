"""Every business route requires a verified bearer token; health does not."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.infrastructure.llm.fake import FakeLLMAdapter
from app.main import create_app
from app.orchestration.limits import RunLimits
from app.security import JwtVerifier, VerificationRules
from app.security.dev_keys import DevIssuer
from app.security.tokens import StaticKeys

RUNS = "/api/v1/runs"
TASK = {"task": "Crear módulo de pacientes con pruebas"}
PROBLEM_JSON = "application/problem+json"


@pytest.fixture
def issuer() -> DevIssuer:
    return DevIssuer()


@pytest.fixture
def client(fake_llm: FakeLLMAdapter, issuer: DevIssuer) -> Iterator[TestClient]:
    verifier = JwtVerifier(
        StaticKeys(issuer.jwks), VerificationRules(issuer=issuer.issuer, audience=issuer.audience)
    )
    with TestClient(create_app(fake_llm, RunLimits(), token_verifier=verifier)) as client:
        yield client


def bearer(issuer: DevIssuer, **kwargs: object) -> dict[str, str]:
    claims: dict[str, object] = {"roles": ["operator", "approver"], "projects": ["alpha"], **kwargs}
    return {"Authorization": f"Bearer {issuer.token('user-1', **claims)}"}  # type: ignore[arg-type]


ROUTES = [
    ("POST", RUNS, TASK),
    ("GET", f"{RUNS}/{uuid.uuid4()}", None),
    ("POST", f"{RUNS}/{uuid.uuid4()}/approve", {"approved": True}),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=["create", "get", "approve"])
def test_routes_reject_requests_without_a_token(
    client: TestClient, method: str, path: str, body: dict[str, object] | None
):
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["code"] == "unauthenticated"


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Bearer "},
    ],
    ids=["basic", "garbage", "empty"],
)
def test_bad_credentials_are_401(client: TestClient, header: dict[str, str]):
    response = client.get(f"{RUNS}/{uuid.uuid4()}", headers=header)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_invalid_and_expired_tokens_carry_an_error_challenge(client: TestClient, issuer: DevIssuer):
    wrong_audience = client.get(f"{RUNS}/{uuid.uuid4()}", headers=bearer(issuer, audience="other"))
    assert wrong_audience.status_code == 401
    assert wrong_audience.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    expired = client.get(
        f"{RUNS}/{uuid.uuid4()}", headers=bearer(issuer, ttl_seconds=1, issued_at=1)
    )
    assert expired.status_code == 401
    assert expired.json()["detail"] == "El token ha expirado."
    assert "eyJ" not in expired.text  # the token is never echoed back


def test_health_probes_stay_open(client: TestClient):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code in (200, 503)  # no DB in unit tests


def test_a_valid_token_allows_the_full_flow(client: TestClient, issuer: DevIssuer):
    headers = bearer(issuer)
    created = client.post(RUNS, json=TASK, headers=headers)
    assert created.status_code == 201
    run_id = created.json()["run_id"]
    assert client.get(f"{RUNS}/{run_id}", headers=headers).status_code == 200
    approved = client.post(f"{RUNS}/{run_id}/approve", json={"approved": True}, headers=headers)
    assert approved.json()["status"] == "approved"
    # Existing error contract is intact behind authentication.
    missing = client.get(f"{RUNS}/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404 and missing.json()["code"] == "run_not_found"


def test_openapi_declares_bearer_security_on_business_routes(client: TestClient):
    spec = client.get("/openapi.json").json()
    assert "HTTPBearer" in spec["components"]["securitySchemes"]
    for path, operations in spec["paths"].items():
        if path.startswith("/api/v1"):
            for operation in operations.values():
                assert operation.get("security"), f"{path} sin security"


def test_apps_without_an_injected_verifier_run_as_local_admin(fake_llm: FakeLLMAdapter):
    """Test convenience only: production builds its verifier from Settings."""
    with TestClient(create_app(fake_llm, RunLimits())) as client:
        assert client.post(RUNS, json=TASK).status_code == 201

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.contracts.planning import ArchitectureReview, DevelopmentPlan
from app.infrastructure.llm.errors import LLMTimeoutError
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.main import create_app
from app.orchestration.limits import RunLimits

RUNS = "/api/v1/runs"
TASK = {"task": "Crear módulo de pacientes con pruebas"}
PROBLEM_JSON = "application/problem+json"


@pytest.fixture
def client(fake_llm: FakeLLMAdapter) -> Iterator[TestClient]:
    # The context manager runs ``lifespan``, which is where the graph is wired.
    with TestClient(create_app(fake_llm, RunLimits())) as client:
        yield client


def test_create_run_reaches_waiting_approval(client: TestClient):
    response = client.post(RUNS, json=TASK)
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "waiting_approval"
    assert payload["plan"]["summary"]
    assert payload["architecture"]["summary"]
    assert payload["revision"] == 0
    assert payload["llm_calls"] == 2
    assert payload["architect_approved"] is True
    assert payload["error"] is None


def test_approval_flow(client: TestClient):
    run_id = client.post(RUNS, json=TASK).json()["run_id"]

    result = client.post(f"{RUNS}/{run_id}/approve", json={"approved": True})
    assert result.status_code == 200
    assert result.json()["status"] == "approved"

    again = client.post(f"{RUNS}/{run_id}/approve", json={"approved": False})
    assert again.status_code == 409
    assert again.headers["content-type"] == PROBLEM_JSON
    assert again.json()["code"] == "run_already_decided"
    assert again.json()["status"] == 409


def test_rejection_flow(client: TestClient):
    run_id = client.post(RUNS, json=TASK).json()["run_id"]
    result = client.post(f"{RUNS}/{run_id}/approve", json={"approved": False})
    assert result.json()["status"] == "rejected"


def test_get_unknown_run_returns_problem_details_404(client: TestClient):
    response = client.get(f"{RUNS}/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert body["code"] == "run_not_found"
    assert body["title"] == "RunNotFoundError"
    assert "does-not-exist" in body["detail"]


def test_approve_unknown_run_returns_404(client: TestClient):
    response = client.post(f"{RUNS}/does-not-exist/approve", json={"approved": True})
    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def test_create_run_rejects_short_task(client: TestClient):
    response = client.post(RUNS, json={"task": "corto"})
    assert response.status_code == 422


def test_failed_run_is_created_and_cannot_be_approved(client: TestClient, fake_llm: FakeLLMAdapter):
    fake_llm.register_sequence(DevelopmentPlan, [LLMTimeoutError("sin respuesta")])

    created = client.post(RUNS, json=TASK)
    assert created.status_code == 201
    payload = created.json()
    assert payload["status"] == "failed"
    assert payload["error"] == {"code": "llm_timeout", "message": "sin respuesta"}
    assert payload["plan"] is None
    assert payload["architecture"] is None

    run_id = payload["run_id"]
    assert client.get(f"{RUNS}/{run_id}").json() == payload

    approve = client.post(f"{RUNS}/{run_id}/approve", json={"approved": True})
    assert approve.status_code == 409
    assert approve.json()["code"] == "run_already_decided"
    assert "failed" in approve.json()["detail"]


def test_limits_are_injected_into_the_graph(
    fake_llm: FakeLLMAdapter, rejecting_review: ArchitectureReview
):
    fake_llm.register(ArchitectureReview, rejecting_review)
    with TestClient(create_app(fake_llm, RunLimits(max_revisions=1, max_llm_calls=10))) as client:
        payload = client.post(RUNS, json=TASK).json()

    assert payload["status"] == "waiting_approval"
    assert payload["architect_approved"] is False
    assert payload["revision"] == 1
    assert payload["llm_calls"] == 4
    assert payload["architecture"]["issues"] == ["Falta manejo de errores"]


def test_unexpected_error_returns_problem_details_500(fake_llm: FakeLLMAdapter):
    fake_llm.register_sequence(DevelopmentPlan, [RuntimeError("boom")])
    app = create_app(fake_llm, RunLimits())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(RUNS, json=TASK)

    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json()["code"] == "internal_error"
    assert "boom" not in response.text


def test_api_is_versioned(client: TestClient):
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) == {RUNS, f"{RUNS}/{{run_id}}", f"{RUNS}/{{run_id}}/approve"}
    assert client.post("/runs", json=TASK).status_code == 404

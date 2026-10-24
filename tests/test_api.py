import pytest
from fastapi.testclient import TestClient

from app.infrastructure.llm.fake import FakeLLMAdapter
from app.main import create_app


@pytest.fixture
def client(fake_llm: FakeLLMAdapter) -> TestClient:
    return TestClient(create_app(fake_llm))


def test_create_run_reaches_waiting_approval(client: TestClient):
    response = client.post("/runs", json={"task": "Crear módulo de pacientes con pruebas"})
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "waiting_approval"
    assert payload["plan"]["summary"]
    assert payload["architecture"]["summary"]


def test_approval_flow(client: TestClient):
    run_id = client.post("/runs", json={"task": "Crear módulo de pacientes con pruebas"}).json()[
        "run_id"
    ]
    result = client.post(f"/runs/{run_id}/approve", json={"approved": True})
    assert result.status_code == 200
    assert result.json()["status"] == "approved"
    assert client.post(f"/runs/{run_id}/approve", json={"approved": False}).status_code == 409


def test_rejection_flow(client: TestClient):
    run_id = client.post("/runs", json={"task": "Diseñar sistema de auditoría y eventos"}).json()[
        "run_id"
    ]
    result = client.post(f"/runs/{run_id}/approve", json={"approved": False})
    assert result.json()["status"] == "rejected"


def test_get_unknown_run_returns_404(client: TestClient):
    response = client.get("/runs/does-not-exist")
    assert response.status_code == 404


def test_create_run_rejects_short_task(client: TestClient):
    response = client.post("/runs", json={"task": "corto"})
    assert response.status_code == 422

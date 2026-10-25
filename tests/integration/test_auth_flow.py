"""Authenticated end-to-end flow against PostgreSQL (tokens from the dev issuer)."""

import uuid

from fastapi.testclient import TestClient

from app.infrastructure.llm.fake import FakeLLMAdapter
from app.security.dev_keys import DevIssuer
from tests.integration.conftest import make_app

RUNS = "/api/v1/runs"
TASK = {"task": "Crear módulo de pacientes con pruebas"}


def test_persistent_app_requires_tokens_and_serves_the_full_flow(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    issuer = DevIssuer()
    token = issuer.token("user-1", roles=["operator", "approver"], projects=["default"])
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(make_app(fake_llm, checkpoint_database_url, issuer=issuer)) as client:
        assert client.post(RUNS, json=TASK).status_code == 401
        assert client.get("/health/ready").status_code == 200  # probes stay open
        created = client.post(RUNS, json=TASK, headers=headers)
        assert created.status_code == 201
        run_id = created.json()["run_id"]
        assert client.get(f"{RUNS}/{run_id}").status_code == 401
        assert (
            client.get(f"{RUNS}/{run_id}", headers=headers).json()["status"] == "waiting_approval"
        )
        replay = client.post(RUNS, json=TASK, headers={**headers, "Idempotency-Key": run_id})
        assert replay.status_code == 201  # new key: a new run, still authenticated
        approved = client.post(f"{RUNS}/{run_id}/approve", json={"approved": True}, headers=headers)
        assert approved.json()["status"] == "approved"
    # A second instance with a different key set rejects the old tokens.
    with TestClient(make_app(fake_llm, checkpoint_database_url, issuer=DevIssuer())) as other:
        assert other.get(f"{RUNS}/{run_id}", headers=headers).status_code == 401
        assert other.get(f"{RUNS}/{uuid.uuid4()}", headers=headers).status_code == 401

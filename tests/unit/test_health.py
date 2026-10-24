from fastapi.testclient import TestClient

from app.infrastructure.llm.fake import FakeLLMAdapter
from app.main import create_app
from app.orchestration.limits import RunLimits


def test_live_does_not_need_a_database(fake_llm: FakeLLMAdapter):
    with TestClient(create_app(fake_llm, RunLimits())) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_unconfigured_database_as_unavailable(fake_llm: FakeLLMAdapter):
    with TestClient(create_app(fake_llm, RunLimits())) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "not_configured"}


def test_health_routes_are_not_versioned_nor_in_the_schema(fake_llm: FakeLLMAdapter):
    with TestClient(create_app(fake_llm, RunLimits())) as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert client.get("/api/v1/health/live").status_code == 404
    assert not any(path.startswith("/health") for path in paths)


def test_docs_can_be_disabled_for_production(fake_llm: FakeLLMAdapter):
    with TestClient(create_app(fake_llm, RunLimits(), docs_enabled=False)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/health/live").status_code == 200
    with TestClient(create_app(fake_llm, RunLimits())) as client:
        assert client.get("/docs").status_code == 200

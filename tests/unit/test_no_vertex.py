"""The whole pytest run is fenced off from Vertex AI (see tests/conftest.py)."""

import anthropic
import pytest

from app.infrastructure.llm import build_llm
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.settings import Settings


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "anthropic_vertex_project_id": "test-project",
        "database_url": "postgresql+psycopg://localhost/agentforge",
        "auth_mode": "disabled",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_real_vertex_client_cannot_be_constructed_during_tests():
    with pytest.raises(RuntimeError, match="Vertex AI está bloqueado"):
        anthropic.AnthropicVertex(project_id="test-project", region="global")


def test_build_llm_with_fake_model_never_touches_vertex():
    assert isinstance(build_llm(make_settings(use_fake_model=True)), FakeLLMAdapter)


def test_build_llm_real_path_is_blocked_during_tests():
    with pytest.raises(RuntimeError, match="Vertex AI está bloqueado"):
        build_llm(make_settings(use_fake_model=False))

import pytest
from pydantic import ValidationError

from app.settings import Settings


def make(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        anthropic_vertex_project_id="test-project",
        **overrides,  # type: ignore[arg-type]
    )


def test_defaults_are_sane():
    settings = make()
    assert settings.llm_timeout_seconds > 0
    assert settings.llm_max_repair_attempts >= 0
    assert settings.max_llm_calls_per_run >= 2 * (1 + settings.max_plan_revisions)


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_timeout_seconds": 0},
        {"llm_max_retries": -1},
        {"llm_max_repair_attempts": -1},
        {"max_plan_revisions": -1},
        {"max_llm_calls_per_run": 0},
    ],
)
def test_invalid_limits_are_rejected(overrides: dict[str, object]):
    with pytest.raises(ValidationError):
        make(**overrides)


def test_project_id_is_required(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]

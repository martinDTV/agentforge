import os

import pytest
from pydantic import ValidationError

from app.settings import Settings

DB_URL = "postgresql+psycopg://user:s3cret@localhost:5432/agentforge"
_PREFIXES = (
    "ENVIRONMENT",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLOUD_ML_REGION",
    "CLAUDE_MODEL",
    "USE_FAKE_MODEL",
    "LLM_",
    "MAX_",
    "DATABASE_",
    "CHECKPOINT_",
    "SHUTDOWN_",
    "RUN_",
    "RECOVERY_",
)


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings tests must not depend on the shell or the CI job environment."""
    for name in list(os.environ):
        if name.startswith(_PREFIXES):
            monkeypatch.delenv(name, raising=False)


def make(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "anthropic_vertex_project_id": "test-project",
        "database_url": DB_URL,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_defaults_are_sane():
    settings = make()
    assert settings.environment == "local"
    assert settings.llm_timeout_seconds > 0
    assert settings.llm_max_repair_attempts >= 0
    assert settings.max_llm_calls_per_run >= 2 * (1 + settings.max_plan_revisions)
    assert settings.database_pool_size >= 1
    assert settings.database_ssl is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_timeout_seconds": 0},
        {"llm_max_retries": -1},
        {"llm_max_repair_attempts": -1},
        {"max_plan_revisions": -1},
        {"max_llm_calls_per_run": 0},
        {"run_lease_ttl_seconds": 0},
        {"run_heartbeat_seconds": 0},
        {"run_lease_ttl_seconds": 10, "run_heartbeat_seconds": 6},
        {"recovery_sweep_seconds": -1},
        {"recovery_batch_size": 0},
        {"max_run_attempts": 0},
        {"database_pool_size": 0},
        {"database_max_overflow": -1},
        {"database_pool_timeout_seconds": 0},
        {"database_connect_timeout_seconds": 0},
        {"database_statement_timeout_ms": -1},
        {"environment": "qa"},
    ],
)
def test_invalid_values_are_rejected(overrides: dict[str, object]):
    with pytest.raises(ValidationError):
        make(**overrides)


def test_project_id_is_required():
    with pytest.raises(ValidationError, match="anthropic_vertex_project_id"):
        Settings(_env_file=None, database_url=DB_URL)  # type: ignore[call-arg]


def test_database_url_is_required():
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None, anthropic_vertex_project_id="p")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://user:s3cret@localhost:5432/agentforge",
        "postgres://user:s3cret@localhost:5432/agentforge",
        DB_URL,
    ],
)
def test_database_url_aliases_are_normalised_to_psycopg(raw: str):
    assert make(database_url=raw).database_url.get_secret_value() == DB_URL


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql+asyncpg://user:pw@localhost/db",
        "postgresql+psycopg2://user:pw@localhost/db",
        "mysql://user:pw@localhost/db",
        "localhost:5432/db",
    ],
)
def test_database_url_rejects_other_drivers(raw: str):
    with pytest.raises(ValidationError, match="psycopg"):
        make(database_url=raw)


def test_database_dsn_is_libpq_form():
    assert make().database_dsn.get_secret_value() == (
        "postgresql://user:s3cret@localhost:5432/agentforge"
    )


def test_database_password_is_not_leaked_by_repr():
    settings = make()
    assert "s3cret" not in repr(settings)
    assert "s3cret" not in str(settings)
    assert "s3cret" not in repr(settings.database_dsn)


@pytest.mark.parametrize(
    ("environment", "explicit", "expected"),
    [
        ("local", None, False),
        ("test", None, False),
        ("staging", None, True),
        ("production", None, True),
        ("production", False, False),
        ("local", True, True),
    ],
)
def test_database_ssl_is_derived_from_environment_unless_explicit(
    environment: str, explicit: bool | None, expected: bool
):
    settings = make(environment=environment, database_ssl_required=explicit)
    assert settings.database_ssl is expected
    assert settings.is_production_like is (environment in ("staging", "production"))


@pytest.mark.parametrize("key", ["0123456789abcdef", "0" * 24, "x" * 32])
def test_checkpoint_encryption_key_accepts_valid_aes_lengths(key: str):
    settings = make(checkpoint_encryption_key=key)
    assert settings.checkpoint_encryption_key_bytes == key.encode()
    assert key not in repr(settings)


@pytest.mark.parametrize("key", ["short", "0" * 31, "ñ" * 10])
def test_checkpoint_encryption_key_rejects_other_lengths(key: str):
    with pytest.raises(ValidationError, match="16, 24 o 32"):
        make(checkpoint_encryption_key=key)


def test_checkpoint_encryption_key_empty_means_disabled():
    assert make(checkpoint_encryption_key="").checkpoint_encryption_key_bytes is None
    assert make().checkpoint_encryption_key_bytes is None


def test_recovery_defaults_are_conservative():
    settings = make()
    assert settings.recovery_auto_resume is False
    assert settings.run_lease_ttl_seconds >= 3 * (settings.run_heartbeat_seconds or 0)
    assert settings.max_run_attempts >= 1

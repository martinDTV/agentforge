from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]

SQLALCHEMY_SCHEME = "postgresql+psycopg://"
LIBPQ_SCHEME = "postgresql://"


class EnvironmentSettings(BaseSettings):
    """The single setting needed at import time.

    ``create_app()`` must stay importable by tests that never configure the
    service, so the only thing it reads eagerly is the environment (it decides
    whether ``/docs`` is exposed) and that read can never fail validation.
    Everything else is validated in ``lifespan``, where a missing variable
    aborts startup.
    """

    environment: Environment = "local"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def is_production_like(self) -> bool:
        return self.environment in ("staging", "production")


class Settings(EnvironmentSettings):
    anthropic_vertex_project_id: str
    cloud_ml_region: str = "global"
    claude_model: str = "claude-sonnet-4-6"
    use_fake_model: bool = False

    # LLM call resilience (applied inside VertexClaudeAdapter).
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    llm_max_repair_attempts: int = Field(default=1, ge=0)

    # Per-run orchestration limits (mapped to RunLimits in create_app).
    max_plan_revisions: int = Field(default=2, ge=0)
    max_llm_calls_per_run: int = Field(default=8, ge=1)

    # PostgreSQL (business tables; the LangGraph checkpointer reuses the same DSN).
    database_url: SecretStr
    database_pool_size: int = Field(default=5, ge=1)
    database_max_overflow: int = Field(default=5, ge=0)
    database_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    database_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    database_statement_timeout_ms: int = Field(default=30_000, ge=0)
    # None = derive from environment (required outside local/test).
    database_ssl_required: bool | None = None

    # LangGraph checkpointer (AsyncPostgresSaver): same database, its own pool.
    checkpoint_pool_size: int = Field(default=5, ge=1)
    # 16, 24 or 32 bytes => AES-EAX at rest on top of the strict serializer. None = plaintext.
    checkpoint_encryption_key: SecretStr | None = None
    # How long shutdown waits for in-flight runs before closing pools.
    shutdown_grace_seconds: float = Field(default=30.0, ge=0)

    # Execution leases and recovery (ADR 0004).
    run_lease_ttl_seconds: float = Field(default=60.0, gt=0)
    # None = a third of the TTL.
    run_heartbeat_seconds: float | None = Field(default=None, gt=0)
    # 0 disables the periodic sweep; the startup sweep always runs.
    recovery_sweep_seconds: float = Field(default=60.0, ge=0)
    recovery_batch_size: int = Field(default=20, ge=1)
    # Re-drive interrupted runs from their last checkpoint automatically (costs LLM calls).
    recovery_auto_resume: bool = False
    max_run_attempts: int = Field(default=3, ge=1)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalise_database_url(cls, value: object) -> object:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str):
            return value
        for alias in ("postgres://", LIBPQ_SCHEME):
            if raw.startswith(alias):
                return SQLALCHEMY_SCHEME + raw[len(alias) :]
        if not raw.startswith(SQLALCHEMY_SCHEME):
            raise ValueError(
                "DATABASE_URL debe usar el esquema postgresql+psycopg:// "
                "(psycopg 3 es el único driver soportado)"
            )
        return raw

    @field_validator("checkpoint_encryption_key")
    @classmethod
    def _validate_encryption_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or value.get_secret_value() == "":
            return None
        if len(value.get_secret_value().encode()) not in (16, 24, 32):
            raise ValueError("CHECKPOINT_ENCRYPTION_KEY debe tener 16, 24 o 32 bytes")
        return value

    @property
    def checkpoint_encryption_key_bytes(self) -> bytes | None:
        if self.checkpoint_encryption_key is None:
            return None
        return self.checkpoint_encryption_key.get_secret_value().encode()

    @model_validator(mode="after")
    def _heartbeat_fits_in_lease(self) -> "Settings":
        if (
            self.run_heartbeat_seconds is not None
            and self.run_heartbeat_seconds * 2 > self.run_lease_ttl_seconds
        ):
            raise ValueError(
                "RUN_HEARTBEAT_SECONDS debe ser como mucho la mitad de RUN_LEASE_TTL_SECONDS"
            )
        return self

    @property
    def database_ssl(self) -> bool:
        if self.database_ssl_required is not None:
            return self.database_ssl_required
        return self.is_production_like

    @property
    def database_dsn(self) -> SecretStr:
        """libpq form of DATABASE_URL (``postgresql://``), for raw psycopg clients."""
        url = self.database_url.get_secret_value()
        return SecretStr(LIBPQ_SCHEME + url[len(SQLALCHEMY_SCHEME) :])


@lru_cache
def get_environment() -> EnvironmentSettings:
    return EnvironmentSettings()


@lru_cache
def get_settings() -> Settings:
    return Settings()

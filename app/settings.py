from functools import lru_cache
from pathlib import Path
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


class DatabaseSettings(EnvironmentSettings):
    """Everything the deploy steps need and nothing else.

    ``alembic upgrade head`` and ``scripts/setup_checkpoints.py`` read this
    class, so migrating a database never requires LLM or authentication
    configuration. The application reads ``Settings``, which extends it.
    """

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


class Settings(DatabaseSettings):
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

    # How long shutdown waits for in-flight runs before closing pools.
    shutdown_grace_seconds: float = Field(default=30.0, ge=0)

    # Authentication (ADR 0005). The API verifies OIDC access tokens; it never
    # issues credentials. "disabled" is a local/test convenience only.
    auth_mode: Literal["oidc", "disabled"] = "oidc"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    # Exactly one of the two key sources when auth_mode == "oidc".
    oidc_jwks_url: str | None = None
    oidc_jwks_file: Path | None = None  # static JWKS: local/test only
    oidc_algorithms: list[str] = Field(default_factory=lambda: ["RS256", "ES256"])
    oidc_leeway_seconds: int = Field(default=30, ge=0, le=300)
    oidc_jwks_cache_seconds: int = Field(default=300, ge=0)
    # Claim paths (dotted for nested claims, e.g. "realm_access.roles").
    oidc_roles_claim: str = "roles"
    oidc_projects_claim: str = "projects"
    oidc_kind_claim: str = "kind"

    # Authorization: may the creator of a run approve it? None = forbidden in
    # staging/production, allowed in local/test (personal use, development).
    authz_separation_of_duties: bool | None = None

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

    # Tool Gateway (Fase 5, ADR 0006): bounds for the read-only workspace tools.
    tool_max_read_bytes: int = Field(default=64_000, ge=1_024)
    tool_max_entries: int = Field(default=500, ge=1)
    tool_max_output_bytes: int = Field(default=256_000, ge=1_024)
    tool_timeout_seconds: float = Field(default=10.0, gt=0)

    # Managed Git repositories and per-run worktrees (Fase 5, Paso 2). All
    # optional: without GIT_REPOSITORIES_FILE no Git tool is registered.
    git_binary: str | None = None
    git_timeout_seconds: float = Field(default=30.0, gt=0)
    git_repositories_root: Path | None = None  # every registered repository lives under it
    git_repositories_file: Path | None = None  # JSON: {"repositories": [...]}
    worktrees_root: Path | None = None
    worktree_retention_seconds: float = Field(default=3600.0, ge=0)
    worktree_interrupted_retention_seconds: float = Field(default=7 * 24 * 3600.0, ge=0)
    worktree_max_per_project: int = Field(default=20, ge=1)
    worktree_min_free_bytes: int = Field(default=256 * 1024 * 1024, ge=0)

    # Remote sandbox runner (Fase 5, Paso 3; ADR 0007). Without a runner the
    # exec tools refuse: there is no local execution of untrusted code.
    sandbox_runner_url: str | None = None  # https://, or http:// to a tailnet address
    sandbox_runner_token: SecretStr | None = None
    sandbox_runner_id: str | None = None  # identity the runner must report in /v1/health
    sandbox_runner_ca_file: Path | None = None
    # Plain HTTP to a private LAN/VPC address (local VM, cloud subnet). Local/test only.
    sandbox_runner_allow_private_http: bool = False
    sandbox_profile: str = Field(default="python-3.13", pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    sandbox_cpu_seconds: float = Field(default=60.0, gt=0, le=3600)
    sandbox_memory_mb: int = Field(default=1024, ge=64, le=16384)
    sandbox_pids: int = Field(default=256, ge=16, le=4096)
    sandbox_wall_seconds: float = Field(default=120.0, gt=0, le=3600)
    sandbox_storage_mb: int = Field(default=512, ge=16, le=20480)
    sandbox_max_output_bytes: int = Field(default=256_000, ge=4096, le=16 * 1024 * 1024)
    sandbox_max_archive_mb: int = Field(default=50, ge=1, le=1024)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def separation_of_duties(self) -> bool:
        if self.authz_separation_of_duties is not None:
            return self.authz_separation_of_duties
        return self.is_production_like

    @field_validator("oidc_algorithms")
    @classmethod
    def _only_asymmetric_algorithms(cls, value: list[str]) -> list[str]:
        allowed = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
        bad = [alg for alg in value if alg not in allowed]
        if bad or not value:
            raise ValueError(
                "OIDC_ALGORITHMS solo admite algoritmos asimétricos (RS*/ES*/PS*); "
                f"rechazados: {bad or 'lista vacía'}"
            )
        return value

    @model_validator(mode="after")
    def _authentication_is_coherent(self) -> "Settings":
        if self.auth_mode == "disabled":
            if self.is_production_like:
                raise ValueError("AUTH_MODE=disabled está prohibido en staging y production")
            return self
        missing = [
            name
            for name, value in (
                ("OIDC_ISSUER", self.oidc_issuer),
                ("OIDC_AUDIENCE", self.oidc_audience),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"AUTH_MODE=oidc requiere {', '.join(missing)}")
        if bool(self.oidc_jwks_url) == bool(self.oidc_jwks_file):
            raise ValueError(
                "Define exactamente una fuente de claves: OIDC_JWKS_URL u OIDC_JWKS_FILE"
            )
        if self.oidc_jwks_file is not None and self.is_production_like:
            raise ValueError("OIDC_JWKS_FILE solo se admite en local y test; usa OIDC_JWKS_URL")
        return self

    @model_validator(mode="after")
    def _git_storage_is_coherent(self) -> "Settings":
        if self.git_repositories_file is None:
            return self
        missing = [
            name
            for name, value in (
                ("GIT_REPOSITORIES_ROOT", self.git_repositories_root),
                ("WORKTREES_ROOT", self.worktrees_root),
            )
            if value is None
        ]
        if missing:
            raise ValueError(f"GIT_REPOSITORIES_FILE requiere {', '.join(missing)}")
        paths = (self.git_repositories_root, self.git_repositories_file, self.worktrees_root)
        if any(path is not None and not path.is_absolute() for path in paths):
            raise ValueError("Las rutas de repositorios y worktrees deben ser absolutas")
        return self

    @model_validator(mode="after")
    def _sandbox_runner_is_complete(self) -> "Settings":
        given = {
            "SANDBOX_RUNNER_URL": self.sandbox_runner_url,
            "SANDBOX_RUNNER_TOKEN": self.sandbox_runner_token,
            "SANDBOX_RUNNER_ID": self.sandbox_runner_id,
        }
        if any(v is not None for v in given.values()) and not all(given.values()):
            missing = [k for k, v in given.items() if not v]
            raise ValueError(f"El runner de sandbox requiere {', '.join(missing)}")
        if self.sandbox_runner_allow_private_http and self.is_production_like:
            raise ValueError(
                "SANDBOX_RUNNER_ALLOW_PRIVATE_HTTP solo se admite en local y test; "
                "en staging/production usa https:// o una red superpuesta privada"
            )
        return self

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


@lru_cache
def get_environment() -> EnvironmentSettings:
    return EnvironmentSettings()


@lru_cache
def get_database_settings() -> DatabaseSettings:
    """For deploy steps (migrations, checkpoint setup): database only."""
    return DatabaseSettings()


@lru_cache
def get_settings() -> Settings:
    return Settings()

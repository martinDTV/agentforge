from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

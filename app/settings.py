from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    anthropic_vertex_project_id: str
    cloud_ml_region: str = "global"
    claude_model: str = "claude-sonnet-4-6"
    use_fake_model: bool = False
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

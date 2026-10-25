from pydantic import BaseModel, ConfigDict, Field

from app.settings import Settings


class ToolLimits(BaseModel):
    """Bounds applied by the read-only tools and by the gateway itself."""

    model_config = ConfigDict(frozen=True)

    max_read_bytes: int = Field(default=64_000, ge=1_024)
    max_entries: int = Field(default=500, ge=1)
    max_output_bytes: int = Field(default=256_000, ge=1_024)
    timeout_seconds: float = Field(default=10.0, gt=0)
    # Upper bound for tools that declare their own timeout, and how long the
    # gateway waits for a tool's cancel hook after a timeout.
    max_timeout_seconds: float = Field(default=900.0, gt=0)
    cancel_grace_seconds: float = Field(default=5.0, gt=0)

    @classmethod
    def from_settings(cls, settings: Settings) -> "ToolLimits":
        return cls(
            max_read_bytes=settings.tool_max_read_bytes,
            max_entries=settings.tool_max_entries,
            max_output_bytes=settings.tool_max_output_bytes,
            timeout_seconds=settings.tool_timeout_seconds,
        )

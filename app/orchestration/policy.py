import os
import socket
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.settings import Settings


class RecoveryPolicy(BaseModel):
    """Lease, heartbeat and recovery limits for one process (ADR 0004).

    ``lease_ttl_seconds`` is how long a run may go without a heartbeat before
    anyone else may take it over. The heartbeat renews it every
    ``heartbeat_seconds`` (a third of the TTL by default), so two consecutive
    missed renewals are needed before expiry. ``auto_resume`` is off by
    default: recovery then re-projects and applies pending decisions, which
    costs no LLM calls, but leaves interrupted runs as ``interrupted``.
    """

    model_config = ConfigDict(frozen=True)

    lease_ttl_seconds: float = Field(default=60.0, gt=0)
    heartbeat_seconds: float | None = Field(default=None, gt=0)
    auto_resume: bool = False
    max_attempts: int = Field(default=3, ge=1)
    batch_size: int = Field(default=20, ge=1)

    @model_validator(mode="after")
    def _heartbeat_fits(self) -> "RecoveryPolicy":
        if (
            self.heartbeat_seconds is not None
            and self.heartbeat_seconds * 2 > self.lease_ttl_seconds
        ):
            raise ValueError("heartbeat_seconds must be at most half of lease_ttl_seconds")
        return self

    @property
    def heartbeat_interval(self) -> float:
        return (
            self.heartbeat_seconds
            if self.heartbeat_seconds is not None
            else self.lease_ttl_seconds / 3
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "RecoveryPolicy":
        return cls(
            lease_ttl_seconds=settings.run_lease_ttl_seconds,
            heartbeat_seconds=settings.run_heartbeat_seconds,
            auto_resume=settings.recovery_auto_resume,
            max_attempts=settings.max_run_attempts,
            batch_size=settings.recovery_batch_size,
        )


def new_owner_id() -> str:
    """Identity of this process as a lease owner: host, pid and a random suffix."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

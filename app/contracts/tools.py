"""Shared vocabulary of the Tool Gateway (Fase 5, ADR 0006).

Pure schemas: no I/O, no FastAPI, no orchestration. ``ToolCall`` is the only
thing a model may produce; every other object here is built by trusted code
and handed to the model as a result.
"""

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RiskClass(StrEnum):
    """What a tool can do to the world; decides who may call it and when."""

    READ = "read"  # observes the workspace, never changes it
    WRITE = "write"  # changes files inside the run's workspace (later steps)
    EXEC = "exec"  # runs programs, only inside a sandbox (later steps)
    NETWORK = "network"  # talks to anything outside the host (later steps)


class AgentKind(StrEnum):
    """The node on whose behalf a tool is invoked; drives the permission matrix."""

    PLANNER = "planner"
    ARCHITECT = "architect"
    DEVELOPER = "developer"
    QA = "qa"
    SECURITY = "security"


class ToolOutcome(StrEnum):
    """What the caller gets back. Every value is terminal."""

    OK = "ok"
    DENIED = "denied"  # registry, policy or idempotency said no; nothing ran
    INVALID_INPUT = "invalid_input"  # arguments rejected by the tool's schema; nothing ran
    FAILED = "failed"  # the tool ran and reported, or suffered, an error


class InvocationStatus(StrEnum):
    """Life of an audit row. ``pending`` and ``running`` are the only open states.

    A ``running`` row whose attempt is over means the process died mid-call:
    the next attempt marks it ``uncertain`` and, for tools with effects,
    refuses to repeat the step automatically.
    """

    PENDING = "pending"  # claimed; the tool has not started
    RUNNING = "running"  # the tool is executing
    OK = "ok"
    FAILED = "failed"
    DENIED = "denied"
    INVALID_INPUT = "invalid_input"
    UNCERTAIN = "uncertain"  # crashed while running; effects unknown


OPEN_STATUSES: frozenset[InvocationStatus] = frozenset(
    {InvocationStatus.PENDING, InvocationStatus.RUNNING}
)


class ToolCall(BaseModel):
    """What a model asks for. Nothing in here is trusted: the name is a lookup
    key and the arguments are validated against the tool's input schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=64)
    message: str = ""
    details: list[str] = Field(default_factory=list)


STEP_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$"


class ToolResult(BaseModel):
    """Structured answer for the caller and, through it, for the model."""

    model_config = ConfigDict(frozen=True)

    invocation_id: UUID
    tool: str
    step: str = Field(pattern=STEP_PATTERN)
    attempt: int = Field(ge=0)
    outcome: ToolOutcome
    output: dict[str, Any] | None = None
    error: ToolError | None = None
    duration_ms: int = Field(default=0, ge=0)

    @property
    def ok(self) -> bool:
        return self.outcome is ToolOutcome.OK

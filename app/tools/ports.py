"""Ports of the Tool Gateway: what a tool is, the trusted context it runs in,
and the audit sink that records every invocation.

Dependency direction: this package imports contracts and security, never
orchestration or infrastructure. The orchestration will call the gateway;
infrastructure implements the audit repository.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.tools import STEP_PATTERN, AgentKind, InvocationStatus, RiskClass
from app.security import Principal

from .errors import ToolRegistrationError

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
# Namespace for deterministic invocation ids: the same (run, step, attempt)
# always maps to the same id, so a re-driven step cannot record twice.
INVOCATION_NAMESPACE = UUID("4f0c1d4e-7f2a-4b7e-9c1a-2b3d4e5f6a7b")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Static description of a tool: identity, risk and typed contracts."""

    name: str
    description: str
    risk: RiskClass
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    version: str = "1"
    # Tools that legitimately take longer than the gateway default (remote
    # executions) declare it; the gateway still caps it.
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if len(self.name) > 128 or not TOOL_NAME_RE.fullmatch(self.name):
            raise ToolRegistrationError(f"nombre de herramienta inválido: {self.name!r}")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ToolRegistrationError(f"timeout inválido para {self.name}")
        if not 0 < len(self.description) <= 500:
            raise ToolRegistrationError(f"la herramienta {self.name} necesita una descripción")
        # Unknown argument names must fail validation: that is what stops a
        # model from smuggling "project_id" or "workspace_root" into a call.
        if self.input_model.model_config.get("extra") != "forbid":
            raise ToolRegistrationError(
                f"el modelo de entrada de {self.name} debe declarar extra='forbid'"
            )

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()


class InvocationRef(BaseModel):
    """Identity of the call in progress, stamped by the gateway on the context
    it hands to a tool. Tools with effects use ``id`` as their idempotency key."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    step: str = Field(pattern=STEP_PATTERN)
    attempt: int = Field(ge=0)


class ToolContext(BaseModel):
    """Trusted execution context, built by the orchestration for one run attempt.

    Never constructed from model output. ``principal`` is the automation
    identity minted for this run; ``workspace_root`` is the only directory
    filesystem tools may touch. There is deliberately no environment here:
    tools receive nothing from the host process. ``invocation`` is filled by
    the gateway for each call.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    attempt: int = Field(default=0, ge=0)
    project_id: str = Field(min_length=1, max_length=64)
    agent: AgentKind
    principal: Principal
    workspace_root: Path | None = None
    invocation: InvocationRef | None = None


class ToolPort(Protocol):
    """A tool: a spec plus a synchronous ``run`` (the gateway offloads it to a thread).

    ``arguments`` is an instance of ``spec.input_model`` validated by the
    gateway; the return value must be an instance of ``spec.output_model``.
    Expected failures raise ``ToolExecutionError``.
    """

    @property
    def spec(self) -> ToolSpec: ...

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel: ...


class CancellableTool(Protocol):
    """Tools whose work continues elsewhere (a remote sandbox) stop it here.

    Called by the gateway after a timeout, with the same per-call context.
    """

    def cancel(self, context: ToolContext) -> bool: ...


class ToolDescription(BaseModel):
    """What a model is told about a tool it may call."""

    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    description: str
    risk: RiskClass
    input_schema: dict[str, Any]


class ToolInvocation(BaseModel):
    """Audit record of one call. Digests, codes and counters: never arguments,
    file contents or outputs, which may hold data the audit must not keep.

    ``step`` is the logical step chosen by the orchestration (node, loop
    index, purpose); together with ``run_id`` and ``attempt`` it identifies
    the call across restarts. ``input_digest`` lets a retry prove it is
    repeating the same call and not a different one under the same name.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    run_id: UUID
    step: str = Field(pattern=STEP_PATTERN)
    attempt: int = Field(ge=0)
    tool_name: str = Field(max_length=128)
    tool_version: str = Field(default="", max_length=32)
    risk: RiskClass | None = None  # None when the name matched no registered tool
    agent: AgentKind
    actor_subject: str = Field(max_length=255)
    project_id: str = Field(max_length=64)
    status: InvocationStatus
    error_code: str | None = Field(default=None, max_length=64)
    input_digest: str = Field(max_length=64)
    output_digest: str | None = Field(default=None, max_length=64)
    output_bytes: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class InvocationCompletion(BaseModel):
    """Data written when a row leaves an open state."""

    model_config = ConfigDict(frozen=True)

    finished_at: datetime
    error_code: str | None = None
    output_digest: str | None = None
    output_bytes: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)


class ToolInvocationRepository(Protocol):
    async def find_step(self, run_id: UUID, step: str) -> list[ToolInvocation]:
        """Every attempt recorded for a logical step, oldest attempt first."""
        ...

    async def start(self, invocation: ToolInvocation) -> bool:
        """Insert the row before anything runs (``pending``, or a terminal
        status for refusals). False when ``(run, step, attempt)`` exists."""
        ...

    async def transition(
        self,
        invocation_id: UUID,
        *,
        expected: InvocationStatus,
        status: InvocationStatus,
        completion: InvocationCompletion | None = None,
    ) -> bool:
        """Conditional update ``expected -> status``; False when the row was
        not in ``expected``. ``completion`` is required for terminal states."""
        ...

    async def list_for_run(self, run_id: UUID, *, limit: int = 100) -> list[ToolInvocation]:
        """Oldest first (attempt, started_at)."""
        ...


def invocation_id(run_id: UUID, step: str, attempt: int) -> UUID:
    return uuid5(INVOCATION_NAMESPACE, f"{run_id}:{attempt}:{step}")

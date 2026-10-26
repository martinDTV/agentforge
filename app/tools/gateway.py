"""The Tool Gateway: the single path from a model's request to a tool's code.

Order of operations, each one audited:

1. the logical ``step`` (chosen by the orchestration) is compared with every
   earlier attempt of the same step: same arguments or ``invocation_conflict``;
2. the name must be registered (else ``denied`` / ``tool_not_registered``);
3. the policy must allow it for this principal, agent and project;
4. the arguments must validate against the tool's closed input schema;
5. idempotency across attempts: open rows left by a dead attempt are closed
   (``running`` becomes ``uncertain``); tools with effects are refused when the
   step already succeeded or its outcome is uncertain;
6. the audit row is claimed (``pending``) and moved to ``running`` *before* the
   tool executes in a worker thread under a timeout;
7. the output is validated against the output schema, redacted, bounded and
   digested, and the row is closed with the final status.

Tool failures become ``failed`` results: the gateway never raises them to
the caller, so an agent loop can show the error to the model and continue.
What it does raise is an unavailable audit: without audit nothing runs.
"""

import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from anyio import CancelScope, fail_after, get_cancelled_exc_class, to_thread
from pydantic import BaseModel, ValidationError

from app.contracts.tools import (
    STEP_PATTERN,
    InvocationStatus,
    RiskClass,
    ToolCall,
    ToolError,
    ToolOutcome,
    ToolResult,
)
from app.security import ToolAuthorizationPolicy

from .errors import ToolExecutionError
from .limits import ToolLimits
from .ports import (
    InvocationCompletion,
    InvocationRef,
    ToolContext,
    ToolDescription,
    ToolInvocation,
    ToolInvocationRepository,
    ToolPort,
    invocation_id,
)
from .redaction import redact
from .registry import ToolRegistry

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
_STEP_RE = re.compile(STEP_PATTERN)
_TERMINAL_FOR_OUTCOME = {
    ToolOutcome.OK: InvocationStatus.OK,
    ToolOutcome.FAILED: InvocationStatus.FAILED,
    ToolOutcome.DENIED: InvocationStatus.DENIED,
    ToolOutcome.INVALID_INPUT: InvocationStatus.INVALID_INPUT,
}


def digest_arguments(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolGateway:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: ToolAuthorizationPolicy,
        audit: ToolInvocationRepository,
        *,
        limits: ToolLimits | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._audit = audit
        self._limits = limits or ToolLimits()
        self._clock: Clock = clock or (lambda: datetime.now(UTC))

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    def available_for(self, context: ToolContext) -> list[ToolDescription]:
        """What this agent may call right now: the same decision ``invoke`` makes."""
        allowed = [
            tool.spec.name
            for tool in self._registry
            if self._policy.decide(context.principal, context, tool.spec).allowed
        ]
        return self._registry.describe(allowed)

    async def invoke(self, context: ToolContext, call: ToolCall, *, step: str) -> ToolResult:
        """Run ``call`` as logical ``step`` of ``context``'s run attempt.

        ``step`` comes from trusted code (node name, loop index, purpose) and
        must be stable across restarts: it is what makes a retry recognisable.
        """
        if not _STEP_RE.fullmatch(step):
            raise ValueError(f"paso lógico inválido: {step!r}")
        tool = self._registry.get(call.name)
        base: dict[str, Any] = {
            "id": invocation_id(context.run_id, step, context.attempt),
            "run_id": context.run_id,
            "step": step,
            "attempt": context.attempt,
            "tool_name": call.name,
            "tool_version": tool.spec.version if tool is not None else "",
            "risk": tool.spec.risk if tool is not None else None,
            "agent": context.agent,
            "actor_subject": context.principal.subject,
            "project_id": context.project_id,
            "input_digest": digest_arguments(call.arguments),
        }
        prior = await self._audit.find_step(context.run_id, step)
        if any(row.attempt == context.attempt for row in prior):
            logger.warning(
                "Paso repetido dentro del mismo intento: run=%s step=%s attempt=%d",
                context.run_id,
                step,
                context.attempt,
            )
            return self._result(
                base,
                ToolOutcome.DENIED,
                ToolError(
                    code="duplicate_invocation",
                    message="este paso ya se registró en este intento",
                ),
            )
        if any(row.input_digest != base["input_digest"] for row in prior):
            return await self._refuse(
                base,
                ToolOutcome.DENIED,
                ToolError(
                    code="invocation_conflict",
                    message="este paso ya se registró con otros argumentos o herramienta",
                ),
            )
        if tool is None:
            return await self._refuse(
                base,
                ToolOutcome.DENIED,
                ToolError(
                    code="tool_not_registered", message=f"herramienta desconocida: {call.name}"
                ),
            )
        decision = self._policy.decide(context.principal, context, tool.spec)
        if not decision.allowed:
            return await self._refuse(
                base, ToolOutcome.DENIED, ToolError(code=decision.code, message=decision.reason)
            )
        try:
            arguments = tool.spec.input_model.model_validate(call.arguments)
        except ValidationError as exc:
            details = [
                f"{'.'.join(str(p) for p in error['loc']) or '<root>'}: {error['msg']}"
                for error in exc.errors()
            ]
            return await self._refuse(
                base,
                ToolOutcome.INVALID_INPUT,
                ToolError(
                    code="invalid_arguments", message="argumentos inválidos", details=details
                ),
            )
        blocker = await self._settle_earlier_attempts(prior, tool)
        if blocker is not None:
            return await self._refuse(base, ToolOutcome.DENIED, blocker)
        claimed_at = self._clock()
        record = ToolInvocation(**base, status=InvocationStatus.PENDING, started_at=claimed_at)
        if not await self._audit.start(record):
            return self._result(
                base,
                ToolOutcome.DENIED,
                ToolError(code="duplicate_invocation", message="el paso ya estaba reclamado"),
            )
        if not await self._audit.transition(
            record.id, expected=InvocationStatus.PENDING, status=InvocationStatus.RUNNING
        ):
            return self._result(
                base,
                ToolOutcome.DENIED,
                ToolError(code="duplicate_invocation", message="el paso cambió de estado"),
            )
        return await self._execute(base, tool, context, arguments)

    # -- internals ----------------------------------------------------------------------

    async def _settle_earlier_attempts(
        self, prior: list[ToolInvocation], tool: ToolPort
    ) -> ToolError | None:
        """Close rows a dead attempt left open and decide whether effects allow a retry."""
        now = self._clock()
        uncertain = False
        for row in prior:
            if row.status is InvocationStatus.RUNNING:
                await self._audit.transition(
                    row.id,
                    expected=InvocationStatus.RUNNING,
                    status=InvocationStatus.UNCERTAIN,
                    completion=InvocationCompletion(finished_at=now, error_code="process_lost"),
                )
                uncertain = True
            elif row.status is InvocationStatus.PENDING:
                await self._audit.transition(
                    row.id,
                    expected=InvocationStatus.PENDING,
                    status=InvocationStatus.FAILED,
                    completion=InvocationCompletion(
                        finished_at=now, error_code="abandoned_before_start"
                    ),
                )
            elif row.status is InvocationStatus.UNCERTAIN:
                uncertain = True
        if tool.spec.risk is RiskClass.READ:
            return None  # observing again is always safe
        if uncertain:
            return ToolError(
                code="uncertain_effects",
                message="un intento anterior murió ejecutando este paso; requiere revisión",
            )
        if any(row.status is InvocationStatus.OK for row in prior):
            return ToolError(
                code="already_completed",
                message="este paso ya se completó en un intento anterior",
            )
        return None

    async def _execute(
        self,
        base: dict[str, Any],
        tool: ToolPort,
        context: ToolContext,
        arguments: BaseModel,
    ) -> ToolResult:
        t0 = perf_counter()
        output: dict[str, Any] | None = None
        error: ToolError | None = None
        payload_text: str | None = None
        call_context = context.model_copy(
            update={
                "invocation": InvocationRef(
                    id=base["id"], step=base["step"], attempt=base["attempt"]
                )
            }
        )
        timeout = min(
            tool.spec.timeout_seconds or self._limits.timeout_seconds,
            self._limits.max_timeout_seconds,
        )
        try:
            with fail_after(timeout):
                # The worker thread cannot be killed: on timeout the task
                # resumes and the thread is abandoned. Read-only tools make
                # this safe; tools whose work lives elsewhere (a sandbox)
                # implement ``cancel`` and the gateway calls it below.
                produced = await to_thread.run_sync(
                    tool.run,
                    call_context,
                    arguments,
                    abandon_on_cancel=tool.spec.risk is not RiskClass.WRITE,
                )
            output, payload_text = self._serialize(tool, produced)
        except TimeoutError:
            error = ToolError(code="timeout", message=f"la herramienta superó {timeout:g}s")
            await self._cancel(tool, call_context)
        except get_cancelled_exc_class():
            # A cancelled read did nothing; a cancelled write or sandbox run may
            # have had its effect: the audit must say so, and a later attempt of
            # the same step is then refused (``uncertain_effects``).
            with CancelScope(shield=True):
                await self._cancel(tool, call_context)
                await self._audit.transition(
                    base["id"],
                    expected=InvocationStatus.RUNNING,
                    status=(
                        InvocationStatus.FAILED
                        if tool.spec.risk is RiskClass.READ
                        else InvocationStatus.UNCERTAIN
                    ),
                    completion=InvocationCompletion(
                        finished_at=self._clock(), error_code="cancelled"
                    ),
                )
            raise
        except ToolExecutionError as exc:
            error = ToolError(code=exc.code, message=exc.detail)
        except Exception:  # a bug in a tool must not take the run down
            logger.exception("La herramienta %s falló inesperadamente", tool.spec.name)
            error = ToolError(code="tool_crashed", message="error interno de la herramienta")
        duration_ms = int((perf_counter() - t0) * 1000)
        outcome = ToolOutcome.OK if error is None else ToolOutcome.FAILED
        completion = InvocationCompletion(
            finished_at=self._clock(),
            error_code=error.code if error is not None else None,
            output_digest=(
                hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
                if payload_text is not None
                else None
            ),
            output_bytes=len(payload_text.encode("utf-8")) if payload_text is not None else None,
            duration_ms=duration_ms,
        )
        closed = await self._audit.transition(
            base["id"],
            expected=InvocationStatus.RUNNING,
            status=_TERMINAL_FOR_OUTCOME[outcome],
            completion=completion,
        )
        if not closed:
            logger.error("La auditoría de %s no estaba en ejecución al cerrarla", base["id"])
        return self._result(base, outcome, error, output=output, duration_ms=duration_ms)

    async def _cancel(self, tool: ToolPort, context: ToolContext) -> None:
        """Best effort: stop whatever the tool started elsewhere."""
        cancel = getattr(tool, "cancel", None)
        if cancel is None:
            return
        try:
            with fail_after(self._limits.cancel_grace_seconds):
                stopped = await to_thread.run_sync(cancel, context, abandon_on_cancel=True)
            logger.warning(
                "Herramienta %s cancelada tras timeout (detenida=%s)", tool.spec.name, stopped
            )
        except Exception:  # cancellation is best effort; the result is already "timeout"
            logger.exception("La cancelación de %s falló", tool.spec.name)

    def _serialize(self, tool: ToolPort, produced: object) -> tuple[dict[str, Any], str]:
        if not isinstance(produced, tool.spec.output_model):
            raise ToolExecutionError(
                "invalid_output", f"{tool.spec.name} devolvió un tipo inesperado"
            )
        text = json.dumps(produced.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        text, _ = redact(text)
        size = len(text.encode("utf-8"))
        if size > self._limits.max_output_bytes:
            raise ToolExecutionError(
                "output_too_large",
                f"la salida ocupa {size} bytes; máximo {self._limits.max_output_bytes}",
            )
        try:
            data: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolExecutionError("redaction_failed", "la salida no pudo redactarse") from exc
        return data, text

    async def _refuse(
        self, base: dict[str, Any], outcome: ToolOutcome, error: ToolError
    ) -> ToolResult:
        now = self._clock()
        record = ToolInvocation(
            **base,
            status=_TERMINAL_FOR_OUTCOME[outcome],
            error_code=error.code,
            started_at=now,
            finished_at=now,
        )
        if not await self._audit.start(record):
            logger.warning("Rechazo duplicado no registrado: %s", record.id)
        return self._result(base, outcome, error)

    @staticmethod
    def _result(
        base: dict[str, Any],
        outcome: ToolOutcome,
        error: ToolError | None,
        *,
        output: dict[str, Any] | None = None,
        duration_ms: int = 0,
    ) -> ToolResult:
        return ToolResult(
            invocation_id=base["id"],
            tool=base["tool_name"],
            step=base["step"],
            attempt=base["attempt"],
            outcome=outcome,
            output=output,
            error=error,
            duration_ms=duration_ms,
        )

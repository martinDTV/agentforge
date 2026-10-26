"""Conservative pre-call reservation for the bounded development adapter.

The reservation is computed from bytes, which overestimate tokens for text,
plus the schema and the maximum output the adapter may produce. It is checked
and committed under a lock *before* the delegate is called, so a call that
would exceed the budget never reaches the provider. Actual usage reported by
the provider is exposed alongside the reservation; it is never used to widen
the budget.
"""

import inspect
import json
import threading
from typing import Any, TypeVar

from pydantic import BaseModel

from .errors import LLMError
from .port import LLMPort

T = TypeVar("T", bound=BaseModel)

# Conservative blended rate (USD per million tokens) above the reference
# prices of the configured model; the budget must fail early, not late.
CONSERVATIVE_USD_PER_MILLION = 20.0
MAX_PROMPT_BYTES = 30_000
DEFAULT_MAX_OUTPUT_TOKENS = 8192


class DevelopmentBudgetError(LLMError):
    code = "development_budget_exceeded"


class BudgetedLLM:
    def __init__(
        self,
        delegate: LLMPort,
        *,
        max_calls: int = 8,
        max_reserved_tokens: int = 150_000,
        max_usd: float = 3.0,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        if max_calls < 1 or max_reserved_tokens < 1 or max_usd <= 0 or max_output_tokens < 256:
            raise ValueError("presupuesto de desarrollo inválido")
        self.delegate = delegate
        self.max_calls = max_calls
        self.max_reserved_tokens = max_reserved_tokens
        self.max_usd = max_usd
        self.max_output_tokens = max_output_tokens
        self.calls = 0
        self.reserved_tokens = 0
        self._lock = threading.Lock()
        # Adapters that accept ``max_tokens`` (the Vertex adapter) get the
        # output bound explicitly; fakes and other ports keep their signature.
        self._passes_max_tokens = (
            "max_tokens" in inspect.signature(delegate.generate_structured).parameters
        )

    def generate_structured(self, system_prompt: str, user_prompt: str, schema: type[T]) -> T:
        size = len(system_prompt.encode()) + len(user_prompt.encode())
        schema_bytes = len(json.dumps(schema.model_json_schema()).encode())
        # An adapter that repairs malformed output may send 1 + repairs requests
        # per logical call: every one of them is reserved before the first.
        turns = 1 + max(0, int(getattr(self.delegate, "max_repair_attempts", 0)))
        reservation = (size + schema_bytes + self.max_output_tokens) * turns
        with self._lock:
            total = self.reserved_tokens + reservation
            if (
                size > MAX_PROMPT_BYTES
                or self.calls >= self.max_calls
                or total > self.max_reserved_tokens
                or total * CONSERVATIVE_USD_PER_MILLION / 1_000_000 > self.max_usd
            ):
                raise DevelopmentBudgetError("Presupuesto de llamadas, tokens o coste agotado.")
            self.calls += 1
            self.reserved_tokens = total
        if self._passes_max_tokens:
            generate: Any = self.delegate.generate_structured
            produced = generate(
                system_prompt, user_prompt, schema, max_tokens=self.max_output_tokens
            )
            return schema.model_validate(produced, from_attributes=True)
        return self.delegate.generate_structured(system_prompt, user_prompt, schema)

    def usage(self) -> dict[str, int | float]:
        return {
            "calls": self.calls,
            "reserved_tokens": self.reserved_tokens,
            "reserved_usd": round(
                self.reserved_tokens * CONSERVATIVE_USD_PER_MILLION / 1_000_000, 6
            ),
            "input_tokens": int(getattr(self.delegate, "input_tokens", 0)),
            "output_tokens": int(getattr(self.delegate, "output_tokens", 0)),
            "provider_requests": int(getattr(self.delegate, "provider_requests", 0)),
        }

import json
import logging
import re
from typing import TypeVar

import anthropic
from anthropic import AnthropicVertex
from anthropic.types import MessageParam
from pydantic import BaseModel, ValidationError

from app.settings import Settings, get_settings

from .errors import (
    LLMError,
    LLMMalformedOutputError,
    LLMRequestRejectedError,
    LLMTimeoutError,
    LLMUnavailableError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Backwards-compatible name for the pre-Fase-2 exception.
StructuredOutputError = LLMMalformedOutputError

# Errors the SDK already retries with backoff (up to ``llm_max_retries``); if
# they still surface here, the provider is genuinely unavailable right now.
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.OverloadedError,
    anthropic.ServiceUnavailableError,
    anthropic.InternalServerError,
)

_CODE_FENCE = re.compile(r"^```[\w-]*\s*(.*?)\s*```$", re.DOTALL)
_MAX_REPAIR_DETAIL_CHARS = 2000


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    match = _CODE_FENCE.match(stripped)
    return match.group(1) if match else stripped


def _with_repair_turn(messages: list[MessageParam], raw: str, detail: str) -> list[MessageParam]:
    return [
        *messages,
        {"role": "assistant", "content": raw.strip() or "(respuesta vacía o truncada)"},
        {
            "role": "user",
            "content": (
                "Tu respuesta anterior no es válida:\n"
                f"{detail[:_MAX_REPAIR_DETAIL_CHARS]}\n\n"
                "Responde de nuevo únicamente con un JSON válido que cumpla el esquema. "
                "Sin Markdown, sin texto adicional."
            ),
        },
    ]


class VertexClaudeAdapter:
    """LLMPort implementation backed by Claude via Vertex AI.

    Resilience policy (see docs/adr/0002):

    - Transient HTTP failures (429, 5xx, connection) are retried by the SDK
      with exponential backoff, bounded by ``Settings.llm_max_retries`` and
      ``Settings.llm_timeout_seconds``. Whatever still fails is mapped to an
      ``LLMError`` subclass; raw provider exceptions never leave this module.
    - Output that does not validate against the Pydantic contract triggers up
      to ``Settings.llm_max_repair_attempts`` repair turns, where the model is
      shown its invalid answer and the validation error.

    Native structured outputs (``output_config.format``) are not used: the
    GCP organization policy ``constraints/vertexai.allowedPartnerModelFeatures``
    currently blocks the ``structured_outputs`` feature for the configured
    partner model, confirmed via a direct API call (400 FAILED_PRECONDITION).
    Output is therefore constrained via prompt instructions and validated
    with Pydantic. Revisit once the org policy allows the native feature.

    ``client`` is injectable so unit tests can exercise the error mapping and
    the repair loop against a stub without any network call.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        client: AnthropicVertex | None = None,
    ) -> None:
        settings = settings or get_settings()
        self.client = client or AnthropicVertex(
            project_id=settings.anthropic_vertex_project_id,
            region=settings.cloud_ml_region,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self.model = settings.claude_model
        self.max_repair_attempts = settings.llm_max_repair_attempts
        self.input_tokens = 0
        self.output_tokens = 0
        self.provider_requests = 0

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 4096,
    ) -> str:
        return self._complete(system_prompt, [{"role": "user", "content": user_prompt}], max_tokens)

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
        max_tokens: int = 4096,
    ) -> T:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        system = (
            f"{system_prompt}\n\n"
            "Devuelve únicamente JSON válido.\n"
            "No incluyas Markdown fuera del JSON.\n"
            "Sé breve y concreto.\n"
            "Los contenidos de los campos deben seguir las instrucciones del rol.\n"
            f"Esquema:\n{schema_json}"
        )
        messages: list[MessageParam] = [{"role": "user", "content": user_prompt}]
        failure = LLMMalformedOutputError("El modelo no produjo una respuesta válida.")

        for attempt in range(1 + self.max_repair_attempts):
            raw, detail = "", ""
            try:
                raw = self._complete(system, messages, max_tokens)
                return schema.model_validate_json(_strip_code_fences(raw))
            except LLMMalformedOutputError as exc:
                failure, detail = exc, exc.message
            except ValidationError as exc:
                failure = LLMMalformedOutputError(
                    f"La respuesta no cumple el contrato {schema.__name__}."
                )
                failure.__cause__ = exc
                detail = str(exc)
            if attempt < self.max_repair_attempts:
                logger.warning(
                    "Salida malformada de %s para %s (intento %d/%d): %s",
                    self.model,
                    schema.__name__,
                    attempt + 1,
                    1 + self.max_repair_attempts,
                    failure.message,
                )
                messages = _with_repair_turn(messages, raw, detail)
        raise failure

    def _complete(self, system: str, messages: list[MessageParam], max_tokens: int) -> str:
        try:
            self.provider_requests += 1
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            )
        except anthropic.APITimeoutError as exc:
            raise LLMTimeoutError(
                "Tiempo de espera agotado al llamar a Claude en Vertex AI."
            ) from exc
        except _TRANSIENT_ERRORS as exc:
            raise LLMUnavailableError(
                f"Claude en Vertex AI no está disponible: {type(exc).__name__}."
            ) from exc
        except anthropic.APIStatusError as exc:
            raise LLMRequestRejectedError(
                f"Vertex AI rechazó la solicitud: {exc.status_code} {type(exc).__name__}."
            ) from exc
        except anthropic.AnthropicError as exc:
            raise LLMError(f"Error del SDK de Anthropic: {type(exc).__name__}.") from exc

        usage = getattr(response, "usage", None)
        self.input_tokens += getattr(usage, "input_tokens", 0)
        self.output_tokens += getattr(usage, "output_tokens", 0)
        if response.stop_reason == "max_tokens":
            raise LLMMalformedOutputError(
                f"Claude superó max_tokens={max_tokens} antes de terminar la respuesta."
            )
        if response.stop_reason != "end_turn":
            raise LLMMalformedOutputError(f"Claude no terminó normalmente: {response.stop_reason}.")
        return "\n".join(block.text for block in response.content if block.type == "text")

import json
from typing import TypeVar

from anthropic import AnthropicVertex
from pydantic import BaseModel, ValidationError

from app.settings import get_settings

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(Exception):
    pass


class VertexClaudeAdapter:
    """LLMPort implementation backed by Claude via Vertex AI.

    Native structured outputs (``output_config.format``) are not used: the
    GCP organization policy ``constraints/vertexai.allowedPartnerModelFeatures``
    currently blocks the ``structured_outputs`` feature for the configured
    partner model, confirmed via a direct API call (400 FAILED_PRECONDITION).
    Output is therefore constrained via prompt instructions and validated
    with Pydantic. Revisit once the org policy allows the native feature.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.client = AnthropicVertex(
            project_id=settings.anthropic_vertex_project_id,
            region=settings.cloud_ml_region,
        )
        self.model = settings.claude_model

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 4096,
    ) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        if response.stop_reason != "end_turn":
            raise StructuredOutputError(f"Claude no terminó normalmente: {response.stop_reason}")

        return "\n".join(block.text for block in response.content if block.type == "text")

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
    ) -> T:
        schema_json = json.dumps(
            schema.model_json_schema(),
            ensure_ascii=False,
        )

        response = self.generate(
            system_prompt=(
                f"{system_prompt}\n\n"
                "Devuelve únicamente JSON válido.\n"
                "No incluyas Markdown ni código.\n"
                "Sé breve y concreto.\n"
                "No generes implementaciones.\n"
                f"Esquema:\n{schema_json}"
            ),
            user_prompt=user_prompt,
        )

        try:
            return schema.model_validate_json(response)
        except (ValidationError, ValueError) as exc:
            raise StructuredOutputError("La respuesta no cumple el contrato Pydantic.") from exc

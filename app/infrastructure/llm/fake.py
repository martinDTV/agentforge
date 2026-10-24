from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class FakeLLMAdapter:
    """LLMPort implementation for deterministic tests: no network, no tokens.

    Responses are pre-registered per schema class. If none is registered for
    a requested schema, a default is built by calling ``schema()`` and relies
    on the caller registering one explicitly for required fields.
    """

    def __init__(self, responses: dict[type[BaseModel], BaseModel] | None = None) -> None:
        self._responses: dict[type[BaseModel], BaseModel] = dict(responses or {})
        self.calls: list[tuple[str, str, type[BaseModel]]] = []

    def register(self, schema: type[T], response: T) -> None:
        self._responses[schema] = response

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
    ) -> T:
        self.calls.append((system_prompt, user_prompt, schema))
        if schema not in self._responses:
            raise LookupError(
                f"FakeLLMAdapter no tiene una respuesta registrada para {schema.__name__}"
            )
        response = self._responses[schema]
        return schema.model_validate(response.model_dump())

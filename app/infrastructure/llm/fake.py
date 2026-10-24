from collections import deque
from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class FakeLLMAdapter:
    """LLMPort implementation for deterministic tests: no network, no tokens.

    Two registration styles are supported per schema class:

    - ``register(schema, response)``: the same response is returned on every call.
    - ``register_sequence(schema, [...])``: items are consumed in order; an item
      that is an ``Exception`` instance is raised instead of returned. This is how
      tests simulate revision loops, timeouts and malformed outputs. Sequences
      take precedence over single registrations and raise ``LookupError`` once
      exhausted, so a test never silently reuses a stale response.
    """

    def __init__(self, responses: dict[type[BaseModel], BaseModel] | None = None) -> None:
        self._responses: dict[type[BaseModel], BaseModel] = dict(responses or {})
        self._sequences: dict[type[BaseModel], deque[BaseModel | Exception]] = {}
        self.calls: list[tuple[str, str, type[BaseModel]]] = []

    def register(self, schema: type[T], response: T) -> None:
        self._responses[schema] = response

    def register_sequence(self, schema: type[T], items: Sequence[T | Exception]) -> None:
        self._sequences[schema] = deque[BaseModel | Exception](items)

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
    ) -> T:
        self.calls.append((system_prompt, user_prompt, schema))
        if schema in self._sequences:
            queue = self._sequences[schema]
            if not queue:
                raise LookupError(
                    f"FakeLLMAdapter agotó la secuencia registrada para {schema.__name__}"
                )
            item = queue.popleft()
            if isinstance(item, Exception):
                raise item
            return schema.model_validate(item.model_dump())
        if schema not in self._responses:
            raise LookupError(
                f"FakeLLMAdapter no tiene una respuesta registrada para {schema.__name__}"
            )
        return schema.model_validate(self._responses[schema].model_dump())

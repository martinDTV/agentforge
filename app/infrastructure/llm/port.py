from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMPort(Protocol):
    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
    ) -> T: ...

"""Typed tool registry: the only way a name becomes executable code.

A name that is not registered does not exist for the gateway, whatever a
model claims. Registration validates the spec (name, description, closed
input schema) so every registered tool is describable and safely callable.
"""

from collections.abc import Iterable, Iterator

from .errors import ToolRegistrationError
from .ports import ToolDescription, ToolPort


class ToolRegistry:
    def __init__(self, tools: Iterable[ToolPort] = ()) -> None:
        self._tools: dict[str, ToolPort] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolPort) -> None:
        spec = tool.spec  # ToolSpec validated itself on construction
        if spec.name in self._tools:
            raise ToolRegistrationError(f"herramienta ya registrada: {spec.name}")
        self._tools[spec.name] = tool

    def get(self, name: str) -> ToolPort | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self, names: Iterable[str] | None = None) -> list[ToolDescription]:
        wanted = self.names() if names is None else [n for n in names if n in self._tools]
        return [
            ToolDescription(
                name=spec.name,
                version=spec.version,
                description=spec.description,
                risk=spec.risk,
                input_schema=spec.input_schema(),
            )
            for spec in (self._tools[name].spec for name in wanted)
        ]

    def __iter__(self) -> Iterator[ToolPort]:
        return iter([self._tools[name] for name in self.names()])

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

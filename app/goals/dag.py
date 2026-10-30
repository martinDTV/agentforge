"""Dependency graph of a goal's tasks: validation and a stable topological order."""

from collections.abc import Mapping, Sequence

from app.contracts.planning import find_cycle


class DependencyCycleError(ValueError):
    def __init__(self, cycle: Sequence[str]) -> None:
        self.cycle = list(cycle)
        super().__init__("Las dependencias forman un ciclo: " + " -> ".join(self.cycle))


def topological_order(dependencies: Mapping[str, Sequence[str]]) -> list[str]:
    """Kahn's algorithm with a deterministic tie-break (sorted keys).

    Raises ``ValueError`` for unknown or self dependencies and
    ``DependencyCycleError`` when the graph has a cycle; the error names it.
    """
    known = set(dependencies)
    for key, deps in dependencies.items():
        unknown = sorted(set(deps) - known)
        if unknown:
            raise ValueError(f"La tarea '{key}' depende de ids inexistentes: {unknown}")
        if key in deps:
            raise ValueError(f"La tarea '{key}' no puede depender de sí misma")
    cycle = find_cycle(dependencies)
    if cycle:
        raise DependencyCycleError(cycle)
    remaining: dict[str, set[str]] = {key: set(deps) for key, deps in dependencies.items()}
    order: list[str] = []
    while remaining:
        ready = sorted(key for key, pending in remaining.items() if not pending)
        if not ready:  # pragma: no cover - excluded by find_cycle above
            raise DependencyCycleError(sorted(remaining))
        for key in ready:
            order.append(key)
            del remaining[key]
        for pending in remaining.values():
            pending.difference_update(ready)
    return order


def dependents_of(dependencies: Mapping[str, Sequence[str]], key: str) -> set[str]:
    """Every task that depends on ``key``, directly or transitively."""
    found: set[str] = set()
    frontier = [key]
    while frontier:
        current = frontier.pop()
        for candidate, deps in dependencies.items():
            if current in deps and candidate not in found:
                found.add(candidate)
                frontier.append(candidate)
    return found

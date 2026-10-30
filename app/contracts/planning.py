from collections.abc import Mapping, Sequence

from pydantic import BaseModel, Field, model_validator


class DevelopmentTask(BaseModel):
    id: str
    title: str
    description: str
    dependencies: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(min_length=1)


class DevelopmentPlan(BaseModel):
    summary: str
    tasks: list[DevelopmentTask] = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_dependencies(self) -> "DevelopmentPlan":
        task_ids = {task.id for task in self.tasks}
        for task in self.tasks:
            unknown = set(task.dependencies) - task_ids
            if unknown:
                raise ValueError(
                    f"La tarea '{task.id}' depende de ids inexistentes: {sorted(unknown)}"
                )
            if task.id in task.dependencies:
                raise ValueError(f"La tarea '{task.id}' no puede depender de sí misma")
        cycle = find_cycle({task.id: task.dependencies for task in self.tasks})
        if cycle:
            raise ValueError("Las dependencias forman un ciclo: " + " -> ".join(cycle))
        return self


class ArchitectureDecision(BaseModel):
    id: str
    title: str
    decision: str
    rationale: str


class ArchitectureReview(BaseModel):
    summary: str
    decisions: list[ArchitectureDecision]
    issues: list[str] = Field(default_factory=list)
    approved: bool


def find_cycle(dependencies: Mapping[str, Sequence[str]]) -> list[str] | None:
    """First dependency cycle found, as the closed path ``[a, b, ..., a]``; ``None`` if acyclic.

    Pure and deterministic (keys visited in sorted order), shared by the plan
    contract and the goal scheduler so both reject exactly the same graphs.
    """
    white, grey, black = 0, 1, 2
    colour: dict[str, int] = {key: white for key in dependencies}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        colour[node] = grey
        stack.append(node)
        for dependency in sorted(dependencies.get(node, ())):
            if dependency not in colour:
                continue  # unknown ids are reported by the caller, not here
            state = colour[dependency]
            if state == grey:
                start = stack.index(dependency)
                return [*stack[start:], dependency]
            if state == white:
                found = visit(dependency)
                if found is not None:
                    return found
        stack.pop()
        colour[node] = black
        return None

    for key in sorted(dependencies):
        if colour[key] == white:
            found = visit(key)
            if found is not None:
                return found
    return None

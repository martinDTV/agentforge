from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Structured failure of a planning run whose plan never met the platform constraints.
PLAN_INVALID_CODE = "plan_invalid"
MAX_CONTEXT_PATHS = 300


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


class PlanConstraints(BaseModel):
    """Deterministic facts of the platform a plan must respect (H1, ADR 0012).

    Data, never authority: the planning gate enforces them and the Planner and the
    Architect only read them. Built by the platform from the repository policy and
    the goal limits; nothing here comes from model text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tasks: int | None = Field(default=None, ge=1, le=64)
    writable_globs: list[str] = Field(default_factory=list, max_length=32)
    denied_globs: list[str] = Field(default_factory=list, max_length=96)
    repository_paths: list[str] = Field(default_factory=list, max_length=MAX_CONTEXT_PATHS)
    listing_complete: bool = False
    programs: list[str] = Field(default_factory=list, max_length=8)
    max_files_per_task: int | None = Field(default=None, ge=1)


PlanViolationCode = Literal[
    "too_many_tasks",
    "duplicate_task_id",
    "path_denied",
    "path_outside_policy",
]


class PlanViolation(BaseModel):
    """One deterministic reason a plan cannot be presented to a human."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: PlanViolationCode
    detail: str = Field(min_length=1, max_length=300)
    task_id: str | None = None


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

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

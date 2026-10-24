from pydantic import BaseModel, ConfigDict, Field


class RunLimits(BaseModel):
    """Hard limits applied to a single run by the orchestration graph.

    ``max_revisions`` bounds how many times the architect can send the plan
    back to the planner. ``max_llm_calls`` is the absolute cap on agent calls
    per run: the router never starts a revision round that would not fit in
    the remaining budget, and each LLM node refuses to run once the budget is
    spent (that second check is what makes the cap an invariant rather than a
    routing heuristic).
    """

    model_config = ConfigDict(frozen=True)

    max_revisions: int = Field(default=2, ge=0)
    max_llm_calls: int = Field(default=8, ge=1)

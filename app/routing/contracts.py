from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.routing import Route, RouteMode

POLICY_VERSION = "router-v1"

__all__ = [
    "POLICY_VERSION",
    "Route",
    "RouteDecision",
    "RouteDecisionRepository",
    "RouteMode",
    "TaskClassification",
]


class TaskClassification(BaseModel):
    """What the model is asked for. Advisory: it can only *lower* a route to full."""

    model_config = ConfigDict(extra="forbid")

    category: Literal[
        "bugfix", "feature", "refactor", "docs", "tests", "infrastructure", "security", "unknown"
    ]
    risk: Literal["low", "medium", "high"]
    scope: Literal["single_file", "few_files", "multiple_subsystems", "unknown"]
    ambiguous: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=500)


class RouteDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    route: Route
    mode: RouteMode
    reason: str = Field(max_length=255)
    policy_version: str = POLICY_VERSION
    confidence: float | None = None
    classifier_used: bool = False
    mandatory_rules: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    escalated_from: Route | None = None
    decided_at: datetime


class RouteDecisionRepository(Protocol):
    async def record(self, run_id: UUID, decision: RouteDecision) -> int: ...

    async def list_for_run(self, run_id: UUID) -> list[RouteDecision]: ...

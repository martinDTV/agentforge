"""Smart Router: decides whether a development run needs Planner and Architect.

Deterministic rules first (they can only force the full route), then an optional
classification through ``LLMPort`` whose answer can never grant anything: low
confidence, ambiguity or any mandatory rule means ``full``. The model never
decides permissions; the decision is recorded with its reason, policy version,
confidence and classification budget.
"""

from .contracts import (
    POLICY_VERSION,
    Route,
    RouteDecision,
    RouteDecisionRepository,
    RouteMode,
    TaskClassification,
)
from .router import SmartRouter, escalation_from_scope
from .rules import MANDATORY_FULL_RULES, mandatory_full_rules

__all__ = [
    "MANDATORY_FULL_RULES",
    "POLICY_VERSION",
    "Route",
    "RouteDecision",
    "RouteDecisionRepository",
    "RouteMode",
    "SmartRouter",
    "TaskClassification",
    "escalation_from_scope",
    "mandatory_full_rules",
]

"""Smart Router vocabulary shared by the API contract and the orchestration."""

from enum import StrEnum


class RouteMode(StrEnum):
    AUTO = "auto"  # rules + classifier decide
    FAST = "fast"  # requested fast; mandatory rules can still force full
    FULL = "full"  # always Planner -> Architect -> plan approval first


class Route(StrEnum):
    FAST = "fast"  # Scoper -> scope approval -> Developer/QA -> publish approval -> Publisher
    FULL = "full"  # Planner -> Architect -> plan approval -> the fast route

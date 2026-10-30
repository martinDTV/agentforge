"""Aggregate budget of a goal: reserve before creating, like ``BudgetedLLM`` reserves
before calling. Nothing here is spent by the goal itself; children keep their own limits."""

from dataclasses import dataclass

from app.contracts.goals import GoalLimits, GoalUsage

# Conservative upper bound of one planning round, in USD, at the blended rate
# BudgetedLLM assumes (20 USD per million tokens, ~25k tokens per call).
PLANNING_RESERVATION_USD = 0.5 * 8


@dataclass(frozen=True)
class Reservation:
    calls: int
    usd: float


def resolve_limits(
    requested: GoalLimits | None, *, planning_calls: int, per_task_calls: int
) -> GoalLimits:
    """Fill the derived default of ``max_total_model_calls`` so the stored snapshot is explicit."""
    limits = requested or GoalLimits()
    if limits.max_total_model_calls is not None:
        return limits
    return limits.model_copy(
        update={"max_total_model_calls": planning_calls + limits.max_tasks * per_task_calls}
    )


def budget_refusal(limits: GoalLimits, usage: GoalUsage, reservation: Reservation) -> str | None:
    """``None`` when the reservation fits; otherwise the stable reason code."""
    if usage.tasks_created >= limits.max_tasks:
        return "budget_tasks"
    ceiling = limits.max_total_model_calls
    if ceiling is not None and usage.reserved_model_calls + reservation.calls > ceiling:
        return "budget_calls"
    if usage.reserved_usd + reservation.usd > limits.max_total_usd + 1e-9:
        return "budget_usd"
    return None

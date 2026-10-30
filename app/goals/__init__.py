"""Goal coordination domain (Fase 8, Paso 1; ADR 0012).

Pure rules of the deterministic Supervisor: the dependency graph, the state
machines of goals and tasks, the aggregate budget, the task-text template and
the ports the coordinator needs. This package imports only ``app.contracts``:
no LangGraph, no FastAPI, no SQL, no model, no Git, no GitHub.
"""

from .dag import DependencyCycleError, dependents_of, topological_order
from .limits import PLANNING_RESERVATION_USD, Reservation, budget_refusal, resolve_limits
from .ports import (
    CreateGoalOutcome,
    GoalCursor,
    GoalEvent,
    GoalPatch,
    GoalRepository,
    IntegrationError,
    IntegrationProbe,
    MergeProbe,
    NewGoal,
    NewGoalTask,
    PersistedGoal,
    PersistedGoalEvent,
    PersistedGoalTask,
    ReserveOutcome,
    TaskPatch,
)
from .rules import (
    POLICY_VERSION,
    SUPERVISOR_ACTOR,
    NeedsHumanReason,
    failure_code_for_run,
    needs_human_ordinal,
    reminder_due,
    stage_ordinal,
    task_status_for_run,
)
from .state import (
    GOAL_TRANSITIONS,
    TASK_TRANSITIONS,
    can_transition_goal,
    can_transition_task,
)
from .tasks import fingerprint, single_task_text, task_text

__all__ = [
    "GOAL_TRANSITIONS",
    "PLANNING_RESERVATION_USD",
    "POLICY_VERSION",
    "SUPERVISOR_ACTOR",
    "TASK_TRANSITIONS",
    "CreateGoalOutcome",
    "DependencyCycleError",
    "GoalCursor",
    "GoalEvent",
    "GoalPatch",
    "GoalRepository",
    "IntegrationError",
    "IntegrationProbe",
    "MergeProbe",
    "NeedsHumanReason",
    "NewGoal",
    "NewGoalTask",
    "PersistedGoal",
    "PersistedGoalEvent",
    "PersistedGoalTask",
    "Reservation",
    "ReserveOutcome",
    "TaskPatch",
    "budget_refusal",
    "can_transition_goal",
    "can_transition_task",
    "dependents_of",
    "failure_code_for_run",
    "fingerprint",
    "needs_human_ordinal",
    "reminder_due",
    "resolve_limits",
    "single_task_text",
    "stage_ordinal",
    "task_status_for_run",
    "task_text",
    "topological_order",
]

"""Pure rules of the goal domain: dependency graph, state machines, reminders, budget,
task text and fingerprints. No I/O."""

from datetime import UTC, datetime, timedelta

import pytest

from app.contracts.goals import (
    GOAL_TERMINAL_STATUSES,
    TERMINAL_TASK_STATUSES,
    GoalLimits,
    GoalStatus,
    GoalTaskStatus,
    GoalUsage,
)
from app.contracts.lifecycle import DecisionStage, RunLifecycleStatus
from app.contracts.planning import DevelopmentPlan, DevelopmentTask, find_cycle
from app.goals import (
    DependencyCycleError,
    NeedsHumanReason,
    Reservation,
    budget_refusal,
    can_transition_goal,
    can_transition_task,
    dependents_of,
    failure_code_for_run,
    fingerprint,
    needs_human_ordinal,
    reminder_due,
    resolve_limits,
    task_status_for_run,
    task_text,
    topological_order,
)

T0 = datetime(2026, 10, 30, tzinfo=UTC)


def test_topological_order_is_deterministic_and_names_cycles():
    assert topological_order({"c": ["b"], "b": ["a"], "a": []}) == ["a", "b", "c"]
    assert topological_order({"b": [], "a": [], "c": ["a", "b"]}) == ["a", "b", "c"]
    with pytest.raises(DependencyCycleError) as exc:
        topological_order({"a": ["b"], "b": ["c"], "c": ["a"]})
    assert exc.value.cycle == ["a", "b", "c", "a"]
    with pytest.raises(ValueError, match="inexistentes"):
        topological_order({"a": ["zzz"]})
    with pytest.raises(ValueError, match="sí misma"):
        topological_order({"a": ["a"]})
    assert find_cycle({"a": [], "b": ["a"]}) is None
    assert dependents_of({"a": [], "b": ["a"], "c": ["b"], "d": []}, "a") == {"b", "c"}


def test_the_plan_contract_refuses_cycles_and_accepts_a_single_task():
    def task(key: str, deps: list[str]) -> DevelopmentTask:
        return DevelopmentTask(
            id=key, title=key, description=key, dependencies=deps, acceptance_criteria=["ok"]
        )

    with pytest.raises(ValueError, match="ciclo"):
        DevelopmentPlan(summary="s", tasks=[task("a", ["b"]), task("b", ["a"])])
    assert len(DevelopmentPlan(summary="s", tasks=[task("only", [])]).tasks) == 1


def test_goal_and_task_transitions_only_move_forward():
    for terminal in GOAL_TERMINAL_STATUSES:
        for other in GoalStatus:
            assert can_transition_goal(terminal, other) is (other is terminal)
    assert can_transition_goal(GoalStatus.PLANNING, GoalStatus.ACTIVE)
    assert can_transition_goal(GoalStatus.BLOCKED, GoalStatus.ACTIVE)
    assert not can_transition_goal(GoalStatus.ACTIVE, GoalStatus.PLANNING)
    for terminal in TERMINAL_TASK_STATUSES:
        for other in GoalTaskStatus:
            assert can_transition_task(terminal, other) is (other is terminal)
    assert can_transition_task(GoalTaskStatus.READY, GoalTaskStatus.CREATING)
    assert can_transition_task(GoalTaskStatus.WAITING_HUMAN, GoalTaskStatus.READY)  # stale base
    assert not can_transition_task(GoalTaskStatus.PUBLISHED, GoalTaskStatus.RUNNING)
    assert not can_transition_task(GoalTaskStatus.PENDING, GoalTaskStatus.RUNNING)


def test_run_statuses_project_onto_task_statuses():
    S, T = RunLifecycleStatus, GoalTaskStatus
    assert task_status_for_run(S.WAITING_APPROVAL) is T.WAITING_HUMAN
    assert task_status_for_run(S.PUBLISHED) is T.PUBLISHED
    assert task_status_for_run(S.COMPLETED) is T.PUBLISHED
    assert task_status_for_run(S.INTERRUPTED) is T.RUNNING  # recovery decides, the goal waits
    for status in (S.FAILED, S.REJECTED, S.EXPIRED, S.CANCELLED):
        assert task_status_for_run(status) is T.FAILED
    assert failure_code_for_run(S.FAILED, pending_stage=None, error_code="x") == "x"
    assert (
        failure_code_for_run(S.REJECTED, pending_stage=DecisionStage.SCOPE, error_code=None)
        == "rejected:scope"
    )
    assert (
        failure_code_for_run(S.EXPIRED, pending_stage=DecisionStage.PUBLISH, error_code=None)
        == "expired:publish"
    )
    assert failure_code_for_run(S.CANCELLED, pending_stage=None, error_code=None) == "cancelled"


def test_reminders_fire_at_half_the_window_or_at_the_configured_delay():
    since = T0
    assert not reminder_due(since=since, now=T0 + timedelta(hours=11), reminder_seconds=86_400)
    assert reminder_due(
        since=since,
        now=T0 + timedelta(hours=12, minutes=1),
        reminder_seconds=86_400,
        deadline=T0 + timedelta(hours=24),
    )
    assert not reminder_due(
        since=since,
        now=T0 + timedelta(hours=12, minutes=1),
        reminder_seconds=86_400,
    )
    assert reminder_due(since=since, now=T0 + timedelta(hours=24), reminder_seconds=86_400)
    # A deadline before ``since`` cannot shorten the window below zero.
    assert reminder_due(
        since=since, now=T0 + timedelta(hours=1), reminder_seconds=3600, deadline=T0
    )


def test_needs_human_ordinals_are_unique_per_task_reason_and_stage():
    seen = set()
    for task_ordinal in (-1, 0, 1, 7):
        for reason in NeedsHumanReason:
            for stage in (0, 1, 2):
                ordinal = needs_human_ordinal(task_ordinal, reason, stage=stage)
                assert ordinal >= 0 and ordinal not in seen
                seen.add(ordinal)
    with pytest.raises(ValueError):
        needs_human_ordinal(0, NeedsHumanReason.TASK_FAILED, stage=4)


def test_budget_is_reserved_before_spending_and_derived_defaults_are_explicit():
    limits = resolve_limits(None, planning_calls=8, per_task_calls=10)
    assert limits.max_total_model_calls == 8 + 5 * 10
    explicit = resolve_limits(
        GoalLimits(max_total_model_calls=12), planning_calls=8, per_task_calls=10
    )
    assert explicit.max_total_model_calls == 12
    usage = GoalUsage(tasks_created=1, reserved_model_calls=8, reserved_usd=20.0)
    assert budget_refusal(explicit, usage, Reservation(calls=4, usd=1.0)) is None
    assert budget_refusal(explicit, usage, Reservation(calls=5, usd=1.0)) == "budget_calls"
    assert budget_refusal(explicit, usage, Reservation(calls=1, usd=6.0)) == "budget_usd"
    full = GoalUsage(tasks_created=5)
    assert budget_refusal(explicit, full, Reservation(calls=0, usd=0.0)) == "budget_tasks"
    with pytest.raises(ValueError):
        GoalLimits(max_concurrency=2)  # Paso 1: one child at a time


def test_task_text_is_bounded_and_fingerprints_fold_case_and_whitespace():
    task = DevelopmentTask(
        id="t1",
        title="Añadir validate_email",
        description="x" * 5000,
        acceptance_criteria=["devuelve False con entradas vacías", "y" * 1000],
    )
    text = task_text(task, index=1, total=3, objective="o" * 2000)
    assert len(text) <= 4000
    assert text.startswith("Añadir validate_email. ")
    assert "Criterios de aceptación: devuelve False con entradas vacías;" in text
    assert "Parte 1/3 del objetivo:" in text
    a = fingerprint("repo", "Añade  greet(name)\n en src/app.py ")
    assert a == fingerprint("repo", "añade greet(name) en src/app.py")
    assert a != fingerprint("other", "añade greet(name) en src/app.py")
    assert len(a) == 64


# -- H2: stage-aware failure codes and reminders ---------------------------------------------------


def test_failure_codes_take_the_stage_from_the_error_code_or_the_recorded_decision():
    from app.contracts.lifecycle import RunLifecycleStatus as S
    from app.goals import NeedsHumanReason, needs_human_ordinal, reminders_due

    # The sweep clears ``pending_stage`` but writes ``expired:<stage>`` first (H2).
    assert (
        failure_code_for_run(S.EXPIRED, pending_stage=None, error_code="expired:scope")
        == "expired:scope"
    )
    assert (
        failure_code_for_run(S.EXPIRED, pending_stage=None, error_code="expired:publish")
        == "expired:publish"
    )
    # A rejection names the stage of the decision a human recorded.
    assert (
        failure_code_for_run(
            S.REJECTED, pending_stage=None, error_code=None, decided_stage=DecisionStage.SCOPE
        )
        == "rejected:scope"
    )
    # ``plan`` only when nothing better is known (a planning run has no other stage).
    assert failure_code_for_run(S.EXPIRED, pending_stage=None, error_code=None) == "expired:plan"
    # Every reason, stage pair owns a distinct ordinal, final notices included.
    ordinals = {
        needs_human_ordinal(t, reason, stage=stage)
        for t in (-1, 0, 1)
        for reason in NeedsHumanReason
        for stage in (0, 1, 2)
    }
    assert len(ordinals) == 3 * len(NeedsHumanReason) * 3
    # 24-hour window: midway at 12 h, final notice at 23 h, each exactly once per state.
    since, deadline = T0, T0 + timedelta(hours=24)
    kwargs = dict(since=since, reminder_seconds=86_400, final_notice_seconds=3_600)
    assert reminders_due(now=T0 + timedelta(hours=11), deadline=deadline, **kwargs) == (
        False,
        False,
    )
    assert reminders_due(now=T0 + timedelta(hours=12, minutes=1), deadline=deadline, **kwargs) == (
        True,
        False,
    )
    assert reminders_due(now=T0 + timedelta(hours=22, minutes=59), deadline=deadline, **kwargs) == (
        True,
        False,
    )
    assert reminders_due(now=T0 + timedelta(hours=23), deadline=deadline, **kwargs) == (True, True)
    # A 1-hour window has no room for a distinct final notice: only the midway one.
    short = T0 + timedelta(hours=1)
    assert reminders_due(now=T0 + timedelta(minutes=59), deadline=short, **kwargs) == (True, False)
    # Without a deadline there is never a final notice; the midway reminder waits a day.
    assert reminders_due(now=T0 + timedelta(hours=25), deadline=None, **kwargs) == (True, False)


def test_completed_counts_as_published_only_without_a_remote():
    from app.contracts.lifecycle import RunLifecycleStatus as S
    from app.goals import task_status_for_run

    assert task_status_for_run(S.COMPLETED) is GoalTaskStatus.PUBLISHED
    assert task_status_for_run(S.COMPLETED, remote=False) is GoalTaskStatus.PUBLISHED
    assert task_status_for_run(S.COMPLETED, remote=True) is GoalTaskStatus.FAILED  # P2
    assert task_status_for_run(S.PUBLISHED, remote=True) is GoalTaskStatus.PUBLISHED
    assert task_status_for_run(S.INTERRUPTED, remote=True) is GoalTaskStatus.RUNNING

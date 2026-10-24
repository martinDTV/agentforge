import pytest

from app.contracts.execution import RunStatus
from app.contracts.lifecycle import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    RunLifecycleStatus,
    can_transition,
    sources_for,
)

S = RunLifecycleStatus


def test_one_definition_for_api_and_persistence():
    assert RunStatus is RunLifecycleStatus
    assert set(ALLOWED_TRANSITIONS) == set(RunLifecycleStatus)


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES))
def test_terminal_states_never_change(terminal: RunLifecycleStatus):
    for target in RunLifecycleStatus:
        assert can_transition(terminal, target) is (target == terminal)


def test_recorded_decision_is_never_regressed_to_waiting():
    assert can_transition(S.WAITING_APPROVAL, S.RESUME_PENDING)
    assert not can_transition(S.RESUME_PENDING, S.WAITING_APPROVAL)
    assert not can_transition(S.RESUME_PENDING, S.INTERRUPTED)
    assert not can_transition(S.RESUME_PENDING, S.RUNNING)


def test_running_cannot_be_regressed_to_from_a_wait():
    assert not can_transition(S.WAITING_APPROVAL, S.RUNNING)
    assert can_transition(S.INTERRUPTED, S.RUNNING)  # recovery re-drives the graph


def test_sources_for_are_the_statuses_a_conditional_update_accepts():
    assert sources_for(S.WAITING_APPROVAL) == {S.RUNNING, S.INTERRUPTED, S.WAITING_APPROVAL}
    assert sources_for(S.RESUME_PENDING) == {S.WAITING_APPROVAL, S.RESUME_PENDING}
    assert S.RESUME_PENDING in sources_for(S.APPROVED)
    assert sources_for(S.RUNNING) == {S.RUNNING, S.INTERRUPTED}

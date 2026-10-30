"""Smart Router: deterministic rules, classifier through the LLM port, decision matrix
and escalation from the Scoper. The model never widens a route."""

from datetime import UTC, datetime

import pytest

from app.contracts.scope import ScopeAction, ScopeFile, ScopeProposal
from app.infrastructure.llm.errors import LLMTimeoutError
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.routing import (
    Route,
    RouteDecision,
    RouteMode,
    SmartRouter,
    TaskClassification,
    escalation_from_scope,
    mandatory_full_rules,
)

T0 = datetime(2026, 10, 27, tzinfo=UTC)
SMALL = "Añade la función greet(name) en src/app.py y una prueba unitaria en tests/test_greet.py"


@pytest.mark.parametrize(
    ("task", "rule"),
    [
        ("Crea una migración Alembic para la tabla usuarios", "data_or_schema_migration"),
        ("Cambia el flujo de autenticación OAuth del login", "auth_or_security"),
        (
            "Despliega el servicio con Kubernetes y actualiza el pipeline",
            "architecture_or_infrastructure",
        ),
        ("Introduce un breaking change en la API pública de pagos", "public_contract_change"),
        ("Rediseña todos los módulos para usar eventos", "multiple_subsystems"),
        ("arregla esto", "ambiguous_requirements"),
        ("Mejora el rendimiento donde haga falta, etc.", "ambiguous_requirements"),
        ("Add a JWT token refresh endpoint", "auth_or_security"),
    ],
)
def test_mandatory_rules_force_the_full_route(task: str, rule: str):
    assert rule in mandatory_full_rules(task)
    router = SmartRouter(llm=None, clock=lambda: T0)
    forced = router.decide(task, mode=RouteMode.FAST)  # even when fast is requested
    assert forced.route is Route.FULL and forced.reason.startswith("mandatory:")
    assert rule in forced.mandatory_rules and forced.classifier_used is False


def classified(**overrides) -> TaskClassification:
    base = dict(
        category="feature",
        risk="low",
        scope="few_files",
        ambiguous=False,
        confidence=0.9,
        rationale="pequeña",
    )
    base.update(overrides)
    return TaskClassification(**base)  # type: ignore[arg-type]


def test_auto_mode_uses_the_classifier_only_to_narrow_and_records_the_budget():
    assert mandatory_full_rules(SMALL) == []
    llm = FakeLLMAdapter()
    llm.register(TaskClassification, classified())
    router = SmartRouter(llm, confidence_threshold=0.7, clock=lambda: T0)
    fast = router.decide(SMALL, mode=RouteMode.AUTO, usage=lambda: {"calls": 1})
    assert fast.route is Route.FAST and fast.classifier_used and fast.confidence == 0.9
    assert fast.reason == "classified:feature/low/few_files" and fast.usage == {"calls": 1}
    assert fast.policy_version == "router-v1" and fast.decided_at == T0
    for verdict in (
        classified(risk="high"),
        classified(scope="multiple_subsystems"),
        classified(ambiguous=True),
        classified(confidence=0.55),
        classified(category="infrastructure"),
        classified(category="security"),
        classified(category="unknown"),
    ):
        llm.register(TaskClassification, verdict)
        assert router.decide(SMALL, mode=RouteMode.AUTO).route is Route.FULL, verdict
    # Requested modes bound the decision without consulting the model.
    assert router.decide(SMALL, mode=RouteMode.FULL).reason == "requested_full"
    assert router.decide(SMALL, mode=RouteMode.FAST).reason == "requested_fast"
    assert (
        router.decide(SMALL, mode=RouteMode.AUTO, allow_fast=False).reason
        == "policy_disallows_fast"
    )
    assert llm.calls[-1][2] is TaskClassification


def test_classifier_failure_or_absence_falls_back_to_full():
    llm = FakeLLMAdapter()
    llm.register_sequence(TaskClassification, [LLMTimeoutError("lento")])
    router = SmartRouter(llm, clock=lambda: T0)
    failed = router.decide(SMALL, mode=RouteMode.AUTO)
    assert failed.route is Route.FULL and failed.reason == "classifier_failed:llm_timeout"
    assert failed.classifier_used is True
    none = SmartRouter(llm=None, clock=lambda: T0).decide(SMALL, mode=RouteMode.AUTO)
    assert none.route is Route.FULL and none.reason == "classifier_unavailable"


def test_escalation_from_the_scoper_is_deterministic_and_only_from_fast():
    fast = RouteDecision(
        route=Route.FAST,
        mode=RouteMode.AUTO,
        reason="classified:feature/low/few_files",
        confidence=0.9,
        classifier_used=True,
        decided_at=T0,
    )
    small = ScopeProposal(
        summary="s", files=[ScopeFile(path="src/a.py", action=ScopeAction.EDIT, reason="r")]
    )
    assert escalation_from_scope(fast, small, accepted_files=1, max_files_per_run=6) is None
    high = small.model_copy(update={"complexity": "high"})
    escalated = escalation_from_scope(
        fast, high, accepted_files=1, max_files_per_run=6, clock=lambda: T0
    )
    assert escalated is not None and escalated.route is Route.FULL
    assert escalated.escalated_from is Route.FAST and "scoper_complexity_high" in escalated.reason
    assert escalated.confidence == 0.9 and escalated.classifier_used is True
    flagged = small.model_copy(update={"requires_planning": True})
    assert (
        "scoper_requires_planning"
        in escalation_from_scope(fast, flagged, accepted_files=1, max_files_per_run=6).reason
    )  # type: ignore[union-attr]
    many = escalation_from_scope(fast, small, accepted_files=6, max_files_per_run=6)
    assert many is not None and "files>=6" in many.reason
    full = fast.model_copy(update={"route": Route.FULL})
    assert escalation_from_scope(full, high, accepted_files=9, max_files_per_run=6) is None
    with pytest.raises(ValueError):
        ScopeProposal(summary="s", files=small.files, complexity="extreme")  # type: ignore[arg-type]


def test_planned_upstream_skips_planning_only_when_the_policy_allows_fast(fake_llm: FakeLLMAdapter):
    """ADR 0012, D4: a task of an approved goal plan is not planned again per task; the
    mandatory rules are still recorded and an explicit ``full`` or a policy win."""
    router = SmartRouter(None, clock=lambda: T0)
    risky = "Crea una migración Alembic para la tabla usuarios y su backfill"
    decision = router.decide(risky, mode=RouteMode.AUTO, allow_fast=True, planned_upstream=True)
    assert decision.route is Route.FAST
    assert decision.reason.startswith("planned_upstream:data_or_schema_migration")
    assert decision.mandatory_rules == ["data_or_schema_migration"]
    assert not decision.classifier_used
    assert router.decide(risky, allow_fast=False, planned_upstream=True).route is Route.FULL
    assert router.decide(risky, mode=RouteMode.FULL, planned_upstream=True).route is Route.FULL
    # Without the flag the same task goes full, as before.
    assert router.decide(risky, allow_fast=True).route is Route.FULL

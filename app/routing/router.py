"""Combine deterministic rules with an optional classification into a RouteDecision."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.contracts.scope import ScopeProposal
from app.infrastructure.llm.errors import LLMError
from app.infrastructure.llm.port import LLMPort

from .classifier import TaskClassifier
from .contracts import POLICY_VERSION, Route, RouteDecision, RouteMode, TaskClassification
from .rules import mandatory_full_rules, summarize

logger = logging.getLogger(__name__)

ESCALATION_FILES = 4  # a "small" task touches fewer files than this


def _now() -> datetime:
    return datetime.now(UTC)


class SmartRouter:
    def __init__(
        self,
        llm: LLMPort | None = None,
        *,
        confidence_threshold: float = 0.7,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._classifier = TaskClassifier(llm) if llm is not None else None
        self._threshold = confidence_threshold
        self._clock = clock

    def decide(
        self,
        task: str,
        *,
        mode: RouteMode = RouteMode.AUTO,
        allow_fast: bool = True,
        context: dict[str, Any] | None = None,
        usage: Callable[[], dict[str, Any]] | None = None,
    ) -> RouteDecision:
        """``mode`` is the request; rules and policy bound it; the model only advises."""
        rules = mandatory_full_rules(task)
        base: dict[str, Any] = {"mode": mode, "mandatory_rules": rules, "decided_at": self._clock()}
        if mode is RouteMode.FULL:
            return RouteDecision(route=Route.FULL, reason="requested_full", **base)
        if rules:
            return RouteDecision(
                route=Route.FULL, reason=f"mandatory:{summarize(rules)}"[:255], **base
            )
        if not allow_fast:
            return RouteDecision(route=Route.FULL, reason="policy_disallows_fast", **base)
        if mode is RouteMode.FAST:
            return RouteDecision(route=Route.FAST, reason="requested_fast", **base)
        if self._classifier is None:
            return RouteDecision(route=Route.FULL, reason="classifier_unavailable", **base)
        try:
            verdict: TaskClassification = self._classifier.classify(task, context=context)
        except LLMError as exc:
            logger.warning("Clasificador de ruta no disponible (%s): ruta completa", exc.code)
            return RouteDecision(
                route=Route.FULL,
                reason=f"classifier_failed:{exc.code}"[:255],
                classifier_used=True,
                usage=usage() if usage else {},
                **base,
            )
        spent = usage() if usage else {}
        fast = (
            verdict.risk == "low"
            and verdict.scope in ("single_file", "few_files")
            and not verdict.ambiguous
            and verdict.category not in ("infrastructure", "security", "unknown")
            and verdict.confidence >= self._threshold
        )
        reason = (
            f"classified:{verdict.category}/{verdict.risk}/{verdict.scope}"
            f"{'/ambiguous' if verdict.ambiguous else ''}"
        )
        return RouteDecision(
            route=Route.FAST if fast else Route.FULL,
            reason=reason[:255],
            confidence=verdict.confidence,
            classifier_used=True,
            usage=spent,
            **base,
        )


def escalation_from_scope(
    decision: RouteDecision,
    proposal: ScopeProposal,
    *,
    accepted_files: int,
    max_files_per_run: int,
    clock: Callable[[], datetime] = _now,
) -> RouteDecision | None:
    """After the Scoper, before any write: escalate a fast route when the proposal
    reveals more complexity than the classification assumed. Deterministic."""
    if decision.route is not Route.FAST:
        return None
    reasons: list[str] = []
    if proposal.complexity == "high":
        reasons.append("scoper_complexity_high")
    if proposal.requires_planning:
        reasons.append("scoper_requires_planning")
    if accepted_files >= max(ESCALATION_FILES, max_files_per_run):
        reasons.append(f"files>={accepted_files}")
    if not reasons:
        return None
    return RouteDecision(
        route=Route.FULL,
        mode=decision.mode,
        reason=f"escalated:{','.join(reasons)}"[:255],
        policy_version=POLICY_VERSION,
        confidence=decision.confidence,
        classifier_used=decision.classifier_used,
        mandatory_rules=list(decision.mandatory_rules),
        escalated_from=Route.FAST,
        decided_at=clock(),
    )

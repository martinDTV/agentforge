"""Synthetic notification fixtures: one scenario per mail kind, with no real data.

Used by the preview script (``scripts/notifications_preview.py``) and by the unit
suite so both exercise exactly the same cases. Every identifier, name and text
below is invented; nothing comes from a real run, repository or person.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.contracts.goals import GoalEventKind
from app.contracts.lifecycle import RunEventKind

from .policy import GoalContext, RunContext

RUN_ID = UUID("de8845f3-bd98-48cc-bdd4-1fe67d55ea75")
PLAN_RUN_ID = UUID("0903bb0c-e766-47ce-a2b8-6ee968d3ffb0")
GOAL_ID = UUID("7bd42940-419c-4c55-8e1d-a6ebb5f4c650")
MOMENT = datetime(2026, 10, 30, 18, 25, 30, tzinfo=UTC)
PR_URL = "https://github.com/example-org/sandbox-demo/pull/12"
BASE_SHA = "b04ba76475f2881098d2189d28a4e6182caad32c"
HEAD_SHA = "e4c101fb407352070a38481899338b74b95dd165"
MERGE_SHA = "c9d1f0a2b3c4d5e6f708192a3b4c5d6e7f8091a2"
SCOPE_DIGEST = "23567a52e1989a5cc380fe822d9f3b405bdf1cc2cf9ed0f4fc45a51b5b371179"
PUBLISH_DIGEST = "6b66f7afe9d1eda3269eab8ad0e549d3604c2174668dae3e13d6c939a7bec833"
TASK = (
    "Implementa validate_email(address) en src/mailcheck/validator.py según el README y añade "
    "pruebas unitarias en tests/test_validator.py"
)
OBJECTIVE = (
    "Añade validación de correo al registro de usuarios, con pruebas, y documenta el módulo "
    "mailcheck"
)


@dataclass(frozen=True)
class RunSample:
    name: str
    kind: RunEventKind
    payload: dict[str, Any]
    context: dict[str, Any]
    ordinal: int = 0
    run: RunContext | None = None


@dataclass(frozen=True)
class GoalSample:
    name: str
    kind: GoalEventKind
    payload: dict[str, Any]
    context: dict[str, Any]
    ordinal: int = 0


def run_context(**overrides: Any) -> RunContext:
    base: dict[str, Any] = dict(
        run_id=RUN_ID,
        project_id="sandbox-github",
        task=TASK,
        workflow_kind="development",
        repository_id="sandbox-github",
    )
    base.update(overrides)
    return RunContext(**base)


def goal_context() -> GoalContext:
    return GoalContext(
        goal_id=GOAL_ID,
        project_id="sandbox-github",
        repository_id="sandbox-github",
        objective=OBJECTIVE,
    )


PLAN = {
    "summary": "Implementar normalize_email e integrarlo en validate_email en dos tareas.",
    "tasks": [
        {"id": "T1", "title": "Crear normalize_email con pruebas", "dependencies": []},
        {"id": "T2", "title": "Integrar normalize_email en validate_email", "dependencies": ["T1"]},
    ],
    "risks": ["La parte local es sensible a mayúsculas según RFC 5321"],
}
ARCHITECTURE = {
    "summary": "Plan coherente; se aprueba con decisiones para mitigar riesgos.",
    "issues": [],
    "approved": True,
}
QA_CHECKS = [
    {"program": "ruff_check", "exit_code": 0},
    {"program": "ruff_format_check", "exit_code": 0},
    {"program": "pytest", "exit_code": 0},
]
PUBLICATION = {
    "state": "published",
    "remote_branch": f"agentforge/{RUN_ID}",
    "base_sha": BASE_SHA,
    "head_sha": HEAD_SHA,
    "pr_number": 12,
    "pr_url": PR_URL,
    "approved_by": "paula",
}


def run_samples() -> Iterator[RunSample]:
    yield RunSample("run_created", RunEventKind.RUN_CREATED, {"initiated_by": "martin"}, {})
    yield RunSample(
        "route_selected",
        RunEventKind.ROUTE_SELECTED,
        {
            "route": "fast",
            "mode": "auto",
            "reason": "classified:feature/low/few_files",
            "policy_version": "router-v1",
        },
        {},
    )
    yield RunSample(
        "route_escalated",
        RunEventKind.ROUTE_ESCALATED,
        {"route": "full", "escalated_from": "fast", "reason": "escalated:scoper_complexity_high"},
        {},
    )
    yield RunSample(
        "architect_reviewed",
        RunEventKind.ARCHITECT_REVIEWED,
        {"revision": 0, "approved": True, "issues": 0, "decisions": 4},
        {},
    )
    yield RunSample(
        "plan_pending",
        RunEventKind.WAITING_APPROVAL,
        {"stage": "plan"},
        {
            "stage": "plan",
            "plan_summary": PLAN["summary"],
            "tasks": PLAN["tasks"],
            "risks": PLAN["risks"],
            "architect_approved": True,
            "architecture_summary": ARCHITECTURE["summary"],
            "architecture_issues": [],
            "revision": 0,
            "route": "full",
        },
        run=run_context(run_id=PLAN_RUN_ID, workflow_kind="planning", task=OBJECTIVE),
    )
    yield RunSample(
        "scope_pending",
        RunEventKind.WAITING_APPROVAL,
        {"stage": "scope", "scope_digest": SCOPE_DIGEST, "deadline": "2026-10-31T18:25:30+00:00"},
        {
            "stage": "scope",
            "summary": "Editar src/mailcheck/validator.py y crear tests/test_validator.py",
            "files": [
                {"action": "edit", "path": "src/mailcheck/validator.py"},
                {"action": "create", "path": "tests/test_validator.py"},
            ],
            "tests": ["tests"],
            "risks": ["Las pruebas de aceptación no se modifican"],
            "rejected": 1,
            "hidden_paths": 1,
            "signals": 0,
            "repository": "example-org/sandbox-demo",
            "base_sha": BASE_SHA,
            "limits": {"max_files_per_run": 6},
        },
        ordinal=1,
    )
    yield RunSample(
        "publish_pending",
        RunEventKind.WAITING_APPROVAL,
        {
            "stage": "publish",
            "publish_digest": PUBLISH_DIGEST,
            "deadline": "2026-11-02T07:13:02+00:00",
        },
        {
            "stage": "publish",
            "repository": "example-org/sandbox-demo",
            "remote_branch": f"agentforge/{RUN_ID}",
            "base_branch": "main",
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
            "title": "[AgentForge] Implementa validate_email",
            "summary": "Implementación de validate_email con 25 pruebas propias.",
            "files": [
                {"change": "modified", "path": "src/mailcheck/validator.py"},
                {"change": "added", "path": "tests/test_validator.py"},
            ],
            "qa_checks": QA_CHECKS,
            "scan_findings": 0,
        },
        ordinal=2,
    )
    yield RunSample(
        "developer_started",
        RunEventKind.APPROVAL_RECORDED,
        {"stage": "scope", "approved": True, "approved_by": "omar", "scope_digest": SCOPE_DIGEST},
        {},
        ordinal=1,
    )
    yield RunSample(
        "developer_completed",
        RunEventKind.DEVELOPER_COMPLETED,
        {"files": 2},
        {
            "summary": "Implementa las siete reglas del README y añade pruebas.",
            "files": ["src/mailcheck/validator.py", "tests/test_validator.py"],
            "round": 0,
        },
    )
    yield RunSample("qa_started", RunEventKind.DEVELOPER_APPLIED, {"files": 2}, {})
    yield RunSample(
        "qa_changes_requested",
        RunEventKind.QA_REVIEWED,
        {"approved": False, "issues": 2, "checks": 3, "failed_checks": 1},
        {
            "summary": "pytest falla en test_total_too_long_invalid.",
            "issues": [
                {"code": "technical_checks_failed", "path": ""},
                {"code": "missing_length_check", "path": "src/mailcheck/validator.py"},
            ],
            "checks": [
                {"program": "ruff_check", "exit_code": 0},
                {"program": "ruff_format_check", "exit_code": 0},
                {"program": "pytest", "exit_code": 1},
            ],
            "round": 0,
        },
    )
    yield RunSample(
        "qa_approved",
        RunEventKind.QA_REVIEWED,
        {"approved": True, "issues": 0, "checks": 3, "failed_checks": 0},
        {
            "summary": "Todas las comprobaciones pasan.",
            "issues": [],
            "checks": QA_CHECKS,
            "round": 1,
        },
        ordinal=1,
    )
    yield RunSample(
        "pull_request_opened",
        RunEventKind.PULL_REQUEST_OPENED,
        {
            "pr_number": 12,
            "pr_url": PR_URL,
            "remote_branch": f"agentforge/{RUN_ID}",
            "draft": True,
            "approved_by": "paula",
        },
        {},
        run=run_context(publication=PUBLICATION),
    )
    yield RunSample("run_completed", RunEventKind.RUN_COMPLETED, {}, {})
    yield RunSample(
        "run_failed",
        RunEventKind.RUN_FAILED,
        {"node": "qa", "code": "correction_limit", "executed_by": "agentforge:qa"},
        {"message": "QA no aprobó tras 3 correcciones: technical_checks_failed"},
        run=run_context(error_code="correction_limit"),
    )
    yield RunSample(
        "plan_rejected_with_reason",
        RunEventKind.APPROVAL_RECORDED,
        {
            "stage": "plan",
            "approved": False,
            "rejected_by": "martin",
            "reason": (
                "El plan recorta espacios antes de validar y contradice tests/test_acceptance.py"
            ),
        },
        {},
        run=run_context(run_id=PLAN_RUN_ID, workflow_kind="planning", task=OBJECTIVE),
    )
    yield RunSample(
        "scope_rejected_without_reason",
        RunEventKind.APPROVAL_RECORDED,
        {"stage": "scope", "approved": False, "rejected_by": "omar", "scope_digest": SCOPE_DIGEST},
        {},
        ordinal=1,
    )
    yield RunSample(
        "publish_rejected",
        RunEventKind.APPROVAL_RECORDED,
        {
            "stage": "publish",
            "approved": False,
            "rejected_by": "paula",
            "reason": "Falta cubrir el caso de dominio con guion final",
        },
        {},
        ordinal=2,
    )
    yield RunSample("run_cancelled", RunEventKind.RUN_CANCELLED, {"cancelled_by": "martin"}, {})
    yield RunSample(
        "run_expired",
        RunEventKind.RUN_EXPIRED,
        {},
        {},
        run=run_context(pending_stage="scope"),
    )


def goal_samples() -> Iterator[GoalSample]:
    roster = [
        {
            "task_key": "T1",
            "title": "Crear normalize_email",
            "status": "merged",
            "pr_number": 12,
            "pr_url": PR_URL,
            "merged": True,
            "code": None,
        },
        {
            "task_key": "T2",
            "title": "Integrar normalize_email",
            "status": "published",
            "pr_number": 13,
            "pr_url": PR_URL.replace("/12", "/13"),
            "merged": False,
            "code": None,
        },
    ]
    usage = {
        "tasks_created": 2,
        "reserved_model_calls": 28,
        "reserved_usd": 20.0,
        "llm_calls_used": 11,
    }
    yield GoalSample(
        "goal_created",
        GoalEventKind.GOAL_CREATED,
        {"decomposition": "plan", "repository_id": "sandbox-github", "tasks": 0},
        {},
    )
    yield GoalSample(
        "goal_plan_requested", GoalEventKind.PLAN_REQUESTED, {"run_id": str(PLAN_RUN_ID)}, {}
    )
    yield GoalSample(
        "goal_task_ready", GoalEventKind.TASK_READY, {"task_key": "T2", "dependencies": 1}, {}, 1
    )
    yield GoalSample(
        "goal_task_started",
        GoalEventKind.CHILD_CREATED,
        {"task_key": "T1", "run_id": str(RUN_ID), "attempt": 1, "planned_upstream": True},
        {},
    )
    yield GoalSample(
        "goal_task_published",
        GoalEventKind.TASK_PUBLISHED,
        {
            "task_key": "T1",
            "run_id": str(RUN_ID),
            "pr_number": 12,
            "pr_url": PR_URL,
            "dependents": 1,
        },
        {},
    )
    yield GoalSample(
        "goal_dependency_merged",
        GoalEventKind.DEPENDENCY_MERGED,
        {"task_key": "T1", "pr_number": 12, "merge_commit_sha": MERGE_SHA, "base_after": MERGE_SHA},
        {},
    )
    yield GoalSample(
        "goal_needs_human_merge",
        GoalEventKind.NEEDS_HUMAN,
        {
            "reason": "merge_pending",
            "task_key": "T1",
            "run_id": str(RUN_ID),
            "pr_number": 12,
            "pr_url": PR_URL,
        },
        {},
        12,
    )
    yield GoalSample(
        "goal_needs_human_decision",
        GoalEventKind.NEEDS_HUMAN,
        {
            "reason": "decision_pending",
            "task_key": "T2",
            "run_id": str(RUN_ID),
            "stage": "scope",
            "deadline": "2026-10-31T18:25:30+00:00",
        },
        {},
        13,
    )
    yield GoalSample(
        "goal_needs_human_task_failed",
        GoalEventKind.NEEDS_HUMAN,
        {
            "reason": "task_failed",
            "task_key": "T2",
            "run_id": str(RUN_ID),
            "code": "correction_limit",
        },
        {},
        14,
    )
    yield GoalSample(
        "goal_failed_plan_rejected",
        GoalEventKind.GOAL_FAILED,
        {
            "tasks": 0,
            "succeeded": 0,
            "failed": 0,
            "code": "plan_rejected:plan",
            "decided_by": "martin",
            "reason": "El plan contradice las pruebas de aceptación",
        },
        {
            "tasks": [],
            "usage": {
                "tasks_created": 0,
                "reserved_model_calls": 8,
                "reserved_usd": 4.0,
                "llm_calls_used": 2,
            },
        },
    )
    yield GoalSample(
        "goal_completed",
        GoalEventKind.GOAL_COMPLETED,
        {"tasks": 2, "succeeded": 2, "failed": 0},
        {"tasks": roster, "usage": usage},
    )
    yield GoalSample(
        "goal_partial",
        GoalEventKind.GOAL_PARTIAL,
        {"tasks": 2, "succeeded": 1, "failed": 1, "code": "pr_closed"},
        {
            "tasks": [roster[0], {**roster[1], "status": "skipped", "code": "dependency:T1"}],
            "usage": usage,
        },
    )
    yield GoalSample(
        "goal_cancelled",
        GoalEventKind.GOAL_CANCELLED,
        {"tasks": 2, "succeeded": 1, "failed": 1, "decided_by": "martin"},
        {"tasks": roster, "usage": usage},
    )
    yield GoalSample(
        "goal_expired",
        GoalEventKind.GOAL_EXPIRED,
        {"tasks": 2, "succeeded": 1, "failed": 1, "code": "goal_deadline"},
        {"tasks": roster, "usage": usage},
    )

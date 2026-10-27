"""Smart Router inside the governed development graph, with fakes: fast route, full
route with Planner/Architect and a plan decision, escalation from the Scoper before
any write, mandatory rules over a requested fast mode, classifier failure, recovery
of the chosen route from the checkpoint, and the notifications every transition
leaves in the outbox."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agents.development import DeveloperChanges, FileEdit, QAReview
from app.contracts.lifecycle import DecisionStage, RunEventKind, RunLifecycleStatus, WorkflowKind
from app.contracts.planning import (
    ArchitectureDecision,
    ArchitectureReview,
    DevelopmentPlan,
    DevelopmentTask,
)
from app.contracts.routing import Route, RouteMode
from app.contracts.scope import ScopeAction, ScopeFile, ScopeProposal
from app.infrastructure.db import (
    InMemoryNotificationOutbox,
    InMemoryRouteDecisionRepository,
    InMemoryRunRepository,
    InMemoryToolInvocationRepository,
)
from app.infrastructure.llm.errors import LLMTimeoutError
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.infrastructure.notifications import FakeEmailSender
from app.infrastructure.sandbox import FakeSandbox
from app.notifications import (
    NotificationEnqueuer,
    NotificationKind,
    NotificationPolicy,
    NotificationWorker,
)
from app.orchestration.graph import build_graph
from app.orchestration.runs import RunService
from app.orchestration.scoped_development import DevelopmentRuntime, build_scoped_development_graph
from app.projects import ProjectPolicyRegistry, RepositoryPolicy
from app.routing import SmartRouter, TaskClassification
from app.security import AuthorizationPolicy, PermissionDeniedError, Principal
from tests.unit.git_fixtures import make_sandbox

pytestmark = pytest.mark.anyio
S = RunLifecycleStatus
PROJECT = "sandbox-router"
REPO = "sandbox-router"
SMALL_TASK = (
    "Añade la función greet(name) en src/app.py y una prueba unitaria en tests/test_greet.py"
)
RISKY_TASK = (
    "Cambia la autenticación OAuth del login y añade una migración Alembic para la tabla usuarios"
)


def who(subject: str, roles: set[str]) -> Principal:
    return Principal(
        subject=subject,
        kind="user",
        issuer="https://issuer.test",
        roles=frozenset(roles),
        projects=frozenset({PROJECT}),
    )


OPERATOR = who("olivia", {"project_operator"})
VIEWER = who("victor", {"viewer"})


def low_risk() -> TaskClassification:
    return TaskClassification(
        category="feature",
        risk="low",
        scope="few_files",
        ambiguous=False,
        confidence=0.9,
        rationale="pequeña",
    )


def plan() -> DevelopmentPlan:
    return DevelopmentPlan(
        summary="plan",
        tasks=[
            DevelopmentTask(id="t1", title="greet", description="d", acceptance_criteria=["ok"])
        ],
    )


def review(approved: bool = True) -> ArchitectureReview:
    return ArchitectureReview(
        summary="rev",
        decisions=[ArchitectureDecision(id="d1", title="t", decision="d", rationale="r")],
        issues=[] if approved else ["falta"],
        approved=approved,
    )


class RouterWorld:
    def __init__(
        self, tmp_path: Path, *, router: bool = True, notify: bool = True, complexity: str = "low"
    ) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.box = make_sandbox(tmp_path, project=PROJECT, repository=REPO)
        self.model = FakeLLMAdapter()
        self.model.register(TaskClassification, low_risk())
        self.model.register(DevelopmentPlan, plan())
        self.model.register(ArchitectureReview, review(True))
        self.model.register(
            ScopeProposal,
            ScopeProposal(
                summary="s",
                files=[
                    ScopeFile(path="src/app.py", action=ScopeAction.EDIT, reason="r"),
                    ScopeFile(path="tests/test_greet.py", action=ScopeAction.CREATE, reason="r"),
                ],
                tests=["tests"],
                complexity=complexity,  # type: ignore[arg-type]
            ),
        )
        self.model.register(
            DeveloperChanges,
            DeveloperChanges(
                summary="hecho",
                edits=[
                    FileEdit(path="src/app.py", content="def greet(n):\n    return n\n"),
                    FileEdit(path="tests/test_greet.py", content="def test_ok():\n    assert 1\n"),
                ],
            ),
        )
        self.model.register(QAReview, QAReview(approved=True, summary="ok", issues=[]))
        self.now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        self.enqueuer = (
            NotificationEnqueuer(
                NotificationPolicy(), recipients=["ops@example.com"], level="verbose"
            )
            if notify
            else None
        )
        self.outbox = InMemoryNotificationOutbox(lambda: self.now)
        self.repository = InMemoryRunRepository(
            lambda: self.now, notifications=self.enqueuer, outbox=self.outbox
        )
        self.routes = InMemoryRouteDecisionRepository()
        self.policy = RepositoryPolicy(
            repository_id=REPO,
            project_id=PROJECT,
            writable_globs=["src/**", "tests/**"],
            scope_approval_ttl_seconds=3600,
        )
        self.policies = ProjectPolicyRegistry([self.policy])
        self.runtime = DevelopmentRuntime(
            policies=self.policies,
            workspaces=self.box.manager,
            git=self.box.adapter,
            sandbox=FakeSandbox(runner_id="fake"),
            audit=InMemoryToolInvocationRepository(),
            llm=self.model,
            clock=lambda: self.now,
            router=SmartRouter(self.model, clock=lambda: self.now) if router else None,
            route_decisions=self.routes,
        )
        self.graph = build_scoped_development_graph(self.runtime)

    def service(self, *, default_mode: RouteMode = RouteMode.AUTO) -> RunService:
        return RunService(
            build_graph(self.model),
            self.repository,
            authorization=AuthorizationPolicy(separation_of_duties=False),
            graphs={
                WorkflowKind.PLANNING: build_graph(self.model),
                WorkflowKind.DEVELOPMENT: self.graph,
            },
            policies=self.policies,
            clock=lambda: self.now,
            default_route_mode=default_mode,
        )

    async def start(
        self, service: RunService, task: str = SMALL_TASK, mode: RouteMode | None = None
    ) -> str:
        result = await service.create(
            task,
            OPERATOR,
            project_id=PROJECT,
            workflow_kind=WorkflowKind.DEVELOPMENT,
            repository_id=REPO,
            route_mode=mode,
        )
        return result.response.run_id

    async def kinds(self, run_id: str) -> list[RunEventKind]:
        return [
            e.kind
            for e in await self.repository.list_events(uuid.UUID(run_id), after_id=None, limit=200)
        ]

    async def mails(self) -> list[NotificationKind]:
        return [i.kind for i in sorted(await self.outbox.list_items(limit=200), key=lambda i: i.id)]


async def test_a_small_task_takes_the_fast_route_and_is_notified_at_each_transition(tmp_path: Path):
    world = RouterWorld(tmp_path)
    service = world.service()
    run_id = await world.start(service)
    response = await service.get(run_id, OPERATOR)
    assert response.status is S.WAITING_APPROVAL and response.pending_stage is DecisionStage.SCOPE
    assert response.route is Route.FAST and response.route_mode is RouteMode.AUTO
    decisions = await world.routes.list_for_run(uuid.UUID(run_id))
    assert [(d.route, d.reason, d.classifier_used) for d in decisions] == [
        (Route.FAST, "classified:feature/low/few_files", True)
    ]
    kinds = await world.kinds(run_id)
    assert kinds[:3] == [
        RunEventKind.RUN_CREATED,
        RunEventKind.ROUTE_SELECTED,
        RunEventKind.SCOPE_PROPOSED,
    ]
    assert RunEventKind.PLANNER_COMPLETED not in kinds
    pending = (await service.decisions(run_id, OPERATOR)).pending
    assert pending is not None and pending.stage is DecisionStage.SCOPE
    done = await service.approve(run_id, True, OPERATOR, scope_digest=pending.scope_digest)
    assert done.status is S.COMPLETED and done.route is Route.FAST
    assert world.model.calls[0][2] is TaskClassification  # exactly one classification call
    assert await world.mails() == [
        NotificationKind.RUN_CREATED,
        NotificationKind.ROUTE_SELECTED,
        NotificationKind.SCOPE_PENDING,
        NotificationKind.DEVELOPER_STARTED,
        NotificationKind.DEVELOPER_COMPLETED,
        NotificationKind.QA_STARTED,
        NotificationKind.QA_APPROVED,
        NotificationKind.RUN_COMPLETED,
    ]
    sender = FakeEmailSender()
    report = await NotificationWorker(world.outbox, sender, clock=lambda: world.now).run_once()
    assert report.sent == 8 and len({m.message_id for m in sender.sent}) == 8
    assert all(
        m.references == [f"<agentforge.{run_id}.thread@agentforge.local>"] for m in sender.sent
    )
    assert (
        "Ruta seleccionada: fast" in sender.sent[1].subject
        and "Scoper prepara el alcance" in sender.sent[1].text
    )


async def test_a_risky_task_takes_the_full_route_with_a_human_plan_decision(tmp_path: Path):
    world = RouterWorld(tmp_path)
    service = world.service()
    run_id = await world.start(
        service, RISKY_TASK, mode=RouteMode.FAST
    )  # fast requested, rules win
    response = await service.get(run_id, OPERATOR)
    assert response.status is S.WAITING_APPROVAL and response.pending_stage is DecisionStage.PLAN
    assert response.route is Route.FULL and response.route_mode is RouteMode.FAST
    decision = (await world.routes.list_for_run(uuid.UUID(run_id)))[0]
    assert (
        decision.reason.startswith("mandatory:") and "auth_or_security" in decision.mandatory_rules
    )
    assert decision.classifier_used is False  # the model was never consulted
    kinds = await world.kinds(run_id)
    assert [
        RunEventKind.ROUTE_SELECTED,
        RunEventKind.PLANNER_COMPLETED,
        RunEventKind.ARCHITECT_REVIEWED,
        RunEventKind.WAITING_APPROVAL,
    ] == [
        k
        for k in kinds
        if k
        in (
            RunEventKind.ROUTE_SELECTED,
            RunEventKind.PLANNER_COMPLETED,
            RunEventKind.ARCHITECT_REVIEWED,
            RunEventKind.WAITING_APPROVAL,
        )
    ][:4]
    pending = (await service.decisions(run_id, OPERATOR)).pending
    assert (
        pending is not None
        and pending.stage is DecisionStage.PLAN
        and pending.payload["plan"]["summary"] == "plan"
    )
    with pytest.raises(PermissionDeniedError):
        await service.approve(run_id, True, VIEWER, stage=DecisionStage.PLAN)
    # Plan approved -> Scoper -> scope approval (the second human decision), then Developer/QA.
    after_plan = await service.approve(run_id, True, OPERATOR, stage=DecisionStage.PLAN)
    assert (
        after_plan.status is S.WAITING_APPROVAL and after_plan.pending_stage is DecisionStage.SCOPE
    )
    scope = (await service.decisions(run_id, OPERATOR)).pending
    assert scope is not None
    done = await service.approve(run_id, True, OPERATOR, scope_digest=scope.scope_digest)
    assert done.status is S.COMPLETED and done.route is Route.FULL
    taken = (await service.decisions(run_id, OPERATOR)).decisions
    assert [d.stage for d in taken] == [DecisionStage.PLAN, DecisionStage.SCOPE]
    mails = await world.mails()
    assert mails[:4] == [
        NotificationKind.RUN_CREATED,
        NotificationKind.ROUTE_SELECTED,
        NotificationKind.PLANNING_COMPLETED,
        NotificationKind.PLAN_PENDING,
    ]
    assert NotificationKind.SCOPE_PENDING in mails and mails[-1] is NotificationKind.RUN_COMPLETED
    # A rejected plan ends the run without any Scoper or write.
    world2 = RouterWorld(tmp_path / "two")
    service2 = world2.service()
    run2 = await world2.start(service2, RISKY_TASK)
    rejected = await service2.approve(run2, False, OPERATOR, stage=DecisionStage.PLAN)
    assert rejected.status is S.REJECTED
    assert ScopeProposal not in [c[2] for c in world2.model.calls]
    assert world2.box.manager.get(uuid.UUID(run2), PROJECT) is None


async def test_the_scoper_can_escalate_a_fast_route_before_any_write(tmp_path: Path):
    world = RouterWorld(tmp_path, complexity="high")
    service = world.service()
    run_id = await world.start(service)
    response = await service.get(run_id, OPERATOR)
    # Classified fast, but the Scoper saw high complexity: Planner/Architect run and a
    # plan decision is required before the (already prepared) scope can be approved.
    assert response.status is S.WAITING_APPROVAL and response.pending_stage is DecisionStage.PLAN
    assert response.route is Route.FULL
    decisions = await world.routes.list_for_run(uuid.UUID(run_id))
    assert [(d.route, d.escalated_from) for d in decisions] == [
        (Route.FAST, None),
        (Route.FULL, Route.FAST),
    ]
    assert "scoper_complexity_high" in decisions[1].reason
    kinds = await world.kinds(run_id)
    assert kinds.index(RunEventKind.ROUTE_ESCALATED) < kinds.index(RunEventKind.PLANNER_COMPLETED)
    worktree = world.box.manager.get(uuid.UUID(run_id), PROJECT)
    assert (
        worktree is not None and not (worktree.path / "tests/test_greet.py").exists()
    )  # nothing written
    after_plan = await service.approve(run_id, True, OPERATOR, stage=DecisionStage.PLAN)
    assert after_plan.pending_stage is DecisionStage.SCOPE  # straight to the existing proposal
    assert len([c for c in world.model.calls if c[2] is ScopeProposal]) == 1  # Scoper not re-run
    scope = (await service.decisions(run_id, OPERATOR)).pending
    assert scope is not None
    assert (
        await service.approve(run_id, True, OPERATOR, scope_digest=scope.scope_digest)
    ).status is S.COMPLETED
    assert NotificationKind.ROUTE_ESCALATED in await world.mails()


async def test_classifier_failure_or_no_router_never_skips_the_human_decisions(tmp_path: Path):
    world = RouterWorld(tmp_path)
    world.model.register_sequence(TaskClassification, [LLMTimeoutError("lento")])
    service = world.service()
    run_id = await world.start(service)
    response = await service.get(run_id, OPERATOR)
    assert response.route is Route.FULL and response.pending_stage is DecisionStage.PLAN
    assert (await world.routes.list_for_run(uuid.UUID(run_id)))[
        0
    ].reason == "classifier_failed:llm_timeout"
    # Explicit full mode, no classification at all.
    run_full = await world.start(service, mode=RouteMode.FULL)
    assert (await service.get(run_full, OPERATOR)).pending_stage is DecisionStage.PLAN
    assert (await world.routes.list_for_run(uuid.UUID(run_full)))[0].reason == "requested_full"
    # Without a router the historical behaviour stays: fast, recorded as such.
    legacy = RouterWorld(tmp_path / "legacy", router=False)
    legacy_service = legacy.service()
    run_legacy = await legacy.start(legacy_service)
    assert (await legacy_service.get(run_legacy, OPERATOR)).route is Route.FAST
    assert (await legacy.routes.list_for_run(uuid.UUID(run_legacy)))[
        0
    ].reason == "router_unconfigured"
    # The route survives a process restart: a second service reads it from the row/checkpoint.
    other = world.service()
    assert (await other.get(run_id, OPERATOR)).route is Route.FULL
    assert ScopeProposal not in [c[2] for c in world.model.calls]  # no scope yet in the full route


async def test_project_policy_can_forbid_the_fast_route(tmp_path: Path):
    world = RouterWorld(tmp_path)
    world.policies = ProjectPolicyRegistry(
        [world.policy.model_copy(update={"allow_fast_route": False})]
    )
    world.runtime.policies = world.policies
    service = world.service()
    run_id = await world.start(service, mode=RouteMode.FAST)
    assert (await service.get(run_id, OPERATOR)).route is Route.FULL
    assert (await world.routes.list_for_run(uuid.UUID(run_id)))[0].reason == "policy_disallows_fast"


async def test_cancellation_and_expiry_notify_and_stop_everything(tmp_path: Path):
    world = RouterWorld(tmp_path)
    service = world.service()
    run_id = await world.start(service)
    cancelled = await service.cancel(run_id, OPERATOR)
    assert cancelled.status is S.CANCELLED
    assert (await world.mails())[-1] is NotificationKind.RUN_CANCELLED
    run2 = await world.start(service)
    world.now = world.now.replace(hour=14)
    assert [str(r.id) for r in await service.expire_waiting()] == [run2]
    assert (await world.mails())[-1] is NotificationKind.RUN_EXPIRED
    assert (await service.get(run2, OPERATOR)).status is S.EXPIRED

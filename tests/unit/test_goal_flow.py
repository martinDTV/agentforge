"""Fase 8 end to end with the real ``RunService`` and graphs: a planned goal on a mirrored
GitHub repository (FakeGitHub + a ``file://`` remote) runs its first task through the human
approvals, publishes a draft pull request, waits until a human merges it (squash: the head
SHA never reaches ``main``), verifies the merge in the mirror and only then starts the
dependent task from a base that contains the merge commit. No model, no network, no Runner."""

import uuid
from pathlib import Path

import pytest

from app.contracts.goals import CreateGoal, GoalDecomposition, GoalEventKind, GoalStatus
from app.contracts.goals import GoalTaskStatus as T
from app.contracts.lifecycle import DecisionStage, RunEventKind, RunLifecycleStatus
from app.contracts.planning import ArchitectureReview, DevelopmentPlan, DevelopmentTask
from app.infrastructure.db import InMemoryGoalRepository
from app.infrastructure.github.integration import GitHubIntegrationProbe
from app.orchestration.goals import GoalCoordinator, GoalService, SupervisorLimits
from app.orchestration.limits import RunLimits
from app.security import DELEGATION_ISSUER, AuthorizationPolicy
from tests.unit.git_fixtures import git
from tests.unit.test_mirror_service import PROJECT, REPO
from tests.unit.test_publication_flow import OMAR, PAULA, PublishWorld
from tests.unit.test_scoped_development_mirrors import OPERATOR

pytestmark = pytest.mark.anyio
S = RunLifecycleStatus
OBJECTIVE = "Añade greet(name) en src/app.py con pruebas y después documenta el módulo"


def plan() -> DevelopmentPlan:
    return DevelopmentPlan(
        summary="dos pasos",
        tasks=[
            DevelopmentTask(
                id="greet",
                title="Implementar greet",
                description="Añadir greet(name) en src/app.py con prueba",
                acceptance_criteria=["greet devuelve el nombre"],
            ),
            DevelopmentTask(
                id="docs",
                title="Documentar greet",
                description="Documentar greet en src/app.py",
                dependencies=["greet"],
                acceptance_criteria=["docstring presente"],
            ),
        ],
    )


class GoalWorld(PublishWorld):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.model.register(DevelopmentPlan, plan())
        self.model.register(
            ArchitectureReview, ArchitectureReview(summary="ok", decisions=[], approved=True)
        )
        self.run_service = self.service()
        self.goal_repository = InMemoryGoalRepository(clock=lambda: self.now)
        self.probe = GitHubIntegrationProbe(
            github=self.lab.client,
            tokens=self.lab.tokens,
            mirrors=self.lab.service,
            git=self.lab.adapter,
            policies=self.lab.policies,
            mirror_syncs=self.syncs,
        )
        self.coordinator = GoalCoordinator(
            goals=self.goal_repository,
            runs=self.repository,
            launcher=self.run_service,
            policies=self.lab.policies,
            planning_limits=RunLimits(),
            probe=self.probe,
            limits=SupervisorLimits(),
            clock=lambda: self.now,
            owner="goal-flow-test",
        )
        self.goal_service = GoalService(
            self.coordinator,
            authorization=AuthorizationPolicy(separation_of_duties=False),
            policies=self.lab.policies,
            runs=self.repository,
            clock=lambda: self.now,
        )

    async def tick(self, goal_id: uuid.UUID) -> None:
        await self.coordinator.reconcile(goal_id)
        await self.coordinator.wait_until_idle()

    async def tasks(self, goal_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return {t.task_key: t for t in await self.goal_repository.list_tasks(goal_id)}

    def squash_merge(self, branch: str, number: int) -> str:
        """What a human does on github.com: squash-merge the draft PR into ``main``."""
        git("checkout", "-q", "main", cwd=self.lab.remote, home=self.lab.home)
        git("merge", "--squash", branch, cwd=self.lab.remote, home=self.lab.home)
        message = f"Merge pull request #{number}"
        git("commit", "-q", "-m", message, cwd=self.lab.remote, home=self.lab.home)
        sha = self.lab.remote_head("main")
        self.lab.github.merge_pull_request(number, merge_commit_sha=sha, squash=True)
        return sha


async def test_a_planned_goal_integrates_through_verified_merges_only(tmp_path: Path):
    world = GoalWorld(tmp_path)
    service = world.run_service
    body = CreateGoal(
        objective=OBJECTIVE,
        project_id=PROJECT,
        repository_id=REPO,
        decomposition=GoalDecomposition.PLAN,
    )
    created = await world.goal_service.create(body, OPERATOR)
    await world.coordinator.wait_until_idle()
    goal_id = uuid.UUID(created.response.goal_id)
    goal = await world.goal_repository.get_goal(goal_id)
    assert goal is not None and goal.status is GoalStatus.PLANNING
    assert goal.plan_run_id is not None
    plan_run = str(goal.plan_run_id)
    planning = await service.get(plan_run, OPERATOR)
    assert planning.status is S.WAITING_APPROVAL and planning.workflow_kind.value == "planning"
    assert planning.owner == OPERATOR.subject  # the human owns the plan run
    assert planning.plan is not None and [t.id for t in planning.plan.tasks] == ["greet", "docs"]

    # 1. The human approves the plan: tasks are materialised, the first child starts and
    #    stops at its scope decision; Planner/Architect are not repeated (planned_upstream).
    await service.approve(plan_run, True, OMAR)
    await world.tick(goal_id)
    tasks = await world.tasks(goal_id)
    assert [k for k in tasks] == ["greet", "docs"]
    greet, docs = tasks["greet"], tasks["docs"]
    assert greet.status is T.WAITING_HUMAN and docs.status is T.PENDING
    assert greet.run_id is not None
    child = await service.get(str(greet.run_id), OPERATOR)
    assert child.status is S.WAITING_APPROVAL and child.pending_stage is DecisionStage.SCOPE
    assert child.owner == OPERATOR.subject and child.route == "fast"
    events = await world.repository.list_events(greet.run_id, after_id=None, limit=100)
    route = next(e for e in events if e.kind is RunEventKind.ROUTE_SELECTED)
    assert str(route.payload["reason"]).startswith("planned_upstream")
    assert not any(e.kind is RunEventKind.PLANNER_COMPLETED for e in events)
    row = await world.repository.get_run(greet.run_id)
    assert row is not None and "Parte 1/2 del objetivo" in row.task
    assert greet.base_sha == world.lab.remote_head("main") and greet.scope_paths
    assert greet.scope_digest == child_digest(await service.decisions(str(greet.run_id), OPERATOR))

    # 2. Humans approve scope and publication: a draft PR exists, the goal keeps waiting.
    pending = (await service.decisions(str(greet.run_id), OPERATOR)).pending
    assert pending is not None
    await service.approve(str(greet.run_id), True, OMAR, scope_digest=pending.scope_digest)
    pending = (await service.decisions(str(greet.run_id), OPERATOR)).pending
    assert pending is not None and pending.stage is DecisionStage.PUBLISH
    published = await service.approve(
        str(greet.run_id), True, PAULA, publish_digest=pending.publish_digest
    )
    assert published.status is S.PUBLISHED and published.publication is not None
    pr_number = int(published.publication["pr_number"])
    branch = str(published.publication["remote_branch"])
    await world.coordinator.on_run_terminal(await world.repository.get_run(greet.run_id))  # type: ignore[arg-type]
    await world.coordinator.wait_until_idle()
    tasks = await world.tasks(goal_id)
    assert tasks["greet"].status is T.PUBLISHED and tasks["greet"].pr_number == pr_number
    assert tasks["docs"].status is T.PENDING  # an open PR is not integration
    for _ in range(2):
        await world.tick(goal_id)
    assert (await world.tasks(goal_id))["docs"].status is T.PENDING
    assert len(world.repository.runs) == 2  # the plan run and one child

    # 3. A human squash-merges the PR: GitHub reports a merge commit that is NOT the head.
    head_before = world.lab.remote_head("main")
    merge_sha = world.squash_merge(branch, pr_number)
    assert merge_sha != head_before and merge_sha != published.publication["head_sha"]
    await world.tick(goal_id)
    tasks = await world.tasks(goal_id)
    assert tasks["greet"].status is T.MERGED
    evidence = tasks["greet"].merge_evidence
    assert evidence is not None and evidence.merge_commit_sha == merge_sha
    assert evidence.verified_in_mirror is True and evidence.base_after == merge_sha
    # The dependent task started from a base that contains the merge commit.
    docs = tasks["docs"]
    assert docs.status is T.WAITING_HUMAN and docs.run_id is not None
    assert docs.base_sha == merge_sha
    worktree = world.manager.get(docs.run_id, PROJECT)
    assert worktree is not None
    assert (worktree.path / "src/app.py").read_text() == "def greet(n):\n    return n\n"
    kinds = [e.kind for e in await world.goal_repository.list_events(goal_id)]
    assert GoalEventKind.DEPENDENCY_MERGED in kinds and GoalEventKind.TASK_BASE_RECORDED in kinds
    goal = await world.goal_repository.get_goal(goal_id)
    assert goal is not None and goal.status is GoalStatus.ACTIVE
    assert goal.usage.tasks_created == 2 and goal.usage.llm_calls_used >= 5

    # 4. Every decision was a human's; the delegate never approved, resumed or published.
    for run_id in (uuid.UUID(plan_run), greet.run_id, docs.run_id):
        for decision in await world.repository.list_decisions(run_id):
            assert decision.decided_by in (OMAR.subject, PAULA.subject)
        for event in await world.repository.list_events(run_id, after_id=None, limit=200):
            assert event.actor_subject != DELEGATION_ISSUER
    # The mirror audit shows the platform refreshing for integration evidence.
    assert any(r.operation == "refresh" and r.result == "ok" for r in world.syncs.rows)


def child_digest(view) -> str | None:  # type: ignore[no-untyped-def]
    return view.pending.scope_digest if view.pending is not None else None

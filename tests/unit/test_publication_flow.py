"""Fase 7, Paso 3 with fakes: a run on a mirrored GitHub repository ends ``completed``
unless a second human approves publication of the exact plan; then the platform
pushes exactly one branch under ``agentforge/`` and opens exactly one draft pull
request, idempotently, with no credential anywhere agents or audit can see.

GitHub's REST API is simulated (``FakeGitHub``); its Git endpoint is a local
repository reached over ``file://``. No network, no model, no Runner."""

import json
import logging
import uuid
from pathlib import Path

import pytest

from app.agents.development import DeveloperChanges, FileEdit
from app.contracts.lifecycle import DecisionStage, RunEventKind, RunLifecycleStatus, WorkflowKind
from app.infrastructure.db import InMemoryPublicationRepository
from app.infrastructure.git.publish import GitPublisher
from app.infrastructure.github import GitHubPublisher, InstallationTokenProvider
from app.orchestration.exceptions import (
    DecisionStaleError,
    RunAlreadyDecidedError,
    RunNotResumableError,
)
from app.orchestration.graph import build_graph
from app.orchestration.publication import PublicationRuntime
from app.orchestration.recovery import RecoveryService
from app.orchestration.runs import RunService
from app.orchestration.scoped_development import build_scoped_development_graph
from app.security import AuthorizationPolicy, PermissionDeniedError, Principal
from tests.unit.git_fixtures import git
from tests.unit.test_mirror_service import PROJECT
from tests.unit.test_scoped_development_mirrors import OPERATOR, MirrorWorld

pytestmark = pytest.mark.anyio
S = RunLifecycleStatus


def human(subject: str, roles: set[str]) -> Principal:
    return Principal(
        subject=subject,
        kind="user",
        issuer="https://issuer.test",
        roles=frozenset(roles),
        projects=frozenset({PROJECT}),
    )


OMAR = human("omar", {"project_operator"})  # approves the scope
PAULA = human("paula", {"project_operator"})  # approves the publication
ALICE = human("alice", {"approver"})  # runs:approve without repos:publish
VICTOR = human("victor", {"viewer"})


class PublishWorld(MirrorWorld):
    def __init__(
        self,
        tmp_path: Path,
        *,
        write_enabled: bool = True,
        installation_writes: bool = True,
        allowed: frozenset[str] = frozenset(),
        notifications: object | None = None,
        outbox: object | None = None,
    ) -> None:
        super().__init__(tmp_path, notifications=notifications, outbox=outbox)
        fake = self.lab.github
        if installation_writes:
            fake.app_permissions = {
                "contents": "write",
                "pull_requests": "write",
                "metadata": "read",
            }
        fake.branch_resolver = lambda name: self.remote_heads().get(name)
        self.lab.tokens = InstallationTokenProvider(
            self.lab.client, self.lab.credentials, write_enabled=write_enabled, clock=self.lab.clock
        )
        self.lab.service = self.lab.make_service()
        self.publications = InMemoryPublicationRepository()
        self.runtime.mirrors = self.lab.service
        self.runtime.publisher = (
            PublicationRuntime(
                git=GitPublisher(self.lab.adapter, allow_file_remotes=True),
                github=GitHubPublisher(self.lab.client, self.lab.tokens),
                tokens=self.lab.tokens,
                audit=self.publications,
                runs=self.repository,
                allowed_repositories=allowed,
                askpass_dir=self.lab.askpass,
                clock=lambda: self.now,
            )
            if write_enabled
            else None
        )
        self.graph = build_scoped_development_graph(self.runtime)

    def service(self, *, separation_of_duties: bool = False) -> RunService:  # type: ignore[override]
        return RunService(
            build_graph(self.model),
            self.repository,
            authorization=AuthorizationPolicy(separation_of_duties=separation_of_duties),
            graphs={
                WorkflowKind.PLANNING: build_graph(self.model),
                WorkflowKind.DEVELOPMENT: self.graph,
            },
            policies=self.lab.policies,
            clock=lambda: self.now,
        )

    def remote_heads(self) -> dict[str, str]:
        out = git(
            "for-each-ref",
            "--format=%(refname:short) %(objectname)",
            "refs/heads/",
            cwd=self.lab.remote,
            home=self.lab.home,
        )
        return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())

    async def up_to_publish(self, service: RunService) -> tuple[str, str]:
        """Create, approve the scope, let Developer/QA run: returns (run_id, publish_digest)."""
        run_id = await self.start(service)
        pending = (await service.decisions(run_id, OPERATOR)).pending
        assert pending is not None and pending.stage is DecisionStage.SCOPE
        response = await service.approve(run_id, True, OMAR, scope_digest=pending.scope_digest)
        assert response.status is S.WAITING_APPROVAL, response
        assert response.pending_stage is DecisionStage.PUBLISH
        pending = (await service.decisions(run_id, OPERATOR)).pending
        assert pending is not None and pending.stage is DecisionStage.PUBLISH
        assert pending.publish_digest is not None
        return run_id, pending.publish_digest

    async def kinds(self, run_id: str) -> list[RunEventKind]:
        events = await self.repository.list_events(uuid.UUID(run_id), after_id=None, limit=200)
        return [e.kind for e in events]

    def agentforge_branches(self) -> dict[str, str]:
        return {k: v for k, v in self.remote_heads().items() if k.startswith("agentforge/")}


async def test_nothing_leaves_the_host_without_a_second_human_decision(tmp_path: Path):
    world = PublishWorld(tmp_path)
    service = world.service(separation_of_duties=True)
    run_id, digest = await world.up_to_publish(service)
    base = world.lab.remote_head()
    # The plan is frozen and visible; the remote and GitHub are untouched.
    pending = (await service.decisions(run_id, VICTOR)).pending
    assert pending is not None
    payload = pending.payload
    assert payload["remote_branch"] == f"agentforge/{run_id}" and payload["base_sha"] == base
    assert {f["path"] for f in payload["files"]} == {"src/app.py", "tests/test_greet.py"}
    assert payload["title"].startswith("[AgentForge] ")
    assert world.agentforge_branches() == {} and world.lab.github.pulls == {}
    row = await world.repository.get_run(uuid.UUID(run_id))
    assert row is not None and row.publication is not None
    assert row.publication["state"] == "prepared" and row.publication["publish_digest"] == digest
    assert RunEventKind.PUBLICATION_PREPARED in await world.kinds(run_id)
    assert [r.state.value for r in world.publications.rows] == ["prepared"]
    # Who may not publish: viewers, approvers without repos:publish, the creator,
    # the scope approver (four eyes), anyone with a stale digest.
    with pytest.raises(PermissionDeniedError):
        await service.approve(run_id, True, VICTOR, publish_digest=digest)
    with pytest.raises(PermissionDeniedError):
        await service.approve(run_id, True, ALICE, publish_digest=digest)
    with pytest.raises(PermissionDeniedError):
        await service.approve(run_id, True, OPERATOR, publish_digest=digest)  # creator
    with pytest.raises(PermissionDeniedError, match="distinto"):
        await service.approve(run_id, True, OMAR, publish_digest=digest)  # scope approver
    with pytest.raises(DecisionStaleError):
        await service.approve(run_id, True, PAULA)  # no digest
    with pytest.raises(DecisionStaleError):
        await service.approve(run_id, True, PAULA, publish_digest="0" * 64)
    with pytest.raises(DecisionStaleError):
        await service.approve(run_id, True, PAULA, scope_digest=digest)  # wrong field
    still = await service.get(run_id, OPERATOR)
    assert still.status is S.WAITING_APPROVAL and still.pending_stage is DecisionStage.PUBLISH
    assert world.agentforge_branches() == {} and world.lab.github.pulls == {}
    # Declining publication ends the run without touching GitHub; the work stays local.
    rejected = await service.approve(run_id, False, PAULA, publish_digest=digest)
    assert rejected.status is S.REJECTED and rejected.publication is not None
    assert rejected.publication["state"] == "rejected"
    assert world.agentforge_branches() == {} and world.lab.github.pulls == {}
    assert RunEventKind.RUN_REJECTED in await world.kinds(run_id)


async def test_an_approved_plan_is_pushed_once_as_one_draft_pull_request(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    world = PublishWorld(tmp_path)
    service = world.service()
    with caplog.at_level(logging.DEBUG):
        run_id, digest = await world.up_to_publish(service)
        done = await service.approve(run_id, True, PAULA, publish_digest=digest)
    assert done.status is S.PUBLISHED and done.pending_stage is None and done.error is None
    publication = done.publication
    assert publication is not None and publication["state"] == "published"
    branch = f"agentforge/{run_id}"
    heads = world.remote_heads()
    assert heads[branch] == publication["head_sha"] and heads["main"] == publication["base_sha"]
    assert list(world.agentforge_branches()) == [branch]
    # Exactly one draft pull request, in Spanish, with the required sections.
    assert len(world.lab.github.pulls) == 1
    pr = world.lab.github.pulls[1]
    assert pr["draft"] is True and pr["head"]["ref"] == branch and pr["base"]["ref"] == "main"
    assert pr["title"].startswith("[AgentForge] ")
    for section in (
        "generados mediante agentes de IA",
        "## Resumen",
        "## Archivos modificados",
        "## Pruebas ejecutadas en el sandbox",
        "## Commit base y trazabilidad",
        f"`{publication['base_sha']}`",
        "Publicación aprobada por `paula`",
        "Alcance aprobado por `omar`",
    ):
        assert section in pr["body"], section
    assert "@" not in pr["body"].replace("@​", "")
    assert publication["pr_number"] == 1 and publication["pr_url"].endswith("/pull/1")
    # The local mirror keeps the run branch at the published commit; main is untouched.
    mirror_heads = world.lab.adapter.branch_heads(world.lab.mirror_path)
    assert mirror_heads[f"run/{run_id}"] == publication["head_sha"]
    assert mirror_heads["main"] == publication["base_sha"]
    # Audit: decisions, events and publication steps name humans and the publisher.
    decisions = (await service.decisions(run_id, OPERATOR)).decisions
    assert [(d.stage, d.approved, d.decided_by) for d in decisions] == [
        (DecisionStage.SCOPE, True, "omar"),
        (DecisionStage.PUBLISH, True, "paula"),
    ]
    assert decisions[1].scope_digest == digest and decisions[1].applied_at is not None
    kinds = await world.kinds(run_id)
    for kind in (
        RunEventKind.PUBLICATION_PREPARED,
        RunEventKind.BRANCH_PUSHED,
        RunEventKind.PULL_REQUEST_OPENED,
        RunEventKind.RUN_PUBLISHED,
    ):
        assert kind in kinds, kind
    assert kinds.index(RunEventKind.BRANCH_PUSHED) < kinds.index(RunEventKind.PULL_REQUEST_OPENED)
    events = await world.repository.list_events(uuid.UUID(run_id), after_id=None, limit=200)
    pushed = next(e for e in events if e.kind is RunEventKind.BRANCH_PUSHED)
    assert pushed.payload["executed_by"] == "agentforge:publisher"
    assert pushed.payload["approved_by"] == "paula" and pushed.actor_subject is None
    assert [r.state.value for r in world.publications.rows] == [
        "prepared",
        "approved",
        "pushed",
        "opened",
        "published",
    ]
    assert world.publications.rows[-1].pr_number == 1
    assert all(r.actor_subject == "agentforge:publisher" for r in world.publications.rows)
    # Tokens: one push token (contents:write) and one PR token (pull_requests:write),
    # both narrowed to the repository and revoked at GitHub afterwards.
    minted = {t: info for t, info in world.lab.github.tokens.items()}
    write_tokens = [t for t, i in minted.items() if "write" in i["permissions"].values()]
    assert write_tokens and set(write_tokens) <= set(world.lab.github.revoked_by_api)
    for info in minted.values():
        assert info["repositories"] == ["agentforge-sandbox-demo"]
        assert not ({"contents", "pull_requests"} <= set(info["permissions"]))
    # No credential anywhere: checkpoint, events, audit, run row, PR body, disk, logs.
    snapshot = await world.graph.aget_state({"configurable": {"thread_id": run_id}})
    haystacks = [
        json.dumps(snapshot.values, default=str),
        json.dumps([e.payload for e in events], default=str),
        json.dumps([r.model_dump(mode="json") for r in world.publications.rows]),
        json.dumps(publication),
        pr["body"],
        caplog.text,
    ]
    for secret in minted:
        for haystack in haystacks:
            assert secret not in haystack
        assert world.lab.files_containing(secret) == []
    assert "x-access-token" not in caplog.text
    assert list(world.lab.askpass.iterdir()) == []
    # Replaying the same decision is idempotent; a contradictory one is refused.
    again = await service.approve(run_id, True, PAULA, publish_digest=digest)
    assert again.status is S.PUBLISHED and len(world.lab.github.pulls) == 1
    with pytest.raises(RunAlreadyDecidedError):
        await service.approve(run_id, False, PAULA, publish_digest=digest)
    assert world.remote_heads()[branch] == publication["head_sha"]


async def test_a_plan_changed_after_preparation_is_refused_before_any_remote_operation(
    tmp_path: Path,
):
    world = PublishWorld(tmp_path)
    service = world.service()
    run_id, digest = await world.up_to_publish(service)
    # Someone amends the run branch after the human saw the plan.
    worktree = world.manager.get(uuid.UUID(run_id), PROJECT)
    assert worktree is not None
    (worktree.path / "src" / "app.py").write_text("def greet(n):\n    return 'pwned'\n")
    git("commit", "-q", "--amend", "--no-edit", "-a", cwd=worktree.path, home=world.lab.home)
    failed = await service.approve(run_id, True, PAULA, publish_digest=digest)
    assert failed.status is S.FAILED and failed.error is not None
    assert failed.error.code == "publish_digest_stale"
    assert world.agentforge_branches() == {} and world.lab.github.pulls == {}
    assert failed.publication is not None and failed.publication["state"] == "failed"
    assert failed.publication["retryable"] is False
    with pytest.raises(RunNotResumableError):
        await service.resume(run_id, PAULA)
    # The base branch moving after preparation also invalidates the plan.
    world2 = PublishWorld(tmp_path / "two")
    service2 = world2.service()
    run2, digest2 = await world2.up_to_publish(service2)
    world2.lab.commit_remote("src/other.py", "x = 1\n")  # main advanced at GitHub
    moved = await service2.approve(run2, True, PAULA, publish_digest=digest2)
    assert moved.status is S.FAILED and moved.error is not None
    assert moved.error.code == "base_moved"
    assert world2.agentforge_branches() == {} and world2.lab.github.pulls == {}


async def test_a_crash_between_push_and_pull_request_resumes_without_duplicates(tmp_path: Path):
    world = PublishWorld(tmp_path)
    service = world.service()
    run_id, digest = await world.up_to_publish(service)
    world.lab.github.pulls_fail_next = [502, 503]  # GitHub down right after the push
    interrupted = await service.approve(run_id, True, PAULA, publish_digest=digest)
    assert interrupted.status is S.INTERRUPTED and interrupted.error is None
    publication = interrupted.publication
    assert publication is not None
    assert publication["state"] == "failed" and publication["retryable"] is True
    assert publication["resume_from"] == "pushed"
    branch = f"agentforge/{run_id}"
    assert world.remote_heads()[branch] == publication["head_sha"]  # the push happened once
    assert world.lab.github.pulls == {}
    kinds = await world.kinds(run_id)
    assert RunEventKind.BRANCH_PUSHED in kinds and RunEventKind.PUBLICATION_FAILED in kinds
    # Only a human with repos:publish may retry, and the retry re-enters where it stopped.
    with pytest.raises(PermissionDeniedError):
        await service.resume(run_id, ALICE)
    resumed = await service.resume(run_id, PAULA)
    assert resumed.status is S.PUBLISHED and resumed.publication is not None
    assert resumed.publication["pr_number"] == 1 and resumed.publication["attempts"] == 2
    assert len(world.lab.github.pulls) == 1
    assert world.remote_heads()[branch] == publication["head_sha"]  # not pushed again
    pushes = [
        e
        for e in await world.repository.list_events(uuid.UUID(run_id), after_id=None, limit=200)
        if e.kind is RunEventKind.BRANCH_PUSHED
    ]
    assert len(pushes) == 1 and RunEventKind.RUN_RESUMED in await world.kinds(run_id)
    states = [r.state.value for r in world.publications.rows]
    assert states == ["prepared", "approved", "pushed", "failed", "pushed", "opened", "published"]


async def test_the_recovery_sweep_also_resumes_an_interrupted_publication(tmp_path: Path):
    world = PublishWorld(tmp_path)
    service = world.service()
    run_id, digest = await world.up_to_publish(service)
    world.lab.github.pulls_fail_next = [502, 503]
    assert (await service.approve(run_id, True, PAULA, publish_digest=digest)).status is (
        S.INTERRUPTED
    )
    report = await RecoveryService(service).sweep()
    assert report.outcomes.get("resumed") == 1
    final = await service.get(run_id, OPERATOR)
    assert final.status is S.PUBLISHED and len(world.lab.github.pulls) == 1
    # Nothing left for a second sweep.
    assert (await RecoveryService(service).sweep()).claimed == 0


async def test_a_lost_response_after_pull_request_creation_is_recovered(tmp_path: Path):
    world = PublishWorld(tmp_path)
    service = world.service()
    run_id, digest = await world.up_to_publish(service)
    world.lab.github.lose_create_response = 1  # GitHub creates the PR; the answer never arrives
    done = await service.approve(run_id, True, PAULA, publish_digest=digest)
    assert done.status is S.PUBLISHED and done.publication is not None
    assert done.publication["pr_number"] == 1 and len(world.lab.github.pulls) == 1
    posts = [
        r for r in world.lab.github.requests if r.method == "POST" and r.url.path.endswith("/pulls")
    ]
    assert len(posts) == 2  # the lost one and the retry that GitHub refused as duplicate
    assert list(world.agentforge_branches()) == [f"agentforge/{run_id}"]


def _developer_writes(world: PublishWorld, content: str) -> None:
    world.model.register(
        DeveloperChanges,
        DeveloperChanges(
            summary="hecho",
            edits=[
                FileEdit(path="src/app.py", content=content),
                FileEdit(path="tests/test_greet.py", content="def test_ok():\n    assert 1\n"),
            ],
        ),
    )


async def test_secrets_in_the_diff_block_publication_before_any_plan(tmp_path: Path):
    # Layer 1 (Fase 6): the write tool itself refuses well-known token formats, so the
    # secret never even reaches the worktree.
    world = PublishWorld(tmp_path / "write-tool")
    _developer_writes(world, "def greet(n):\n    return n\n\nTOKEN = 'ghs_" + "A" * 36 + "'\n")
    service = world.service()
    run_id = await world.start(service)
    pending = (await service.decisions(run_id, OPERATOR)).pending
    assert pending is not None
    denied = await service.approve(run_id, True, OMAR, scope_digest=pending.scope_digest)
    assert denied.status is S.FAILED and denied.error is not None
    assert denied.error.code == "content_denied"
    assert world.agentforge_branches() == {} and world.lab.github.pulls == {}
    # Layer 2 (ADR 0011): the pre-publication scan catches what the write tool lets
    # through (here: credentials embedded in a URL) and no plan is ever prepared.
    world2 = PublishWorld(tmp_path / "scan")
    _developer_writes(
        world2,
        "def greet(n):\n    return n\n\nDATABASE = 'postgresql://svc:hunter2hunter2@db.internal/app'\n",
    )
    service2 = world2.service()
    run2 = await world2.start(service2)
    pending2 = (await service2.decisions(run2, OPERATOR)).pending
    assert pending2 is not None
    failed = await service2.approve(run2, True, OMAR, scope_digest=pending2.scope_digest)
    assert failed.status is S.FAILED and failed.error is not None
    assert failed.error.code == "publication_scan_failed"
    assert "secret_detected" in failed.error.message
    assert world2.agentforge_branches() == {} and world2.lab.github.pulls == {}
    assert failed.publication is not None and failed.publication["state"] == "failed"
    assert failed.publication["retryable"] is False
    assert (await service2.decisions(run2, OPERATOR)).pending is None
    kinds = await world2.kinds(run2)
    assert RunEventKind.PUBLICATION_FAILED in kinds
    assert RunEventKind.PUBLICATION_PREPARED not in kinds
    assert all(  # no write-scoped token was ever requested
        "write" not in info["permissions"].values() for info in world2.lab.github.tokens.values()
    )


async def test_without_the_write_flag_an_allowlist_or_the_grant_nothing_is_published(
    tmp_path: Path,
):
    # 1. GITHUB_WRITE_ENABLED=false: the Publisher does not exist; the run ends completed.
    world = PublishWorld(tmp_path / "off", write_enabled=False)
    service = world.service()
    run_id = await world.start(service)
    pending = (await service.decisions(run_id, OPERATOR)).pending
    assert pending is not None
    done = await service.approve(run_id, True, OMAR, scope_digest=pending.scope_digest)
    assert done.status is S.COMPLETED and done.pending_stage is None
    assert world.agentforge_branches() == {} and world.lab.github.pulls == {}
    assert all("write" not in i["permissions"].values() for i in world.lab.github.tokens.values())
    # 2. Repository outside PUBLISH_ALLOWED_REPOSITORIES: completed, publication unavailable.
    world2 = PublishWorld(tmp_path / "allow", allowed=frozenset({"other-repo"}))
    service2 = world2.service()
    run2 = await world2.start(service2)
    pending2 = (await service2.decisions(run2, OPERATOR)).pending
    assert pending2 is not None
    done2 = await service2.approve(run2, True, OMAR, scope_digest=pending2.scope_digest)
    assert done2.status is S.COMPLETED and done2.publication is not None
    assert done2.publication["state"] == "unavailable"
    assert world2.agentforge_branches() == {}
    # 3. Installation without write grants: GitHub refuses the write token; nothing pushed.
    world3 = PublishWorld(tmp_path / "grant", installation_writes=False)
    service3 = world3.service()
    run3, digest3 = await world3.up_to_publish(service3)
    failed = await service3.approve(run3, True, PAULA, publish_digest=digest3)
    assert failed.status is S.FAILED and failed.error is not None
    assert failed.error.code == "token_request_rejected"
    assert world3.agentforge_branches() == {} and world3.lab.github.pulls == {}
    assert failed.publication is not None and failed.publication["retryable"] is False


async def test_a_full_run_mails_both_approvals_and_the_pull_request_without_event_collisions(
    tmp_path: Path,
):
    """The daily flow end to end with the default ``priority`` level: scope pending,
    publish pending, run completed, pull request opened and run published reach the
    outbox; every (kind, ordinal) pair of run_events is unique, so the second human
    decision never collides with the first (the 2026-10-27 incident)."""
    from datetime import UTC, datetime

    from app.infrastructure.db import InMemoryNotificationOutbox
    from app.infrastructure.notifications import FakeEmailSender
    from app.notifications import (
        NotificationEnqueuer,
        NotificationKind,
        NotificationPolicy,
        NotificationWorker,
    )

    clock = lambda: datetime(2026, 10, 27, 9, 0, tzinfo=UTC)  # noqa: E731
    outbox = InMemoryNotificationOutbox(clock)
    enqueuer = NotificationEnqueuer(
        NotificationPolicy(base_url="http://127.0.0.1:8011"),
        recipients=["martin@example.com"],
    )
    assert enqueuer.level == "priority"
    world = PublishWorld(tmp_path, notifications=enqueuer, outbox=outbox)
    service = world.service()
    run_id, digest = await world.up_to_publish(service)
    done = await service.approve(run_id, True, PAULA, publish_digest=digest)
    assert done.status is S.PUBLISHED and done.publication is not None
    events = await world.repository.list_events(uuid.UUID(run_id), after_id=None, limit=300)
    pairs = [(e.kind, e.ordinal) for e in events]
    assert len(pairs) == len(set(pairs)), "run_events natural key must stay unique"
    by_kind = {k: sorted(o for kk, o in pairs if kk == k) for k, _ in pairs}
    assert by_kind[RunEventKind.WAITING_APPROVAL] == [1, 2]
    assert by_kind[RunEventKind.APPROVAL_RECORDED] == [1, 2]
    assert by_kind[RunEventKind.APPROVAL_APPLIED] == [1, 2]
    # The scope decision is audited by approval_recorded/applied; the graph itself
    # emits run_approved only for the publish node.
    assert by_kind[RunEventKind.RUN_APPROVED] == [2]
    mails = [i.kind for i in sorted(await outbox.list_items(limit=100), key=lambda i: i.id)]
    assert mails == [
        NotificationKind.SCOPE_PENDING,
        NotificationKind.RUN_COMPLETED,
        NotificationKind.PUBLISH_PENDING,
        NotificationKind.PULL_REQUEST_OPENED,
        NotificationKind.RUN_PUBLISHED,
    ]
    sender = FakeEmailSender()
    report = await NotificationWorker(outbox, sender, clock=clock).run_once()
    assert report.sent == 5
    opened = next(m for m in sender.sent if "Pull Request" in m.subject)
    assert done.publication["pr_url"] in opened.text and "ghs_" not in opened.text
    assert all(m.references == sender.sent[0].references for m in sender.sent)  # one thread

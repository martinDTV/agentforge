"""Publication state machine and its compare-and-set persistence (ADR 0011 §7)."""

import uuid
from datetime import UTC, datetime

import pytest

from app.contracts.lifecycle import WorkflowKind
from app.infrastructure.db import InMemoryPublicationRepository, InMemoryRunRepository
from app.publishing import (
    PublicationError,
    PublicationProgress,
    PublicationRecord,
    PublicationState,
    advance,
)
from app.publishing.state import ALLOWED, IN_PROGRESS

pytestmark = pytest.mark.anyio
S = PublicationState


def test_transitions_follow_the_documented_machine_and_failed_resumes_where_it_stopped():
    progress = PublicationProgress(state=S.PREPARED, publish_digest="d" * 64)
    approved = advance(progress, S.APPROVED, approved_by="omar")
    pushed = advance(approved, S.PUSHED, head_sha="b" * 40, remote_branch="agentforge/x")
    failed = advance(pushed, S.FAILED, code="github_unreachable", retryable=True)
    assert failed.resume_from is S.PUSHED and failed.retryable and failed.code
    resumed = advance(failed, S.PUSHED)
    assert resumed.code is None and resumed.resume_from is None and not resumed.retryable
    opened = advance(resumed, S.OPENED, pr_number=7, pr_url="https://github.com/x/y/pull/7")
    published = advance(opened, S.PUBLISHED)
    assert published.terminal and published.pr_number == 7
    for illegal in (S.PREPARED, S.APPROVED, S.PUSHED, S.FAILED):
        with pytest.raises(PublicationError) as exc:
            advance(published, illegal)
        assert exc.value.code == "publication_state_conflict"
    with pytest.raises(PublicationError):
        advance(progress, S.PUSHED)  # prepared cannot skip the decision
    with pytest.raises(PublicationError):
        advance(approved, S.OPENED)  # approved cannot skip the push
    assert ALLOWED[S.PUBLISHED] == frozenset() and ALLOWED[S.REJECTED] == frozenset()
    assert IN_PROGRESS == {S.APPROVED, S.PUSHED, S.OPENED}
    # A failure before any remote step resumes from nothing durable.
    early = advance(advance(progress, S.APPROVED), S.FAILED, code="x", retryable=True)
    assert early.resume_from is S.APPROVED
    assert PublicationProgress.from_json(None) is None
    assert PublicationProgress.from_json(published.as_json()) == published


async def test_set_publication_is_a_compare_and_set_on_the_state():
    repo = InMemoryRunRepository()
    run_id = uuid.uuid4()
    await repo.create_run(
        run_id,
        "t",
        owner="p",
        ttl_seconds=60,
        owner_subject="olivia",
        project_id="sandbox-github",
        workflow_kind=WorkflowKind.DEVELOPMENT,
    )
    prepared = PublicationProgress(state=S.PREPARED, publish_digest="d" * 64).as_json()
    assert await repo.set_publication(run_id, prepared, expect_absent=True) is not None
    assert await repo.set_publication(run_id, prepared, expect_absent=True) is None  # not absent
    approved = {**prepared, "state": "approved"}
    assert await repo.set_publication(run_id, approved, expected_state="pushed") is None
    row = await repo.set_publication(run_id, approved, expected_state="prepared")
    assert row is not None and row.publication == approved
    # Two publishers racing on the same step: exactly one wins.
    pushed = {**approved, "state": "pushed"}
    first = await repo.set_publication(run_id, pushed, expected_state="approved")
    second = await repo.set_publication(run_id, pushed, expected_state="approved")
    assert first is not None and second is None
    assert await repo.set_publication(uuid.uuid4(), pushed) is None


async def test_the_audit_repository_keeps_every_step_in_order_without_credentials():
    audit = InMemoryPublicationRepository()
    run_id = uuid.uuid4()
    now = datetime(2026, 10, 27, tzinfo=UTC)
    for state in (S.PREPARED, S.APPROVED, S.PUSHED):
        await audit.record(
            PublicationRecord(
                run_id=run_id,
                repository_id="sandbox-github",
                project_id="sandbox-github",
                state=state,
                remote_branch="agentforge/x",
                head_sha="b" * 40,
                recorded_at=now,
            )
        )
    rows = await audit.list_for_run(run_id)
    assert [r.state for r in rows] == [S.PREPARED, S.APPROVED, S.PUSHED]
    assert all(r.actor_subject == "agentforge:publisher" for r in rows)
    assert "token" not in " ".join(sorted(PublicationRecord.model_fields)).replace(
        "token_expires_at", ""
    )
    assert await audit.list_for_run(uuid.uuid4()) == []

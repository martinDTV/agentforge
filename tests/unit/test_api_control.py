"""Control API: scoped listing, paginated audit, manual resume, synchronous sweeps."""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.contracts.lifecycle import RunLifecycleStatus
from app.contracts.planning import DevelopmentPlan
from app.infrastructure.db import InMemoryRunRepository
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.main import create_app
from app.orchestration.graph import build_state_graph
from app.orchestration.limits import RunLimits
from app.orchestration.policy import RecoveryPolicy
from app.orchestration.ports import RunProjection
from app.security import AuthorizationPolicy, JwtVerifier, VerificationRules
from app.security.dev_keys import DevIssuer
from app.security.tokens import StaticKeys

RUNS = "/api/v1/runs"
SWEEPS = "/api/v1/admin/recovery/sweeps"
TASK = {"task": "Crear módulo de pacientes con pruebas"}
S = RunLifecycleStatus


@pytest.fixture
def issuer() -> DevIssuer:
    return DevIssuer()


@pytest.fixture
def repository() -> InMemoryRunRepository:
    return InMemoryRunRepository()


@pytest.fixture
def app(fake_llm: FakeLLMAdapter, issuer: DevIssuer, repository: InMemoryRunRepository) -> Any:
    verifier = JwtVerifier(
        StaticKeys(issuer.jwks), VerificationRules(issuer=issuer.issuer, audience=issuer.audience)
    )
    return create_app(
        fake_llm,
        RunLimits(),
        repository=repository,
        token_verifier=verifier,
        authorization=AuthorizationPolicy(separation_of_duties=False),
        policy=RecoveryPolicy(lease_ttl_seconds=5, heartbeat_seconds=1, max_attempts=3),
    )


@pytest.fixture
def client(app: Any) -> Any:
    with TestClient(app) as client:
        yield client


def bearer(
    issuer: DevIssuer, subject: str, roles: list[str], projects: list[str]
) -> dict[str, str]:
    return {"Authorization": f"Bearer {issuer.token(subject, roles=roles, projects=projects)}"}


def create(client: TestClient, headers: dict[str, str], **body: object) -> dict[str, Any]:
    response = client.post(RUNS, json={**TASK, **body}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def abandoned_run(
    app: Any, repository: InMemoryRunRepository, llm: FakeLLMAdapter, *, owner: str, project: str
) -> str:
    """Row owned by a dead process whose checkpoint stopped after the planner."""
    rid = uuid.uuid4()

    async def _make() -> None:
        await repository.create_run(
            rid,
            TASK["task"],
            owner="dead-process",
            ttl_seconds=60,
            owner_subject=owner,
            project_id=project,
        )
        saver = app.state.run_service.graph.checkpointer
        halted = build_state_graph(llm).compile(checkpointer=saver, interrupt_after=["planner"])
        await halted.ainvoke(TASK, {"configurable": {"thread_id": str(rid)}})

    asyncio.run(_make())
    repository.expire_lease(rid)
    return str(rid)


def interrupted_run(
    app: Any,
    repository: InMemoryRunRepository,
    llm: FakeLLMAdapter,
    *,
    owner: str,
    project: str,
    attempt: int = 1,
) -> str:
    run_id = abandoned_run(app, repository, llm, owner=owner, project=project)
    rid = uuid.UUID(run_id)
    asyncio.run(
        repository.project(
            rid,
            RunProjection(status=S.INTERRUPTED, plan={"summary": "p", "tasks": []}),
            release=True,
        )
    )
    if attempt != 1:
        repository.runs[rid] = repository.runs[rid].model_copy(update={"attempt": attempt})
    return run_id


# -- listing ------------------------------------------------------------------


def test_listing_is_scoped_by_project_and_ownership(client: TestClient, issuer: DevIssuer):
    alice = bearer(issuer, "alice", ["operator"], ["alpha"])
    bob = bearer(issuer, "bob", ["operator"], ["alpha"])
    carol = bearer(issuer, "carol", ["operator"], ["beta"])
    a1, a2 = create(client, alice)["run_id"], create(client, alice)["run_id"]
    b1 = create(client, bob)["run_id"]
    c1 = create(client, carol)["run_id"]

    def ids(headers: dict[str, str], **params: object) -> list[str]:
        response = client.get(RUNS, headers=headers, params=params)
        assert response.status_code == 200, response.text
        return [item["run_id"] for item in response.json()["items"]]

    assert set(ids(bearer(issuer, "vic", ["viewer"], ["alpha"]))) == {a1, a2, b1}
    assert ids(bearer(issuer, "alice", [], ["alpha"])) == [a2, a1]  # owner-only, newest first
    assert set(ids(bearer(issuer, "root", ["platform_admin"], []))) == {a1, a2, b1, c1}
    assert ids(carol) == [c1]
    assert ids(bearer(issuer, "nomad", ["operator"], [])) == []
    assert ids(alice, project_id="beta") == []  # a filter never widens the scope
    assert set(ids(bob, mine="true")) == {b1}
    assert set(ids(bob, owner="alice")) == {a1, a2}
    assert ids(alice, status="failed") == []
    item = client.get(RUNS, headers=alice).json()["items"][0]
    assert set(item) >= {"run_id", "status", "owner", "project_id", "attempt", "created_at"}
    assert "plan" not in item


def test_listing_pagination_is_stable_and_cursors_are_bound_to_filters(
    client: TestClient, issuer: DevIssuer
):
    alice = bearer(issuer, "alice", ["operator"], ["alpha"])
    created = [create(client, alice)["run_id"] for _ in range(7)]
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, object] = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = client.get(RUNS, headers=alice, params=params).json()
        seen.extend(item["run_id"] for item in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 3 and seen == list(reversed(created))  # newest first, no gaps or dupes
    first = client.get(RUNS, headers=alice, params={"limit": 3}).json()["next_cursor"]
    reused = client.get(
        RUNS, headers=alice, params={"limit": 3, "cursor": first, "status": "failed"}
    )
    assert reused.status_code == 400 and reused.json()["code"] == "invalid_cursor"
    garbage = client.get(RUNS, headers=alice, params={"cursor": "zzz"})
    assert garbage.status_code == 400
    other_user = client.get(
        RUNS, headers=bearer(issuer, "bob", ["viewer"], ["alpha"]), params={"cursor": first}
    )
    assert other_user.status_code == 400  # bound to the subject too
    assert client.get(RUNS, headers=alice, params={"limit": 0}).status_code == 422
    assert client.get(RUNS, headers=alice, params={"limit": 101}).status_code == 422


# -- events -------------------------------------------------------------------


def test_events_need_visibility_then_audit_permission_and_paginate(
    client: TestClient, issuer: DevIssuer
):
    alice = bearer(issuer, "alice", ["operator", "approver"], ["alpha"])
    run_id = create(client, alice)["run_id"]
    client.post(f"{RUNS}/{run_id}/approve", json={"approved": True}, headers=alice)

    assert (
        client.get(
            f"{RUNS}/{run_id}/events", headers=bearer(issuer, "bob", ["viewer"], ["beta"])
        ).status_code
        == 404
    )
    owner_without_roles = client.get(
        f"{RUNS}/{run_id}/events", headers=bearer(issuer, "alice", [], ["alpha"])
    )
    assert (
        owner_without_roles.status_code == 403
        and owner_without_roles.json()["code"] == "permission_denied"
    )

    viewer = bearer(issuer, "vic", ["viewer"], ["alpha"])
    first = client.get(f"{RUNS}/{run_id}/events", headers=viewer, params={"limit": 4}).json()
    assert [e["kind"] for e in first["items"]] == [
        "run_created",
        "planner_completed",
        "architect_reviewed",
        "waiting_approval",
    ]
    assert first["items"][0]["actor"] == "alice" and first["items"][1]["actor"] is None
    assert first["next_cursor"]
    second = client.get(
        f"{RUNS}/{run_id}/events",
        headers=viewer,
        params={"limit": 4, "cursor": first["next_cursor"]},
    ).json()
    assert [e["kind"] for e in second["items"]] == [
        "approval_recorded",
        "run_approved",
        "approval_applied",
    ]
    assert second["next_cursor"] is None and all(e["actor"] == "alice" for e in second["items"])
    ids = [e["id"] for e in first["items"] + second["items"]]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    other_run = create(client, alice)["run_id"]
    crossed = client.get(
        f"{RUNS}/{other_run}/events", headers=viewer, params={"cursor": first["next_cursor"]}
    )
    assert crossed.status_code == 400 and crossed.json()["code"] == "invalid_cursor"


# -- manual resume ------------------------------------------------------------


def test_resume_is_authorized_then_lease_protected_and_continues_from_the_checkpoint(
    app: Any,
    client: TestClient,
    issuer: DevIssuer,
    repository: InMemoryRunRepository,
    fake_llm: FakeLLMAdapter,
):
    run_id = interrupted_run(app, repository, fake_llm, owner="alice", project="alpha")

    assert (
        client.post(
            f"{RUNS}/{run_id}/resume", headers=bearer(issuer, "bob", ["operator"], ["beta"])
        ).status_code
        == 404
    )
    forbidden = client.post(
        f"{RUNS}/{run_id}/resume", headers=bearer(issuer, "vic", ["viewer"], ["alpha"])
    )
    assert forbidden.status_code == 403 and "runs:resume" in forbidden.json()["detail"]

    waiting = create(client, bearer(issuer, "alice", ["operator"], ["alpha"]))["run_id"]
    not_interrupted = client.post(
        f"{RUNS}/{waiting}/resume", headers=bearer(issuer, "alice", ["operator"], ["alpha"])
    )
    assert (
        not_interrupted.status_code == 409 and not_interrupted.json()["code"] == "run_not_resumable"
    )

    planner_calls = sum(1 for c in fake_llm.calls if c[2] is DevelopmentPlan)
    ok = client.post(
        f"{RUNS}/{run_id}/resume", headers=bearer(issuer, "olga", ["operator"], ["alpha"])
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "waiting_approval"
    row = repository.runs[uuid.UUID(run_id)]
    assert row.attempt == 2 and row.lease_owner is None and row.architecture is not None
    assert (
        sum(1 for c in fake_llm.calls if c[2] is DevelopmentPlan) == planner_calls
    )  # planner not repeated
    resumed = [e for e in repository.events[uuid.UUID(run_id)] if e.kind.value == "run_resumed"]
    assert len(resumed) == 1 and resumed[0].actor_subject == "olga" and resumed[0].ordinal == 2
    again = client.post(
        f"{RUNS}/{run_id}/resume", headers=bearer(issuer, "olga", ["operator"], ["alpha"])
    )
    assert again.status_code == 409


def test_resume_respects_max_attempts_and_concurrent_resumes_have_one_winner(
    app: Any,
    client: TestClient,
    issuer: DevIssuer,
    repository: InMemoryRunRepository,
    fake_llm: FakeLLMAdapter,
):
    exhausted = interrupted_run(
        app, repository, fake_llm, owner="alice", project="alpha", attempt=3
    )
    headers = bearer(issuer, "olga", ["operator"], ["alpha"])
    spent = client.post(f"{RUNS}/{exhausted}/resume", headers=headers)
    assert spent.status_code == 409 and "intentos" in spent.json()["detail"]

    run_id = interrupted_run(app, repository, fake_llm, owner="alice", project="alpha")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: client.post(f"{RUNS}/{run_id}/resume", headers=headers), range(2))
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    assert repository.runs[uuid.UUID(run_id)].attempt == 2


# -- administrative sweep -----------------------------------------------------


def test_sweep_is_synchronous_permissioned_and_scoped(
    app: Any,
    client: TestClient,
    issuer: DevIssuer,
    repository: InMemoryRunRepository,
    fake_llm: FakeLLMAdapter,
):
    alpha_run = abandoned_run(app, repository, fake_llm, owner="alice", project="alpha")
    beta_run = abandoned_run(app, repository, fake_llm, owner="carol", project="beta")

    assert (
        client.post(SWEEPS, headers=bearer(issuer, "op", ["operator"], ["alpha"])).status_code
        == 403
    )
    alpha_admin = client.post(SWEEPS, headers=bearer(issuer, "adm", ["admin"], ["alpha"]))
    assert alpha_admin.status_code == 200, alpha_admin.text
    assert alpha_admin.json() == {
        "claimed": 1,
        "outcomes": {"interrupted": 1},
        "errors": 0,
        "projects": ["alpha"],
    }
    # Synchronous: the row is already updated when the response arrives.
    assert repository.runs[uuid.UUID(alpha_run)].status is S.INTERRUPTED
    assert repository.runs[uuid.UUID(beta_run)].status is S.RUNNING  # out of scope, untouched
    assert [
        e.actor_subject
        for e in repository.events[uuid.UUID(alpha_run)]
        if e.kind.value == "run_interrupted"
    ] == [None]

    foreign = client.post(
        SWEEPS, headers=bearer(issuer, "adm", ["admin"], ["alpha"]), params={"project_id": "beta"}
    )
    assert foreign.status_code == 200 and foreign.json()["claimed"] == 0

    platform = client.post(SWEEPS, headers=bearer(issuer, "root", ["platform_admin"], []))
    assert platform.json()["claimed"] == 1 and platform.json()["projects"] is None
    assert repository.runs[uuid.UUID(beta_run)].status is S.INTERRUPTED
    assert (
        client.post(SWEEPS, headers=bearer(issuer, "root", ["platform_admin"], [])).json()[
            "claimed"
        ]
        == 0
    )

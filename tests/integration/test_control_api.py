"""Control API against PostgreSQL: keyset listing, audit pages, manual resume, scoped sweeps."""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi.testclient import TestClient

from app.contracts.planning import DevelopmentPlan
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.orchestration.graph import build_state_graph
from app.security import AuthorizationPolicy
from app.security.dev_keys import DevIssuer
from tests.integration.conftest import libpq_dsn, make_app
from tests.integration.test_business_persistence import run_row
from tests.integration.test_checkpoints import query
from tests.integration.test_recovery import FAST, components, expire_lease

RUNS = "/api/v1/runs"
SWEEPS = "/api/v1/admin/recovery/sweeps"
TASK = {"task": "Crear módulo de pacientes con pruebas"}


def bearer(
    issuer: DevIssuer, subject: str, roles: list[str], projects: list[str]
) -> dict[str, str]:
    return {"Authorization": f"Bearer {issuer.token(subject, roles=roles, projects=projects)}"}


def abandoned_in_project(url: str, llm: Any, *, owner: str, project: str) -> str:
    """Row + checkpoint after the planner, lease expired, in the given project."""
    run_id = str(uuid.uuid4())

    async def _run() -> None:
        async with components(url, llm, owner="dead-process") as (service, repository):
            await repository.create_run(
                uuid.UUID(run_id),
                TASK["task"],
                owner="dead-process",
                ttl_seconds=60,
                owner_subject=owner,
                project_id=project,
            )
            halted = build_state_graph(llm).compile(
                checkpointer=service.graph.checkpointer, interrupt_after=["planner"]
            )
            await halted.ainvoke(TASK, {"configurable": {"thread_id": run_id}})

    asyncio.run(_run())
    expire_lease(url, run_id)
    return run_id


def test_keyset_listing_matches_the_database_order_and_scope(
    fake_llm: FakeLLMAdapter, fresh_checkpoint_database_url: str
):
    url = fresh_checkpoint_database_url
    issuer = DevIssuer()
    alice = bearer(issuer, "alice", ["operator"], ["alpha"])
    carol = bearer(issuer, "carol", ["operator"], ["beta"])
    with TestClient(
        make_app(fake_llm, url, issuer=issuer, authorization=AuthorizationPolicy())
    ) as client:
        alpha_ids = [client.post(RUNS, json=TASK, headers=alice).json()["run_id"] for _ in range(7)]
        beta_ids = [client.post(RUNS, json=TASK, headers=carol).json()["run_id"] for _ in range(2)]

        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, object] = {"limit": 3}
            if cursor:
                params["cursor"] = cursor
            page = client.get(RUNS, headers=alice, params=params).json()
            seen.extend(i["run_id"] for i in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        expected = [
            str(r[0])
            for r in query(
                url,
                "SELECT id FROM runs WHERE project_id = 'alpha' ORDER BY created_at DESC, id DESC",
            )
        ]
        assert seen == expected == list(reversed(alpha_ids))
        assert not set(seen) & set(beta_ids)
        root = client.get(
            RUNS, headers=bearer(issuer, "root", ["platform_admin"], []), params={"limit": 100}
        ).json()
        assert {i["run_id"] for i in root["items"]} == set(alpha_ids) | set(beta_ids)
        assert (
            client.get(RUNS, headers=carol, params={"status": "waiting_approval"}).json()["items"][
                0
            ]["project_id"]
            == "beta"
        )


def test_event_pages_are_persisted_with_actors(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    issuer = DevIssuer()
    alice = bearer(issuer, "alice", ["operator", "approver"], ["alpha"])
    with TestClient(
        make_app(
            fake_llm,
            checkpoint_database_url,
            issuer=issuer,
            authorization=AuthorizationPolicy(separation_of_duties=False),
        )
    ) as client:
        run_id = client.post(RUNS, json=TASK, headers=alice).json()["run_id"]
        client.post(f"{RUNS}/{run_id}/approve", json={"approved": True}, headers=alice)
        kinds: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, object] = {"limit": 3}
            if cursor:
                params["cursor"] = cursor
            page = client.get(f"{RUNS}/{run_id}/events", headers=alice, params=params).json()
            kinds.extend(e["kind"] for e in page["items"])
            pages += 1
            cursor = page["next_cursor"]
            if cursor is None:
                break
    assert pages == 3 and kinds == [
        "run_created",
        "planner_completed",
        "architect_reviewed",
        "waiting_approval",
        "approval_recorded",
        "run_approved",
        "approval_applied",
    ]


def test_manual_resume_continues_from_the_checkpoint_with_one_winner(
    fake_llm: FakeLLMAdapter, fresh_checkpoint_database_url: str
):
    url = fresh_checkpoint_database_url
    run_id = abandoned_in_project(url, fake_llm, owner="local-dev", project="default")
    planner_calls = sum(1 for c in fake_llm.calls if c[2] is DevelopmentPlan)
    with TestClient(make_app(fake_llm, url, policy=FAST)) as client:  # local-dev platform_admin
        swept = client.post(SWEEPS).json()
        assert swept["claimed"] == 1 and swept["outcomes"] == {"interrupted": 1}
        assert run_row(url, run_id)["status"] == "interrupted"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: client.post(f"{RUNS}/{run_id}/resume"), range(2)))
        assert sorted(r.status_code for r in results) == [200, 409]
        winner = next(r for r in results if r.status_code == 200).json()
        assert winner["status"] == "waiting_approval"
    row = run_row(url, run_id)
    assert (row["status"], row["attempt"], row["architecture"] is not None) == (
        "waiting_approval",
        2,
        True,
    )
    assert sum(1 for c in fake_llm.calls if c[2] is DevelopmentPlan) == planner_calls
    assert query(
        url,
        "SELECT actor_subject FROM run_events WHERE run_id = %s AND kind = 'run_resumed'",
        run_id,
    ) == [("local-dev",)]
    assert query(url, "SELECT lease_owner FROM runs WHERE id = %s", run_id) == [(None,)]


def test_admin_sweep_is_scoped_to_the_callers_projects(
    fake_llm: FakeLLMAdapter, fresh_checkpoint_database_url: str
):
    url = fresh_checkpoint_database_url
    issuer = DevIssuer()
    alpha_run = abandoned_in_project(url, fake_llm, owner="alice", project="alpha")
    beta_run = abandoned_in_project(url, fake_llm, owner="carol", project="beta")
    with TestClient(
        make_app(fake_llm, url, issuer=issuer, authorization=AuthorizationPolicy(), policy=FAST)
    ) as client:
        assert (
            client.post(SWEEPS, headers=bearer(issuer, "op", ["operator"], ["alpha"])).status_code
            == 403
        )
        alpha = client.post(SWEEPS, headers=bearer(issuer, "adm", ["admin"], ["alpha"]))
        assert (
            alpha.status_code == 200
            and alpha.json()["claimed"] == 1
            and alpha.json()["projects"] == ["alpha"]
        )
        assert run_row(url, alpha_run)["status"] == "interrupted"
        assert run_row(url, beta_run)["status"] == "running"
        everything = client.post(SWEEPS, headers=bearer(issuer, "root", ["platform_admin"], []))
        assert everything.json()["claimed"] == 1 and everything.json()["projects"] is None
        assert run_row(url, beta_run)["status"] == "interrupted"
    assert libpq_dsn(url).startswith("postgresql://")

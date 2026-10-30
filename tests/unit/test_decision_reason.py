"""The optional formal reason of a human decision: accepted by the API and the CLI,
recorded with the decision, visible in the decisions listing and in the goal's audit;
never required, never invented."""

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.contracts.execution import Approval
from app.contracts.goals import GoalDecomposition, GoalEventKind
from app.contracts.lifecycle import DecisionStage, RunEventKind
from scripts import agentforge as cli
from tests.unit.goal_fixtures import plan_of
from tests.unit.test_api_development import BODY, RUNS, bearer, client, issuer  # noqa: F401
from tests.unit.test_goal_coordinator import World

pytestmark = pytest.mark.anyio


def test_the_reason_is_optional_and_bounded():
    assert Approval(approved=False).reason is None
    assert Approval(approved=False, reason="  ").reason == "  "  # the service strips it
    with pytest.raises(ValueError):
        Approval(approved=False, reason="x" * 501)


def test_api_records_and_lists_the_reason(client: TestClient, issuer):  # noqa: F811
    olivia = bearer(issuer, "olivia", ["project_operator"])
    run = client.post(RUNS, json=BODY, headers=olivia).json()
    run_id = run["run_id"]
    digest = client.get(f"{RUNS}/{run_id}/decisions", headers=olivia).json()["pending"][
        "scope_digest"
    ]
    rejected = client.post(
        f"{RUNS}/{run_id}/approve",
        json={
            "approved": False,
            "stage": "scope",
            "scope_digest": digest,
            "reason": "El alcance no incluye las pruebas de aceptación",
        },
        headers=olivia,
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    decisions = client.get(f"{RUNS}/{run_id}/decisions", headers=olivia).json()["decisions"]
    assert decisions[0]["stage"] == "scope" and decisions[0]["approved"] is False
    assert decisions[0]["reason"] == "El alcance no incluye las pruebas de aceptación"
    events = client.get(f"{RUNS}/{run_id}/events", headers=olivia).json()["items"]
    recorded = next(e for e in events if e["kind"] == "approval_recorded")
    assert recorded["payload"]["reason"] == "El alcance no incluye las pruebas de aceptación"
    too_long = client.post(
        f"{RUNS}/{run_id}/approve",
        json={"approved": False, "stage": "scope", "reason": "x" * 501},
        headers=olivia,
    )
    assert too_long.status_code == 422


def test_the_cli_sends_the_reason_only_when_given(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    run_id = str(uuid.uuid4())
    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path.endswith("/decisions"):
            return httpx.Response(
                200,
                json={
                    "run_id": run_id,
                    "status": "waiting_approval",
                    "decisions": [],
                    "pending": {
                        "stage": "plan",
                        "scope_digest": None,
                        "publish_digest": None,
                        "deadline": None,
                        "proposal": {"plan": {"summary": "s", "tasks": []}},
                    },
                },
            )
        return httpx.Response(200, json={"run_id": run_id, "status": "rejected"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(base_url="http://api.test/api/v1", transport=transport) as http:
        assert (
            cli.run(
                [
                    "--project",
                    "p",
                    "reject",
                    run_id,
                    "--yes",
                    "--reason",
                    "No encaja con el README",
                ],
                client=http,
            )
            == 0
        )
        assert calls[-1][2] == {
            "approved": False,
            "stage": "plan",
            "reason": "No encaja con el README",
        }
        assert cli.run(["--project", "p", "reject", run_id, "--yes"], client=http) == 0
        assert calls[-1][2] == {"approved": False, "stage": "plan"}


async def test_a_refused_plan_carries_the_human_reason_into_the_goal_audit():
    world = World()
    goal_id = await world.create(decomposition=GoalDecomposition.PLAN)
    plan_run = await world.plan_run(goal_id)
    # The human rejects through the real decision path of the fake launcher's repository.
    row = await world.runs.get_run(plan_run)
    assert row is not None
    claim = await world.runs.claim_decision(
        plan_run,
        DecisionStage.PLAN,
        False,
        owner="test",
        ttl_seconds=60,
        decided_by="martin",
        reason="Contradice tests/test_acceptance.py",
    )
    assert claim is not None and claim.claimed and claim.lease is not None
    await world.launcher.refuse_plan_after_claim(plan_run, claim.lease)
    await world.tick(goal_id)
    goal = await world.goal(goal_id)
    assert goal.status.value == "failed" and goal.error is not None
    events = {e.kind: e for e in await world.goals.list_events(goal_id)}
    assert (
        events[GoalEventKind.PLAN_REFUSED].payload["reason"]
        == "Contradice tests/test_acceptance.py"
    )
    assert events[GoalEventKind.PLAN_REFUSED].payload["decided_by"] == "martin"
    failed = events[GoalEventKind.GOAL_FAILED]
    assert failed.payload["reason"] == "Contradice tests/test_acceptance.py"
    assert failed.payload["decided_by"] == "martin"
    assert failed.context == {} or "tasks" in failed.context  # context is ephemeral
    run_events = await world.runs.list_events(plan_run)
    assert any(
        e.kind is RunEventKind.APPROVAL_RECORDED and e.payload.get("reason") for e in run_events
    )
    _ = plan_of  # the fixture module is imported for its helpers

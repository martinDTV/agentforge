"""The daily-use client builds the API calls a human would make, without a network."""

import json

import httpx
import pytest

from scripts import agentforge as cli

RUN = "de8845f3-bd98-48cc-bdd4-1fe67d55ea75"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append(
            (
                request.method,
                request.url.path + ("?" + request.url.query.decode() if request.url.query else ""),
                body,
            )
        )
        if request.method == "POST" and request.url.path == "/api/v1/runs":
            return httpx.Response(
                201,
                json={
                    "run_id": RUN,
                    "status": "waiting_approval",
                    "pending_stage": "scope",
                    "route": "fast",
                    "route_mode": body.get("route_mode", "auto"),
                },
            )
        if request.url.path.endswith("/decisions"):
            return httpx.Response(
                200,
                json={
                    "run_id": RUN,
                    "status": "waiting_approval",
                    "decisions": [],
                    "pending": {
                        "stage": "publish",
                        "scope_digest": None,
                        "publish_digest": "6b66f7af" + "0" * 56,
                        "deadline": "2026-10-30T07:13:02Z",
                        "proposal": {
                            "repository": "o/r",
                            "remote_branch": f"agentforge/{RUN}",
                            "base_branch": "main",
                            "base_sha": "b" * 40,
                            "head_sha": "e" * 40,
                            "files": [{"path": "a.py", "change": "modified", "sha256": "1" * 64}],
                            "qa_checks": [{"program": "pytest", "exit_code": 0}],
                            "scan": {"findings": []},
                        },
                    },
                },
            )
        if request.url.path.endswith("/approve"):
            return httpx.Response(
                200,
                json={
                    "run_id": RUN,
                    "status": "published",
                    "publication": {
                        "state": "published",
                        "pr_url": "https://github.com/o/r/pull/1",
                    },
                },
            )
        if request.url.path == "/api/v1/runs":
            return httpx.Response(
                200, json={"items": [{"run_id": RUN, "status": "published"}], "limit": 20}
            )
        return httpx.Response(404, json={"detail": "nope"})

    client = httpx.Client(base_url="http://test/api/v1", transport=httpx.MockTransport(handler))
    yield client, calls
    client.close()


def test_task_sends_the_mode_and_approve_binds_the_shown_digest(api, capsys):
    client, calls = api
    assert (
        cli.run(
            [
                "--project",
                "sandbox",
                "task",
                "Implementa greet en src/a.py",
                "--repository",
                "sandbox",
                "--mode",
                "full",
            ],
            client=client,
        )
        == 0
    )
    assert calls[-1][2] == {
        "task": "Implementa greet en src/a.py",
        "project_id": "sandbox",
        "workflow_kind": "development",
        "repository_id": "sandbox",
        "route_mode": "full",
    }
    assert cli.run(["--project", "sandbox", "pending", RUN], client=client) == 0
    out = capsys.readouterr().out
    assert (
        "Etapa pendiente: publish" in out
        and "Digest: 6b66f7af" in out
        and "Hallazgos del escaneo: 0" in out
    )
    # Wrong digest: nothing is sent.
    before = len(calls)
    assert (
        cli.run(["--project", "sandbox", "approve", RUN, "--digest", "f" * 64], client=client) == 1
    )
    assert len(calls) == before + 1 and calls[-1][0] == "GET"
    # Right digest, non-interactive: the exact digest travels in publish_digest.
    assert (
        cli.run(
            ["--project", "sandbox", "approve", RUN, "--digest", "6b66f7af" + "0" * 56],
            client=client,
        )
        == 0
    )
    assert calls[-1] == (
        "POST",
        f"/api/v1/runs/{RUN}/approve",
        {"approved": True, "stage": "publish", "publish_digest": "6b66f7af" + "0" * 56},
    )
    assert "pull/1" in capsys.readouterr().out
    assert cli.run(["--project", "sandbox", "runs", "--status", "published"], client=client) == 0
    assert calls[-1][1] == "/api/v1/runs?limit=20&status=published&project_id=sandbox"


def test_interactive_confirmation_requires_the_digest_prefix(api, capsys, monkeypatch):
    client, calls = api
    monkeypatch.setattr("builtins.input", lambda _prompt: "deadbeef")
    assert cli.run(["--project", "sandbox", "approve", RUN], client=client) == 1
    assert calls[-1][0] == "GET"
    monkeypatch.setattr("builtins.input", lambda _prompt: "6b66f7af")
    assert cli.run(["--project", "sandbox", "reject", RUN], client=client) == 0
    assert calls[-1][2] == {
        "approved": False,
        "stage": "publish",
        "publish_digest": "6b66f7af" + "0" * 56,
    }


GOAL = "7b2f1c9e-1d3a-4c5b-9e8f-0a1b2c3d4e5f"


@pytest.fixture
def goals_api(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    calls: list[tuple[str, str, dict | None]] = []
    goal = {
        "goal_id": GOAL,
        "status": "active",
        "repository_id": "sandbox",
        "decomposition": "plan",
        "route": None,
        "plan_run_id": RUN,
        "blocked_reasons": ["merge_pending"],
        "usage": {"tasks_created": 1, "llm_calls_used": 5, "reserved_model_calls": 10},
        "deadline": "2026-11-06T09:00:00Z",
        "tasks": [
            {
                "task_key": "a",
                "status": "published",
                "run_id": RUN,
                "pr_number": 3,
                "dependencies": [],
            },
            {
                "task_key": "b",
                "status": "pending",
                "run_id": None,
                "dependencies": ["a"],
                "pending_stage": None,
            },
        ],
        "error": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "POST" and request.url.path == "/api/v1/goals":
            return httpx.Response(201, json=goal)
        if request.method == "GET" and request.url.path == f"/api/v1/goals/{GOAL}":
            return httpx.Response(200, json=goal)
        if request.method == "GET" and request.url.path == "/api/v1/goals":
            return httpx.Response(200, json={"items": [{"goal_id": GOAL, "status": "active"}]})
        if request.method == "POST" and request.url.path == f"/api/v1/goals/{GOAL}/cancel":
            return httpx.Response(200, json={**goal, "status": "cancelled"})
        if request.url.path == f"/api/v1/goals/{GOAL}/events":
            return httpx.Response(
                200,
                json={"items": [{"created_at": "t", "kind": "goal_created", "payload": {}}]},
            )
        return httpx.Response(404, json={"code": "not_found", "detail": request.url.path})

    client = httpx.Client(base_url="http://api.test/api/v1", transport=httpx.MockTransport(handler))
    yield client, calls
    client.close()


def test_goal_commands_build_the_requests_and_describe_the_goal(goals_api, capsys):
    client, calls = goals_api
    assert (
        cli.run(
            [
                "--project",
                "sandbox",
                "goal",
                "Añade validación de correo con pruebas",
                "--repository",
                "sandbox",
                "--decomposition",
                "plan",
                "--max-tasks",
                "3",
                "--deadline-days",
                "2",
            ],
            client=client,
        )
        == 0
    )
    assert calls[-1][2] == {
        "objective": "Añade validación de correo con pruebas",
        "project_id": "sandbox",
        "repository_id": "sandbox",
        "decomposition": "plan",
        "limits": {"max_tasks": 3, "deadline_seconds": 172800.0},
    }
    out = capsys.readouterr().out
    assert f"Objetivo {GOAL}: active" in out and "Bloqueado por: merge_pending" in out
    assert "a          published" in out and "PR #3" in out and "← a" in out
    assert cli.run(["--project", "sandbox", "goals", "--status", "active"], client=client) == 0
    assert calls[-1][1] == "/api/v1/goals"
    assert cli.run(["--project", "sandbox", "goal-show", GOAL], client=client) == 0
    assert cli.run(["--project", "sandbox", "goal-events", GOAL], client=client) == 0
    assert cli.run(["--project", "sandbox", "goal-cancel", GOAL], client=client) == 0
    assert calls[-1] == ("POST", f"/api/v1/goals/{GOAL}/cancel", None)
    assert '"status": "cancelled"' in capsys.readouterr().out

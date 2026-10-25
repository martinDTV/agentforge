"""``RemoteSandboxAdapter`` against an httpx mock transport.

No network, no runner, no host execution."""

import json
import uuid
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.infrastructure.sandbox import RemoteSandboxAdapter, RunnerConfig, is_tailnet_host
from app.sandbox import (
    ProgramId,
    SandboxError,
    SandboxLimits,
    SandboxRequest,
    WorkspaceArchive,
)
from tests.unit.tool_fixtures import make_context

TOKEN = "runner-token-0123456789abcdef"
RUNNER = "agentforge-runner-01"
HEALTH_OK = {
    "runner_id": RUNNER,
    "version": "0.1.0",
    "backend": "podman-rootless",
    "rootless": True,
    "profiles": ["python-3.13"],
}
Handler = Callable[[httpx.Request], httpx.Response]


def config(url: str = "http://100.101.102.103:8787") -> RunnerConfig:
    return RunnerConfig(base_url=url, token=SecretStr(TOKEN), expected_runner_id=RUNNER)


def adapter(handler: Handler, url: str = "http://100.101.102.103:8787") -> RemoteSandboxAdapter:
    return RemoteSandboxAdapter(config(url), transport=httpx.MockTransport(handler))


def request() -> SandboxRequest:
    context = make_context()
    return SandboxRequest(
        execution_id=uuid.uuid4(),
        run_id=context.run_id,
        attempt=0,
        step="tests",
        project_id="alpha",
        agent=context.agent,
        actor_subject=context.principal.subject,
        repository_id="demo",
        branch=f"run/{context.run_id}",
        program=ProgramId.PYTEST,
        argv=["python", "-I", "-m", "pytest", "--", "tests"],
        profile="python-3.13",
        limits=SandboxLimits(wall_seconds=5),
        workspace_sha256="a" * 64,
        workspace_bytes=3,
    )


ARCHIVE = WorkspaceArchive(sha256="a" * 64, size_bytes=3, file_count=1, content=b"tgz")


def result_for(req: SandboxRequest, **overrides: object) -> dict[str, object]:
    return {
        "execution_id": str(req.execution_id),
        "status": "completed",
        "exit_code": 0,
        "stdout": "1 passed\n",
        "stderr": "",
        "duration_ms": 1200,
        "runner_id": RUNNER,
        "backend": "podman-rootless",
        **overrides,
    }


def scripted(
    health: dict[str, object] | None = HEALTH_OK,
    execution: Callable[[httpx.Request], httpx.Response] | None = None,
) -> tuple[Handler, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/v1/health":
            return httpx.Response(200, json=health) if health else httpx.Response(500)
        if req.url.path == "/v1/executions" and req.method == "POST":
            assert execution is not None
            return execution(req)
        if req.url.path.startswith("/v1/executions/") and req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    return handler, seen


def test_only_private_transports_are_accepted():
    assert is_tailnet_host("100.64.0.1") and is_tailnet_host("100.127.255.254")
    assert is_tailnet_host("runner.tailnet-abcd.ts.net") and not is_tailnet_host("100.128.0.1")
    config("https://runner.example.internal")
    config("http://runner.tailnet-abcd.ts.net:8787")
    config("http://127.0.0.1:8787")  # API and runner on the same machine
    for insecure in ("http://192.168.1.20:8787", "http://runner.example.com", "ftp://100.64.0.1"):
        with pytest.raises(ValidationError):
            config(insecure)
    # A local VM or a VPC over plain HTTP needs the explicit opt-in, and never a public host.
    RunnerConfig(
        base_url="http://192.168.64.5:8787",
        token=SecretStr(TOKEN),
        expected_runner_id=RUNNER,
        allow_private_http=True,
    )
    with pytest.raises(ValidationError):
        RunnerConfig(
            base_url="http://8.8.8.8:8787",
            token=SecretStr(TOKEN),
            expected_runner_id=RUNNER,
            allow_private_http=True,
        )
    assert TOKEN not in repr(config()) and TOKEN not in str(config())


def test_runner_identity_and_backend_are_verified_before_anything_runs():
    handler, seen = scripted(health={**HEALTH_OK, "runner_id": "someone-else"})
    client = adapter(handler)
    assert client.health().verified is False
    with pytest.raises(SandboxError) as exc:
        client.run(request(), ARCHIVE)
    assert exc.value.code == "runner_unverified"
    assert [r.url.path for r in seen] == ["/v1/health", "/v1/health"]  # never POSTed
    for broken in ({**HEALTH_OK, "backend": "docker"}, {**HEALTH_OK, "rootless": False}):
        handler, _ = scripted(health=broken)
        assert adapter(handler).health().verified is False


def test_successful_execution_sends_the_plan_and_the_archive_with_idempotency():
    req = request()

    def execution(http_request: httpx.Request) -> httpx.Response:
        assert http_request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert http_request.headers["Idempotency-Key"] == str(req.execution_id)
        assert http_request.headers["Content-Type"].startswith("multipart/form-data")
        body = http_request.content
        assert b'"program":"pytest"' in body and b'"network":"none"' in body
        assert b"tgz" in body and b'"untrusted":true' in body
        return httpx.Response(200, json=result_for(req))

    handler, seen = scripted(execution=execution)
    client = adapter(handler)
    assert client._client.trust_env is False  # no proxies, no .netrc from the host
    result = client.run(req, ARCHIVE)
    assert result.succeeded and result.stdout == "1 passed\n" and result.runner_id == RUNNER
    assert [r.method for r in seen] == ["GET", "POST"]
    # The verification is cached: a second run does not re-check health.
    client.run(req, ARCHIVE)
    assert [r.method for r in seen] == ["GET", "POST", "POST"]


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (409, {"code": "execution_in_progress"}, "execution_in_progress"),
        (401, {}, "runner_auth_failed"),
        (403, {}, "runner_auth_failed"),
        (422, {"code": "program_not_allowed"}, "runner_rejected"),
        (500, {}, "runner_failed"),
        (302, {}, "runner_protocol_error"),
    ],
)
def test_runner_answers_map_to_stable_codes(status: int, body: dict[str, object], code: str):
    handler, _ = scripted(execution=lambda _r: httpx.Response(status, json=body))
    with pytest.raises(SandboxError) as exc:
        adapter(handler).run(request(), ARCHIVE)
    assert exc.value.code == code


def test_results_from_another_runner_or_execution_are_refused():
    req = request()
    handler, _ = scripted(
        execution=lambda _r: httpx.Response(
            200, json=result_for(req, execution_id=str(uuid.uuid4()))
        )
    )
    with pytest.raises(SandboxError) as exc:
        adapter(handler).run(req, ARCHIVE)
    assert exc.value.code == "runner_protocol_error"
    handler, _ = scripted(
        execution=lambda _r: httpx.Response(200, json=result_for(req, runner_id="x"))
    )
    with pytest.raises(SandboxError) as exc:
        adapter(handler).run(req, ARCHIVE)
    assert exc.value.code == "runner_unverified"
    handler, _ = scripted(execution=lambda _r: httpx.Response(200, content=b"not json"))
    with pytest.raises(SandboxError) as exc:
        adapter(handler).run(req, ARCHIVE)
    assert exc.value.code == "runner_protocol_error"


def test_unreachable_runner_is_an_error_never_a_local_fallback():
    def down(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = adapter(down)
    with pytest.raises(SandboxError) as exc:
        client.health()
    assert exc.value.code == "runner_unreachable" and exc.value.retryable
    with pytest.raises(SandboxError) as exc:
        client.run(request(), ARCHIVE)
    assert exc.value.code == "runner_unreachable"
    handler, _ = scripted(health=None)
    with pytest.raises(SandboxError) as exc:
        adapter(handler).run(request(), ARCHIVE)
    assert exc.value.code == "runner_protocol_error"


def test_read_timeout_cancels_the_remote_execution():
    req = request()
    deleted: list[str] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.url.path == "/v1/health":
            return httpx.Response(200, json=HEALTH_OK)
        if http_request.method == "POST":
            raise httpx.ReadTimeout("slow runner")
        if http_request.method == "DELETE":
            deleted.append(http_request.url.path)
            return httpx.Response(204)
        return httpx.Response(404)

    client = adapter(handler)
    with pytest.raises(SandboxError) as exc:
        client.run(req, ARCHIVE)
    assert exc.value.code == "runner_timeout"
    assert deleted == [f"/v1/executions/{req.execution_id}"]
    assert client.cancel(req.execution_id) is True


def test_cancel_reports_what_the_runner_did():
    handler, _ = scripted(execution=None)
    assert adapter(handler).cancel(uuid.uuid4()) is True  # 204 from the scripted handler

    def missing(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    assert adapter(missing).cancel(uuid.uuid4()) is False

    def down(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert adapter(down).cancel(uuid.uuid4()) is False


def test_oversized_responses_are_refused():
    req = request()
    huge = result_for(req, stdout="x" * 10_000)
    cfg = RunnerConfig(
        base_url="http://100.64.0.9",
        token=SecretStr(TOKEN),
        expected_runner_id=RUNNER,
        max_response_bytes=4096,
    )
    handler, _ = scripted(
        execution=lambda _r: httpx.Response(200, content=json.dumps(huge).encode())
    )
    client = RemoteSandboxAdapter(cfg, transport=httpx.MockTransport(handler))
    with pytest.raises(SandboxError) as exc:
        client.run(req, ARCHIVE)
    assert exc.value.code == "runner_protocol_error"

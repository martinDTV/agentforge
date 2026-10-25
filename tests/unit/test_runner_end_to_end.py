"""The API's RemoteSandboxAdapter talking to the real runner service over HTTP on
loopback, with the scripted backend behind it. No container, no network beyond
this process."""

import socket
import threading
import time
import uuid
from pathlib import Path

import pytest
import uvicorn
from pydantic import SecretStr

from app.infrastructure.sandbox import RemoteSandboxAdapter, RunnerConfig
from app.sandbox import ProgramId, SandboxError, SandboxLimits, SandboxRequest, WorkspaceArchive
from tests.unit.runner_fixtures import DEFAULT_ARCHIVE, RUNNER_ID, TOKEN, make_harness
from tests.unit.tool_fixtures import make_context


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def served(tmp_path: Path):  # type: ignore[no-untyped-def]
    harness = make_harness(tmp_path, allowed_client_cidrs=["127.0.0.1/32"])
    port = _free_port()
    config = uvicorn.Config(
        harness.app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield harness, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _request(run_id: uuid.UUID) -> SandboxRequest:
    context = make_context(run_id=run_id)
    import hashlib

    return SandboxRequest(
        execution_id=uuid.uuid4(),
        run_id=run_id,
        attempt=0,
        step="lint",
        project_id="alpha",
        agent=context.agent,
        actor_subject=context.principal.subject,
        repository_id="demo",
        branch=f"run/{run_id}",
        program=ProgramId.RUFF_CHECK,
        argv=["ruff", "check", "--no-cache", "--output-format", "concise", "--", "src"],
        profile="python-3.13",
        limits=SandboxLimits(),
        workspace_sha256=hashlib.sha256(DEFAULT_ARCHIVE).hexdigest(),
        workspace_bytes=len(DEFAULT_ARCHIVE),
    )


def test_adapter_and_runner_speak_the_same_protocol(served):  # type: ignore[no-untyped-def]
    harness, url = served
    adapter = RemoteSandboxAdapter(
        RunnerConfig(base_url=url, token=SecretStr(TOKEN), expected_runner_id=RUNNER_ID)
    )
    health = adapter.health()
    assert health.verified and health.backend == "podman-rootless"
    import hashlib

    archive = WorkspaceArchive(
        sha256=hashlib.sha256(DEFAULT_ARCHIVE).hexdigest(),
        size_bytes=len(DEFAULT_ARCHIVE),
        file_count=3,
        content=DEFAULT_ARCHIVE,
    )
    request = _request(uuid.uuid4())
    result = adapter.run(request, archive)
    assert (
        result.succeeded
        and result.runner_id == RUNNER_ID
        and result.execution_id == request.execution_id
    )
    assert len(harness.backend.runs) == 1 and harness.backend.runs[0].argv == request.argv
    replay = adapter.run(request, archive)  # idempotent: same answer, still one container
    assert replay == result and len(harness.backend.runs) == 1
    tampered = request.model_copy(update={"argv": [*request.argv[:-1], "."]})
    with pytest.raises(SandboxError) as exc:
        adapter.run(tampered, archive)
    assert exc.value.code == "execution_conflict"
    assert adapter.cancel(request.execution_id) is False  # already finished: 409 -> False
    wrong = RemoteSandboxAdapter(
        RunnerConfig(base_url=url, token=SecretStr("not-the-token"), expected_runner_id=RUNNER_ID)
    )
    with pytest.raises(SandboxError) as exc:
        wrong.health()
    assert exc.value.code == "runner_auth_failed"
    impostor = RemoteSandboxAdapter(
        RunnerConfig(base_url=url, token=SecretStr(TOKEN), expected_runner_id="someone-else")
    )
    with pytest.raises(SandboxError) as exc:
        impostor.run(request, archive)
    assert exc.value.code == "runner_unverified"
    adapter.close()

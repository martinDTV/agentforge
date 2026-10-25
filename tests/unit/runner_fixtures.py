"""Builders for the runner's unit tests: in-memory settings, fake backend, archives."""

import gzip
import hashlib
import io
import json
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentforge_runner.auth import token_digest
from agentforge_runner.config import RunnerSettings
from agentforge_runner.fake_backend import FakeBackend
from agentforge_runner.protocol import ProgramId, SandboxLimits, SandboxRequest
from agentforge_runner.service import create_runner_app
from agentforge_runner.store import ExecutionStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

TOKEN = "test-runner-token-0123456789abcdefghijklmnop"
RUNNER_ID = "runner-under-test"
IMAGE = "localhost/agentforge/python-3.13@sha256:" + "ab" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TAILNET_CLIENT = ("100.64.0.7", 51000)


def make_settings(tmp_path: Path, **overrides: Any) -> RunnerSettings:
    values: dict[str, Any] = {
        "runner_id": RUNNER_ID,
        "token_sha256": token_digest(TOKEN),
        "state_dir": tmp_path / "state",
        "images": {"python-3.13": IMAGE},
        "max_concurrent": 1,
        "health_cache_seconds": 0,
        "probe_cache_seconds": 0,
        **overrides,
    }
    return RunnerSettings.model_validate(values)


@dataclass
class RunnerHarness:
    settings: RunnerSettings
    backend: FakeBackend
    store: ExecutionStore
    app: FastAPI

    def client(self, *, address: tuple[str, int] = TAILNET_CLIENT) -> TestClient:
        return TestClient(self.app, client=address)


def make_harness(tmp_path: Path, **overrides: Any) -> RunnerHarness:
    settings = make_settings(tmp_path, **overrides)
    backend = FakeBackend()
    store = ExecutionStore(settings.state_dir / "executions.sqlite3")
    return RunnerHarness(settings, backend, store, create_runner_app(settings, backend, store))


# -- archives ---------------------------------------------------------------------------------


def _entry(tar: tarfile.TarFile, name: str, data: bytes = b"", **attrs: Any) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mode = attrs.pop("mode", 0o644)
    for key, value in attrs.items():
        setattr(info, key, value)
    tar.addfile(info, io.BytesIO(data) if data else None)


def build_archive(
    entries: list[tuple[str, bytes]] | None = None, *, raw: list[dict[str, Any]] | None = None
) -> bytes:
    """``entries`` are regular files; ``raw`` lets a test craft hostile members."""
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        for name, data in entries or []:
            _entry(tar, name, data)
        for member in raw or []:
            _entry(tar, **member)
    return buffer.getvalue()


DEFAULT_ARCHIVE = build_archive(
    [
        ("README.md", b"# demo\n"),
        ("src/app.py", b"x = 1\n"),
        ("tests/test_x.py", b"def test_x(): pass\n"),
    ]
)


def make_request(archive: bytes = DEFAULT_ARCHIVE, **overrides: Any) -> SandboxRequest:
    run_id = overrides.pop("run_id", uuid.uuid4())
    program = overrides.pop("program", ProgramId.RUFF_CHECK)
    argv = overrides.pop("argv", None)
    if argv is None:
        argv = {
            ProgramId.RUFF_CHECK: [
                "ruff",
                "check",
                "--no-cache",
                "--output-format",
                "concise",
                "--",
                "src",
            ],
            ProgramId.RUFF_FORMAT_CHECK: ["ruff", "format", "--check", "--no-cache", "--", "."],
            ProgramId.PYTEST: [
                "python",
                "-I",
                "-P",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--",
                "tests",
            ],
        }[program]
    values: dict[str, Any] = {
        "execution_id": overrides.pop("execution_id", uuid.uuid4()),
        "run_id": run_id,
        "attempt": 0,
        "step": "lint",
        "project_id": "alpha",
        "agent": "developer",
        "actor_subject": "agentforge:developer",
        "repository_id": "demo",
        "branch": f"run/{run_id}",
        "program": program,
        "argv": argv,
        "profile": "python-3.13",
        "limits": SandboxLimits(),
        "workspace_sha256": hashlib.sha256(archive).hexdigest(),
        "workspace_bytes": len(archive),
        **overrides,
    }
    return SandboxRequest.model_validate(values)


def multipart(
    request: SandboxRequest | dict[str, Any], archive: bytes = DEFAULT_ARCHIVE
) -> dict[str, Any]:
    payload = (
        request.model_dump_json() if isinstance(request, SandboxRequest) else json.dumps(request)
    )
    return {
        "request": ("request.json", payload.encode(), "application/json"),
        "workspace": ("workspace.tar.gz", archive, "application/gzip"),
    }

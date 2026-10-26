"""Requests the runner must refuse before touching a container."""

import hashlib
from pathlib import Path
from typing import Any

import pytest
from agentforge_runner.errors import RunnerError
from agentforge_runner.programs import PREFIXES, validate_argv
from agentforge_runner.protocol import ProgramId

from tests.unit.runner_fixtures import AUTH, DEFAULT_ARCHIVE, make_harness, make_request, multipart


def post(harness: Any, request: Any, archive: bytes = DEFAULT_ARCHIVE) -> Any:
    with harness.client() as client:
        return client.post("/v1/executions", files=multipart(request, archive), headers=AUTH)


@pytest.mark.parametrize(
    ("program", "argv"),
    [
        (ProgramId.RUFF_CHECK, ["bash", "-c", "id"]),
        (ProgramId.RUFF_CHECK, ["ruff", "check", "--", "src"]),  # prefix altered
        (ProgramId.RUFF_CHECK, [*PREFIXES[ProgramId.RUFF_CHECK], "--fix", "--", "src"]),
        (ProgramId.RUFF_CHECK_DIFF, [*PREFIXES[ProgramId.RUFF_CHECK_DIFF], "--fix", "--", "src"]),
        (ProgramId.RUFF_CHECK_DIFF, ["ruff", "check", "--no-cache", "--fix", "--", "src"]),
        (ProgramId.RUFF_CHECK, [*PREFIXES[ProgramId.RUFF_CHECK], "src"]),  # no separator
        (ProgramId.RUFF_CHECK, [*PREFIXES[ProgramId.RUFF_CHECK], "--"]),  # no paths
        (ProgramId.RUFF_CHECK, [*PREFIXES[ProgramId.RUFF_CHECK], "--", "../etc"]),
        (ProgramId.RUFF_CHECK, [*PREFIXES[ProgramId.RUFF_CHECK], "--", "-rf"]),
        (ProgramId.RUFF_CHECK, [*PREFIXES[ProgramId.RUFF_CHECK], "--", "a;b"]),
        (ProgramId.PYTEST, [*PREFIXES[ProgramId.PYTEST], "--", "."]),  # "." only for ruff
        (ProgramId.PYTEST, [*PREFIXES[ProgramId.PYTEST], "-k", "x --collect-only", "--", "tests"]),
        (ProgramId.PYTEST, [*PREFIXES[ProgramId.PYTEST], "--rootdir=/", "--", "tests"]),
        (
            ProgramId.PYTEST,
            [*PREFIXES[ProgramId.PYTEST], "-x", "-k", "x", "--", "tests"],
        ),  # wrong order
        (ProgramId.PYTEST, [*PREFIXES[ProgramId.RUFF_CHECK], "--", "tests"]),
    ],
)
def test_argv_that_departs_from_the_allowlist_is_rejected(program: ProgramId, argv: list[str]):
    with pytest.raises(RunnerError) as exc:
        validate_argv(program, argv)
    assert exc.value.code == "argv_rejected" and exc.value.status == 422


def test_argv_that_matches_the_allowlist_yields_its_paths():
    assert validate_argv(
        ProgramId.RUFF_CHECK_DIFF, [*PREFIXES[ProgramId.RUFF_CHECK_DIFF], "--", "src", "tests"]
    ) == ["src", "tests"]
    assert validate_argv(ProgramId.RUFF_CHECK, [*PREFIXES[ProgramId.RUFF_CHECK], "--", "."]) == [
        "."
    ]
    assert validate_argv(
        ProgramId.PYTEST, [*PREFIXES[ProgramId.PYTEST], "-k", "x and y", "-x", "--", "tests/unit"]
    ) == ["tests/unit"]
    assert validate_argv(
        ProgramId.RUFF_FORMAT_CHECK, [*PREFIXES[ProgramId.RUFF_FORMAT_CHECK], "--", "app", "tests"]
    ) == ["app", "tests"]


def test_service_rejects_bad_argv_limits_profiles_and_policies(tmp_path: Path):
    harness = make_harness(tmp_path)
    cases = [
        (make_request(argv=["bash", "-c", "id"]), 422, "argv_rejected"),
        (make_request(limits={"wall_seconds": 3000}), 422, "limits_rejected"),
        (make_request(limits={"memory_bytes": 8192 * 1024 * 1024}), 422, "limits_rejected"),
        (make_request(profile="node-20"), 422, "profile_unknown"),
    ]
    for request, status, code in cases:
        response = post(harness, request)
        assert (response.status_code, response.json()["code"]) == (status, code), request.argv
    raw = make_request().model_dump(mode="json")
    for mutation in (
        {"network": "egress"},
        {"untrusted": False},
        {"env": {"GIT_SSH_COMMAND": "x"}},
        {"command": "ls"},
        {"argv": []},
    ):
        response = post(harness, {**raw, **mutation})
        assert response.status_code == 422 and response.json()["code"] == "request_invalid", (
            mutation
        )
    assert harness.backend.runs == []
    assert harness.store.get(make_request().execution_id) is None  # nothing was claimed


def test_archive_must_match_the_request_and_fit(tmp_path: Path):
    harness = make_harness(tmp_path, max_archive_bytes=4096)
    other = DEFAULT_ARCHIVE + b"\x00"
    request = make_request(DEFAULT_ARCHIVE)
    mismatch = post(harness, request, other)
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "archive_mismatch"
    lying = make_request(DEFAULT_ARCHIVE, workspace_sha256=hashlib.sha256(b"x").hexdigest())
    assert post(harness, lying).json()["code"] == "archive_mismatch"
    big = b"\x00" * 5000
    too_big = post(harness, make_request(big), big)
    assert too_big.status_code == 413 and too_big.json()["code"] == "archive_too_large"
    assert harness.backend.runs == []


def test_paths_must_exist_inside_the_shipped_archive(tmp_path: Path):
    harness = make_harness(tmp_path)
    request = make_request(argv=[*PREFIXES[ProgramId.RUFF_CHECK], "--", "missing_dir"])
    response = post(harness, request)
    assert response.status_code == 422 and response.json()["code"] == "path_rejected"
    assert harness.backend.runs == [] and harness.store.get(request.execution_id) is None
    nested = make_request(argv=[*PREFIXES[ProgramId.RUFF_CHECK], "--", "src/app.py", "tests"])
    assert post(harness, nested).status_code == 200  # a file and a directory implied by a file

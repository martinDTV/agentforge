"""Program allowlist, argv building and the sandbox request/archive contracts."""

import tarfile
from io import BytesIO
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.sandbox import (
    DEFAULT_SANDBOX_ENV,
    ProgramId,
    SandboxError,
    SandboxLimits,
    SandboxRequest,
    SandboxResult,
    SandboxStatus,
    build_plan,
    pack_worktree,
)
from app.sandbox.ports import MiB
from tests.unit.tool_fixtures import make_context


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "app").mkdir(parents=True)
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "app" / "main.py").write_text("x = 1\n")
    (root / "tests" / "unit" / "test_x.py").write_text("def test_x(): pass\n")
    (root / "README.md").write_text("# demo\n")
    return root


def test_programs_build_exact_argv_from_fixed_prefixes(tree: Path):
    check = build_plan(ProgramId.RUFF_CHECK, [], worktree=tree, authorized_test_paths={"tests"})
    assert check.argv == ["ruff", "check", "--no-cache", "--output-format", "concise", "--", "."]
    fmt = build_plan(
        ProgramId.RUFF_FORMAT_CHECK,
        ["app", "tests/unit"],
        worktree=tree,
        authorized_test_paths={"tests"},
    )
    assert fmt.argv == ["ruff", "format", "--check", "--no-cache", "--", "app", "tests/unit"]
    tests = build_plan(ProgramId.PYTEST, [], worktree=tree, authorized_test_paths={"tests"})
    assert tests.argv == [
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
    ]
    narrowed = build_plan(
        ProgramId.PYTEST,
        ["tests/unit/test_x.py"],
        worktree=tree,
        authorized_test_paths={"tests"},
        keyword="x and not slow",
        fail_fast=True,
    )
    assert narrowed.argv[-5:] == ["-k", "x and not slow", "-x", "--", "tests/unit/test_x.py"]
    assert narrowed.profile == "python-3.13" and narrowed.program is ProgramId.PYTEST


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("-x", "path_invalid"),
        ("--help", "path_invalid"),
        ("../etc/passwd", "path_invalid"),
        ("/etc/passwd", "path_invalid"),
        ("app; rm -rf /", "path_invalid"),
        ("app && echo", "path_invalid"),
        ("$(whoami)", "path_invalid"),
        ("`id`", "path_invalid"),
        ("app|tee", "path_invalid"),
        ("app\nmain.py", "path_invalid"),
        ("app/missing.py", "path_not_found"),
    ],
)
def test_dangerous_or_missing_paths_are_refused(tree: Path, path: str, code: str):
    with pytest.raises(SandboxError) as exc:
        build_plan(ProgramId.RUFF_CHECK, [path], worktree=tree, authorized_test_paths={"tests"})
    assert exc.value.code == code


def test_pytest_is_confined_to_authorized_test_paths_and_typed_options(tree: Path):
    for path in ("app", ".", "README.md", "tests_other"):
        with pytest.raises(SandboxError) as exc:
            build_plan(ProgramId.PYTEST, [path], worktree=tree, authorized_test_paths={"tests"})
        assert exc.value.code in {"path_not_authorized", "path_not_found"}, path
    (tree / "tests_other").mkdir()
    with pytest.raises(SandboxError) as exc:
        build_plan(
            ProgramId.PYTEST, ["tests_other"], worktree=tree, authorized_test_paths={"tests"}
        )
    assert exc.value.code == "path_not_authorized"  # prefix "tests" is not "tests_other"
    for keyword in ("x --collect-only", "x; rm", "$(x)", "-k", ""):
        with pytest.raises(SandboxError) as exc:
            build_plan(
                ProgramId.PYTEST,
                [],
                worktree=tree,
                authorized_test_paths={"tests"},
                keyword=keyword,
            )
        assert exc.value.code == "keyword_invalid", keyword
    with pytest.raises(SandboxError):
        build_plan(
            ProgramId.RUFF_CHECK, [], worktree=tree, authorized_test_paths=set(), keyword="x"
        )
    with pytest.raises(SandboxError):
        build_plan(
            ProgramId.RUFF_CHECK, [], worktree=tree, authorized_test_paths=set(), fail_fast=True
        )
    with pytest.raises(SandboxError) as exc:
        build_plan(ProgramId.RUFF_CHECK, ["app"] * 51, worktree=tree, authorized_test_paths=set())
    assert exc.value.code == "path_invalid"


def test_symlinked_paths_that_leave_the_tree_are_refused(tree: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tree / "escape").symlink_to(outside)
    with pytest.raises(SandboxError) as exc:
        build_plan(ProgramId.RUFF_CHECK, ["escape"], worktree=tree, authorized_test_paths=set())
    assert exc.value.code == "path_invalid"


def _request(**overrides: object) -> SandboxRequest:
    context = make_context()
    base: dict[str, object] = {
        "execution_id": context.run_id,
        "run_id": context.run_id,
        "attempt": 0,
        "step": "s",
        "project_id": "alpha",
        "agent": context.agent,
        "actor_subject": context.principal.subject,
        "repository_id": "demo",
        "branch": f"run/{context.run_id}",
        "program": ProgramId.RUFF_CHECK,
        "argv": ["ruff", "check", "--", "."],
        "profile": "python-3.13",
        "limits": SandboxLimits(),
        "workspace_sha256": "0" * 64,
        "workspace_bytes": 10,
    }
    return SandboxRequest.model_validate({**base, **overrides})


def test_requests_only_accept_the_closed_vocabulary():
    request = _request()
    assert request.network == "none" and request.untrusted is True
    assert request.env == DEFAULT_SANDBOX_ENV
    for bad in (
        {"network": "egress"},
        {"untrusted": False},
        {"env": {"GIT_TOKEN": "x"}},
        {"env": {"PYTHONPATH": "/x"}},
        {"env": {"AWS_SECRET_ACCESS_KEY": "x"}},
        {"env": {"lowercase": "x"}},
        {"argv": ["ruff", "check\n--", "."]},
        {"argv": []},
        {"profile": "../evil"},
        {"image": "docker.io/evil"},
        {"command": "rm -rf /"},
        {"head_sha": "not-a-sha"},
    ):
        with pytest.raises(ValidationError):
            _request(**bad)


def test_limits_are_explicit_and_bounded():
    limits = SandboxLimits()
    assert (limits.cpu_seconds, limits.memory_bytes, limits.pids) == (60.0, 1024 * MiB, 256)
    assert (limits.wall_seconds, limits.storage_bytes, limits.max_output_bytes) == (
        120.0,
        512 * MiB,
        256_000,
    )
    for bad in (
        {"cpu_seconds": 0},
        {"cpu_seconds": 3601},
        {"memory_bytes": 1},
        {"pids": 1},
        {"wall_seconds": 4000},
        {"storage_bytes": 0},
        {"max_output_bytes": 1},
        {"gpus": 1},
    ):
        with pytest.raises(ValidationError):
            SandboxLimits(**bad)  # type: ignore[arg-type]
    result = SandboxResult(
        execution_id=_request().execution_id,
        status=SandboxStatus.COMPLETED,
        exit_code=0,
        duration_ms=1,
        runner_id="r",
    )
    assert result.succeeded
    assert not result.model_copy(update={"exit_code": 1}).succeeded
    assert not result.model_copy(update={"status": SandboxStatus.TIMED_OUT}).succeeded


def test_archives_are_deterministic_bounded_and_never_ship_secrets(tree: Path, tmp_path: Path):
    (tree / ".git").write_text(
        "gitdir: /srv/agentforge/repos/demo.git/worktrees/x\n"
    )  # linked worktree
    (tree / "sub").mkdir()
    (tree / "sub" / ".git").mkdir()
    (tree / "sub" / ".git" / "config").write_text("[core]\n")
    (tree / ".env").write_text("SECRET=1\n")
    (tree / "keys.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    (tree / "__pycache__").mkdir()
    (tree / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    outside = tmp_path / "outside.txt"
    outside.write_text("host\n")
    (tree / "link.txt").symlink_to(outside)
    (tree / "run.sh").write_text("#!/bin/sh\n")
    (tree / "run.sh").chmod(0o755)
    first = pack_worktree(tree, max_bytes=1024 * 1024)
    second = pack_worktree(tree, max_bytes=1024 * 1024)
    assert first.sha256 == second.sha256 and first.content == second.content
    with tarfile.open(fileobj=BytesIO(first.content), mode="r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
    assert set(members) == {"README.md", "app/main.py", "tests/unit/test_x.py", "run.sh"}
    assert first.file_count == 4 and members["run.sh"].mode == 0o755
    assert all(m.mtime == 0 and m.uid == 0 and m.uname == "" for m in members.values())
    assert "content" not in first.model_dump() and "content" not in first.model_dump_json()
    with pytest.raises(SandboxError) as exc:
        pack_worktree(tree, max_bytes=10)
    assert exc.value.code == "archive_too_large"

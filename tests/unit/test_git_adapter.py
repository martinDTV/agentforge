"""``LocalGitAdapter`` against temporary repositories only."""

import json
import os
import stat
import uuid
from pathlib import Path

import pytest

from app.git import GitIdentity, GitOperationError, RepositoryConfig, RepositoryRegistry
from app.infrastructure.git import LocalGitAdapter
from tests.unit.git_fixtures import SEED_IDENTITY, git, make_sandbox, seed_repository


def test_environment_is_sanitized_and_host_configuration_is_invisible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_never_leak_0123456789abcdef")
    monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
    adapter = LocalGitAdapter(scratch_home=tmp_path / "home")
    env = adapter._env(GitIdentity())
    assert not {"SSH_AUTH_SOCK", "GITHUB_TOKEN", "GIT_DIR", "GIT_WORK_TREE"} & env.keys()
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["HOME"] == str(tmp_path / "home")
    assert env["GIT_AUTHOR_NAME"] == "AgentForge"
    # Whatever ~/.gitconfig says on this machine, the adapter cannot see it.
    completed = adapter._run(["config", "--global", "--list"], cwd=tmp_path, ok_codes=(0, 1))
    assert completed.stdout.strip() == ""


def test_commits_carry_the_repository_service_identity_not_the_seeder_nor_the_host(tmp_path: Path):
    box = make_sandbox(tmp_path)
    record = box.manager.ensure(uuid.uuid4(), "alpha", "demo")
    (record.path / "NOTES.md").write_text("by an agent\n")
    commit = box.adapter.commit(record.path, box.config, "feat: notes")
    assert (commit.author_name, commit.author_email) == (
        "AgentForge",
        "agentforge@noreply.agentforge.local",
    )
    assert commit.subject == "feat: notes"
    seed = box.adapter.log(record.path, max_count=2)[-1]
    assert (seed.author_name, seed.author_email) == SEED_IDENTITY
    raw = git("log", "-1", "--format=%cn <%ce>", cwd=record.path, home=box.home)
    assert raw.strip() == "AgentForge <agentforge@noreply.agentforge.local>"


def test_write_operations_refuse_protected_refs_and_anything_but_run_branches(tmp_path: Path):
    box = make_sandbox(tmp_path)
    for name, code in (
        ("main", "ref_protected"),
        ("master", "ref_protected"),
        ("release/1.0", "ref_protected"),
        ("feature/x", "not_a_run_branch"),
        ("refs/tags/v1", "ref_invalid"),
    ):
        with pytest.raises(GitOperationError) as exc:
            box.adapter.create_branch(box.config, name, start_point="main")
        assert exc.value.code == code, name
        with pytest.raises(GitOperationError):
            box.adapter.worktree_add(box.config, tmp_path / "wt", name, start_point="main")
    assert not (tmp_path / "wt").exists()
    # A checkout sitting on main (a plain clone) cannot be committed through the adapter.
    work = seed_repository(box.repos_root / "plain", home=box.home)
    plain = box.registry.register(RepositoryConfig(id="plain", project_id="alpha", path=work))
    (work / "x.txt").write_text("x")
    with pytest.raises(GitOperationError) as exc:
        box.adapter.commit(work, plain, "nope")
    assert exc.value.code == "ref_protected"
    assert git("status", "--porcelain", cwd=work, home=box.home).strip() == "?? x.txt"


def test_push_is_disabled_whatever_the_branch(tmp_path: Path):
    box = make_sandbox(tmp_path)
    with pytest.raises(GitOperationError) as exc:
        box.adapter.push(box.config, f"run/{uuid.uuid4()}")
    assert exc.value.code == "push_disabled"


def test_read_operations_describe_history_changes_and_state(tmp_path: Path):
    box = make_sandbox(tmp_path)
    record = box.manager.ensure(uuid.uuid4(), "alpha", "demo")
    assert box.adapter.current_branch(record.path) == record.branch
    assert box.adapter.head(record.path).subject == "seed"
    clean = box.adapter.status(record.path)
    assert clean.clean and clean.branch == record.branch and clean.entries == []
    (record.path / "src" / "app.py").write_text("print('changed')\n")
    (record.path / "new.txt").write_text("new\n")
    status = box.adapter.status(record.path)
    assert not status.clean
    assert {(e.path, e.state) for e in status.entries} == {("src/app.py", " M"), ("new.txt", "??")}
    diff = box.adapter.diff(record.path)
    assert [f.path for f in diff.files] == ["src/app.py"]
    assert diff.files[0].additions == 1 and diff.files[0].deletions == 1
    assert "+print('changed')" in diff.patch and not diff.truncated
    small = box.adapter.diff(record.path, max_bytes=16)
    assert small.truncated and len(small.patch.encode()) <= 16
    scoped = box.adapter.diff(record.path, path="README.md")
    assert scoped.files == [] and scoped.patch == ""
    assert [c.subject for c in box.adapter.log(record.path, max_count=5)] == ["seed"]
    assert box.adapter.log(record.path, path="src/app.py")[0].subject == "seed"
    assert box.adapter.branch_exists(box.config, record.branch)
    assert box.adapter.rev_parse(box.config, "main") == box.adapter.head(record.path).sha


def test_bad_refs_and_pathspecs_never_reach_git_and_git_errors_have_codes(tmp_path: Path):
    box = make_sandbox(tmp_path)
    record = box.manager.ensure(uuid.uuid4(), "alpha", "demo")
    with pytest.raises(GitOperationError) as exc:
        box.adapter.log(record.path, ref="--output=/tmp/pwned")
    assert exc.value.code == "ref_invalid" and not Path("/tmp/pwned").exists()
    with pytest.raises(GitOperationError) as exc:
        box.adapter.diff(record.path, path="-R")
    assert exc.value.code == "pathspec_invalid"
    with pytest.raises(GitOperationError) as exc:
        box.adapter.log(record.path, ref="nonexistent")
    assert exc.value.code == "ref_invalid"
    with pytest.raises(GitOperationError) as exc:
        box.adapter.rev_parse(box.config, "ghost")
    assert exc.value.code == "ref_invalid"


def test_timeouts_are_reported_not_hung(tmp_path: Path):
    slow = tmp_path / "slow-git"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(slow.stat().st_mode | stat.S_IXUSR)
    adapter = LocalGitAdapter(binary=str(slow), timeout_seconds=0.2, scratch_home=tmp_path / "h")
    with pytest.raises(GitOperationError) as exc:
        adapter.current_branch(tmp_path)
    assert exc.value.code == "git_timeout"
    with pytest.raises(GitOperationError) as exc:
        LocalGitAdapter(binary=str(tmp_path / "missing"), scratch_home=tmp_path / "h2")
    assert exc.value.code == "git_unavailable"


def test_registry_only_accepts_real_repositories_under_its_root(tmp_path: Path):
    box = make_sandbox(tmp_path)
    outside = seed_repository(tmp_path / "elsewhere", home=box.home)
    with pytest.raises(GitOperationError) as exc:
        box.registry.register(RepositoryConfig(id="outside", project_id="alpha", path=outside))
    assert exc.value.code == "path_outside_root"
    (box.repos_root / "not-a-repo").mkdir()
    with pytest.raises(GitOperationError) as exc:
        box.registry.register(
            RepositoryConfig(id="junk", project_id="alpha", path=box.repos_root / "not-a-repo")
        )
    assert exc.value.code == "repository_invalid"
    with pytest.raises(GitOperationError) as exc:
        box.registry.register(RepositoryConfig(id="demo", project_id="alpha", path=box.config.path))
    assert exc.value.code == "repository_invalid"
    with pytest.raises(GitOperationError) as exc:
        RepositoryRegistry(Path("relative"))
    assert exc.value.code == "path_outside_root"
    # A symlink that points outside the root resolves outside the root.
    link = box.repos_root / "link.git"
    link.symlink_to(outside)
    with pytest.raises(GitOperationError) as exc:
        box.registry.register(RepositoryConfig(id="link", project_id="alpha", path=link))
    assert exc.value.code == "path_outside_root"
    assert box.registry.require("demo", "alpha").id == "demo"
    with pytest.raises(GitOperationError) as exc:
        box.registry.require("demo", "beta")
    assert exc.value.code == "repository_mismatch"
    with pytest.raises(GitOperationError) as exc:
        box.registry.require("ghost", "alpha")
    assert exc.value.code == "repository_unknown"


def test_registry_loads_trusted_configuration_from_a_file(tmp_path: Path):
    box = make_sandbox(tmp_path)
    file = tmp_path / "repositories.json"
    file.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "id": "demo",
                        "project_id": "alpha",
                        "path": str(box.config.path),
                        "protected_refs": ["main", "release/*"],
                        "identity": {"name": "AgentForge Bot", "email": "bot@agentforge.local"},
                    }
                ]
            }
        )
    )
    loaded = RepositoryRegistry.from_file(box.repos_root, file)
    config = loaded.require("demo", "alpha")
    assert config.identity.name == "AgentForge Bot" and config.is_protected("release/2")
    assert config.is_protected("main") and not config.is_protected(f"run/{uuid.uuid4()}")

"""The platform Git publisher: one commit per run, diff facts for the scan, base
checks and a push that can only create or re-assert ``agentforge/<run_id>``
(ADR 0011 §5, §7). The "GitHub" Git endpoint is a local repository over file://."""

import os
import uuid
from pathlib import Path

import pytest

from app.git import GitIdentity, RepositoryConfig, RepositoryRegistry
from app.infrastructure.git.publish import GitPublisher
from app.publishing import PublicationError
from app.workspaces import WorktreeManager
from tests.unit.git_fixtures import git
from tests.unit.test_mirror_service import PROJECT, REPO, Lab

RUN = uuid.UUID("11111111-2222-4333-8444-555555555555")


class Bench:
    """Remote (file://) + mirror + a worktree of one run, as the platform would have them."""

    def __init__(self, tmp_path: Path) -> None:
        self.lab = Lab(tmp_path / "lab")
        self.lab.service.create(REPO, PROJECT)
        self.config = RepositoryConfig(
            id=REPO,
            project_id=PROJECT,
            path=self.lab.mirror_path,
            identity=GitIdentity(name="AgentForge", email="agentforge@noreply.local"),
        )
        registry = RepositoryRegistry(self.lab.root, [self.config])
        self.manager = WorktreeManager(tmp_path / "worktrees", self.lab.adapter, registry)
        self.record = self.manager.ensure(RUN, PROJECT, REPO)
        self.tree = self.record.path
        self.base = self.lab.remote_head()
        self.publisher = GitPublisher(self.lab.adapter, allow_file_remotes=True)
        self.branch = f"agentforge/{RUN}"

    def write(self, relative: str, content: str | bytes) -> None:
        target = self.tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)

    def remote_heads(self) -> dict[str, str]:
        out = git(
            "for-each-ref",
            "--format=%(refname:short) %(objectname)",
            "refs/heads/",
            cwd=self.lab.remote,
            home=self.lab.home,
        )
        return dict(line.split(" ", 1) for line in out.splitlines())


def test_commit_run_branch_makes_exactly_one_commit_and_is_idempotent(tmp_path: Path):
    bench = Bench(tmp_path)
    with pytest.raises(PublicationError) as exc:  # nothing to publish yet
        bench.publisher.commit_run_branch(
            bench.tree, bench.config, base_sha=bench.base, message="x"
        )
    assert exc.value.code == "head_invalid"
    bench.write("src/app.py", "def greet(n):\n    return n\n")
    bench.write("tests/test_greet.py", "def test_ok():\n    assert 1\n")
    head = bench.publisher.commit_run_branch(
        bench.tree, bench.config, base_sha=bench.base, message="[AgentForge] greet\n\nRun-Id: x\n"
    )
    assert head != bench.base and bench.publisher.parent_of(bench.lab.mirror_path, head) == [
        bench.base
    ]
    assert bench.publisher.commits_between(bench.lab.mirror_path, bench.base, head) == 1
    again = bench.publisher.commit_run_branch(
        bench.tree, bench.config, base_sha=bench.base, message="y"
    )
    assert again == head  # already committed: nothing new, same head
    info = bench.lab.adapter.head(bench.tree)
    assert (info.author_name, info.author_email) == ("AgentForge", "agentforge@noreply.local")
    # A worktree with a commit *and* new uncommitted changes is not publishable as is.
    bench.write("src/app.py", "def greet(n):\n    return n * 2\n")
    with pytest.raises(PublicationError) as exc:
        bench.publisher.commit_run_branch(
            bench.tree, bench.config, base_sha=bench.base, message="z"
        )
    assert exc.value.code == "head_invalid"


def test_changed_entries_and_patch_digest_describe_the_diff_for_the_scan(tmp_path: Path):
    bench = Bench(tmp_path)
    bench.write("src/app.py", "print('hi')\nTOKEN = 'ghs_" + "A" * 36 + "'\n")
    bench.write("tests/test_greet.py", "def test_ok():\n    assert 1\n")
    bench.write("bin.dat", b"\x00\x01\x02binary")
    bench.write("run.sh", "#!/bin/sh\necho hi\n")
    os.chmod(bench.tree / "run.sh", 0o755)
    os.symlink("README.md", bench.tree / "link")
    (bench.tree / "README.md").unlink()
    head = bench.publisher.commit_run_branch(
        bench.tree, bench.config, base_sha=bench.base, message="m"
    )
    entries = {
        e.path: e for e in bench.publisher.changed_entries(bench.lab.mirror_path, bench.base, head)
    }
    assert set(entries) == {
        "src/app.py",
        "tests/test_greet.py",
        "bin.dat",
        "run.sh",
        "link",
        "README.md",
    }
    app = entries["src/app.py"]
    assert app.change == "modified" and app.is_binary is False and app.size and app.sha256
    assert any("ghs_" in line for line in app.added_lines)
    assert (
        entries["tests/test_greet.py"].change == "added"
        and entries["tests/test_greet.py"].old_mode is None
    )
    assert entries["bin.dat"].is_binary is True and entries["bin.dat"].added_lines == ()
    assert entries["run.sh"].new_mode == "100755"
    assert entries["link"].new_mode == "120000"
    readme = entries["README.md"]
    assert readme.change == "deleted" and readme.size is None and readme.sha256 is None
    digest = bench.publisher.patch_digest(bench.lab.mirror_path, bench.base, head)
    assert len(digest) == 64
    assert digest == bench.publisher.patch_digest(bench.lab.mirror_path, bench.base, head)
    assert bench.publisher.is_ancestor(bench.lab.mirror_path, bench.base, head)
    assert not bench.publisher.is_ancestor(bench.lab.mirror_path, head, bench.base)


def test_push_creates_only_the_reserved_branch_and_never_overwrites(tmp_path: Path):
    bench = Bench(tmp_path)
    bench.write("src/app.py", "x = 1\n")
    head = bench.publisher.commit_run_branch(
        bench.tree, bench.config, base_sha=bench.base, message="m"
    )
    mirror = bench.lab.mirror_path
    assert bench.publisher.remote_branch_head(mirror, bench.branch, credential_env=None) is None
    bench.publisher.push_run_branch(
        mirror,
        bench.config,
        head_sha=head,
        remote_branch=bench.branch,
        branch_prefix="agentforge/",
        expected_remote_sha=None,
        credential_env=None,
    )
    heads = bench.remote_heads()
    assert heads[bench.branch] == head and heads["main"] == bench.base  # main untouched
    assert bench.publisher.remote_branch_head(mirror, bench.branch, credential_env=None) == head
    # Retry with the lease set to our own head: idempotent.
    bench.publisher.push_run_branch(
        mirror,
        bench.config,
        head_sha=head,
        remote_branch=bench.branch,
        branch_prefix="agentforge/",
        expected_remote_sha=head,
        credential_env=None,
    )
    # A remote branch that already holds exactly our head makes the push a no-op.
    bench.publisher.push_run_branch(
        mirror,
        bench.config,
        head_sha=head,
        remote_branch=bench.branch,
        branch_prefix="agentforge/",
        expected_remote_sha=None,
        credential_env=None,
    )
    assert bench.remote_heads()[bench.branch] == head
    # Someone moved the remote branch meanwhile: a first-attempt push (branch expected
    # absent) and a retry (lease = our head) are both refused; nothing is overwritten.
    git("branch", "-f", bench.branch, bench.base, cwd=bench.lab.remote, home=bench.lab.home)
    with pytest.raises(PublicationError) as exc:
        bench.publisher.push_run_branch(
            mirror,
            bench.config,
            head_sha=head,
            remote_branch=bench.branch,
            branch_prefix="agentforge/",
            expected_remote_sha=None,
            credential_env=None,
        )
    assert exc.value.code == "remote_branch_conflict"
    with pytest.raises(PublicationError) as exc:
        bench.publisher.push_run_branch(
            mirror,
            bench.config,
            head_sha=head,
            remote_branch=bench.branch,
            branch_prefix="agentforge/",
            expected_remote_sha=head,
            credential_env=None,
        )
    assert exc.value.code == "remote_branch_conflict"
    assert bench.remote_heads()[bench.branch] == bench.base


@pytest.mark.parametrize(
    ("branch", "prefix"),
    [
        ("main", "agentforge/"),
        ("master", "agentforge/"),
        ("release/1.0", "agentforge/"),
        (f"run/{RUN}", "agentforge/"),
        (f"feature/{RUN}", "agentforge/"),
        ("agentforge/", "agentforge/"),
        (f"agentforge/{RUN}", "run/"),
        (f"agentforge/{RUN}", "main/"),
    ],
)
def test_pushes_outside_the_reserved_prefix_are_refused_before_git_runs(
    tmp_path: Path, branch: str, prefix: str
):
    bench = Bench(tmp_path)
    bench.write("src/app.py", "x = 1\n")
    head = bench.publisher.commit_run_branch(
        bench.tree, bench.config, base_sha=bench.base, message="m"
    )
    before = bench.remote_heads()
    with pytest.raises((PublicationError, ValueError)):
        bench.publisher.push_run_branch(
            bench.lab.mirror_path,
            bench.config,
            head_sha=head,
            remote_branch=branch,
            branch_prefix=prefix,
            expected_remote_sha=None,
            credential_env=None,
        )
    assert bench.remote_heads() == before  # nothing reached the remote


def test_malformed_shas_and_deletion_refspecs_are_impossible(tmp_path: Path):
    bench = Bench(tmp_path)
    with pytest.raises(PublicationError) as exc:
        bench.publisher.push_run_branch(
            bench.lab.mirror_path,
            bench.config,
            head_sha="not-a-sha",
            remote_branch=bench.branch,
            branch_prefix="agentforge/",
            expected_remote_sha=None,
            credential_env=None,
        )
    assert exc.value.code == "head_invalid"
    with pytest.raises(PublicationError):
        bench.publisher.commits_between(bench.lab.mirror_path, "x", bench.base)

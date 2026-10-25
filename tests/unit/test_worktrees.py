"""``WorktreeManager`` over temporary repositories: lifecycle, isolation, cleanup."""

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.contracts.lifecycle import RunLifecycleStatus
from app.git import GitOperationError
from app.workspaces import WorkspaceError, WorktreeManager, WorktreePolicy
from tests.unit.git_fixtures import fixed_clock, git, make_sandbox

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def test_two_runs_get_independent_worktrees_on_their_own_branches(tmp_path: Path):
    box = make_sandbox(tmp_path)
    a, b = uuid.uuid4(), uuid.uuid4()
    wa = box.manager.ensure(a, "alpha", "demo")
    wb = box.manager.ensure(b, "alpha", "demo")
    assert wa.path != wb.path
    assert wa.run_dir.parent == wb.run_dir.parent == box.worktrees_root / "alpha"
    assert (wa.branch, wb.branch) == (f"run/{a}", f"run/{b}")
    assert (
        wa.path.name == "tree"
        and wa.marker_path.exists()
        and not (wa.path / "worktree.json").exists()
    )
    assert box.adapter.status(wa.path).clean  # the marker is not part of the checkout
    assert (wa.path / "README.md").read_text() == "# Demo\n" == (wb.path / "README.md").read_text()
    (wa.path / "README.md").write_text("changed in a\n")
    assert (wb.path / "README.md").read_text() == "# Demo\n"
    assert box.adapter.current_branch(wa.path) == wa.branch
    assert box.adapter.current_branch(wb.path) == wb.branch
    listed = {w.branch for w in box.adapter.worktree_list(box.config)}
    assert {wa.branch, wb.branch} <= listed
    assert box.manager.count("alpha") == 2 and box.manager.count("beta") == 0


def test_a_restarted_process_reuses_the_existing_worktree(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    first = box.manager.ensure(run_id, "alpha", "demo")
    (first.path / "work.txt").write_text("in progress\n")
    again = box.manager.ensure(run_id, "alpha", "demo")
    assert again == first  # idempotent inside the same process
    restarted = box.new_manager()
    after = restarted.ensure(run_id, "alpha", "demo")
    assert (after.path, after.branch, after.created_at) == (
        first.path,
        first.branch,
        first.created_at,
    )
    assert (after.path / "work.txt").read_text() == "in progress\n"
    assert restarted.get(run_id, "alpha") == after
    assert len([w for w in box.adapter.worktree_list(box.config) if w.branch == first.branch]) == 1


def test_paths_outside_the_storage_are_refused(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    assert box.manager.record_at(tmp_path / "elsewhere") is None
    assert box.manager.record_at(box.worktrees_root) is None
    assert box.manager.record_at(box.worktrees_root / "alpha") is None
    assert box.manager.record_at(record.path / "src") is None
    assert box.manager.record_at(record.path) == record == box.manager.record_at(record.run_dir)
    for bad in ("../alpha", "alpha/..", "/abs", "Alpha", "a b", "", ".."):
        with pytest.raises(WorkspaceError) as exc:
            box.manager.path_for(bad, run_id)
        assert exc.value.code == "invalid_identifier", bad
    # A run directory replaced by a symlink is neither trusted nor removed.
    other = tmp_path / "victim"
    other.mkdir()
    (other / "keep.txt").write_text("keep")
    planted = uuid.uuid4()
    (box.worktrees_root / "alpha" / str(planted)).symlink_to(other)
    assert box.manager.get(planted, "alpha") is None
    with pytest.raises(WorkspaceError) as exc:
        box.manager.remove(planted, "alpha")
    assert exc.value.code == "path_outside_storage"
    assert (other / "keep.txt").exists()
    with pytest.raises(WorkspaceError) as exc:
        box.manager.ensure(planted, "alpha", "demo")
    assert exc.value.code == "path_outside_storage"
    # Worktree storage and repository storage may not nest.
    with pytest.raises(WorkspaceError) as exc:
        WorktreeManager(box.repos_root / "wt", box.adapter, box.registry)
    assert exc.value.code == "storage_invalid"


def test_projects_cannot_see_or_reuse_each_other(tmp_path: Path):
    box = make_sandbox(tmp_path)
    run_id = uuid.uuid4()
    record = box.manager.ensure(run_id, "alpha", "demo")
    with pytest.raises(GitOperationError) as exc:
        box.manager.ensure(uuid.uuid4(), "beta", "demo")  # demo belongs to alpha
    assert exc.value.code == "repository_mismatch"
    assert box.manager.get(run_id, "beta") is None
    # A marker copied into another project's slot does not make it that project's.
    foreign = box.worktrees_root / "beta" / str(run_id)
    foreign.mkdir(parents=True)
    (foreign / record.marker_path.name).write_text(record.marker_path.read_text())
    assert box.manager.record_at(foreign) is None and box.manager.get(run_id, "beta") is None
    # Nor does a marker that claims another run id.
    data = json.loads(record.marker_path.read_text())
    data["run_id"] = str(uuid.uuid4())
    record.marker_path.write_text(json.dumps(data))
    assert box.manager.get(run_id, "alpha") is None


def test_partial_creations_are_detected_and_rebuilt(tmp_path: Path):
    box = make_sandbox(tmp_path)
    # (a) a directory with junk and no marker: the crash happened before git ran
    run_a = uuid.uuid4()
    junk = box.manager.path_for("alpha", run_a)
    junk.mkdir(parents=True)
    (junk / "half.txt").write_text("partial")
    rebuilt = box.manager.ensure(run_a, "alpha", "demo")
    assert not (rebuilt.path / "half.txt").exists() and (rebuilt.path / "README.md").exists()
    assert box.adapter.current_branch(rebuilt.path) == rebuilt.branch
    # (b) git created the worktree but the marker was never written
    run_b = uuid.uuid4()
    first = box.manager.ensure(run_b, "alpha", "demo")
    first.marker_path.unlink()
    assert box.manager.get(run_b, "alpha") is None
    second = box.manager.ensure(run_b, "alpha", "demo")
    assert second.path == first.path and second.branch == first.branch
    assert box.adapter.current_branch(second.path) == second.branch
    assert len([w for w in box.adapter.worktree_list(box.config) if w.branch == second.branch]) == 1
    # (c) marker present but the checkout is gone: rebuilt as well
    (second.path / ".git").unlink()
    third = box.manager.ensure(run_b, "alpha", "demo")
    assert (third.path / ".git").is_file() and third.branch == second.branch
    # (d) a marker that names another repository is a corruption, not a silent switch
    data = json.loads(third.marker_path.read_text())
    data["repository_id"] = "other"
    third.marker_path.write_text(json.dumps(data))
    with pytest.raises(WorkspaceError) as exc:
        box.manager.ensure(run_b, "alpha", "demo")
    assert exc.value.code == "worktree_corrupt"


def test_release_and_retention_remove_only_what_expired(tmp_path: Path):
    policy = WorktreePolicy(retention_seconds=60, interrupted_retention_seconds=600)
    box = make_sandbox(tmp_path, policy=policy, clock=fixed_clock(T0))
    done, interrupted, alive = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    w_done = box.manager.ensure(done, "alpha", "demo")
    w_int = box.manager.ensure(interrupted, "alpha", "demo")
    w_alive = box.manager.ensure(alive, "alpha", "demo")
    (w_alive.path / "precious.txt").write_text("alive\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "host.txt").write_text("host\n")
    # Links planted inside the expiring worktree must not drag other data along.
    (w_done.path / "to_alive").symlink_to(w_alive.path)
    (w_done.path / "to_outside").symlink_to(outside)
    box.manager.release(done, "alpha", status=RunLifecycleStatus.APPROVED)
    box.manager.release(interrupted, "alpha", status=RunLifecycleStatus.INTERRUPTED)
    assert box.manager.get(done, "alpha").release_status == "approved"  # type: ignore[union-attr]

    early = box.manager.sweep(now=T0 + timedelta(seconds=30))
    assert early.removed == [] and len(early.retained) == 3

    later = box.manager.sweep(now=T0 + timedelta(seconds=61))
    assert later.removed == [w_done.run_dir] and w_int.run_dir in later.retained
    assert not w_done.path.exists()
    assert (w_alive.path / "precious.txt").read_text() == "alive\n"
    assert (outside / "host.txt").read_text() == "host\n"
    assert {w.branch for w in box.adapter.worktree_list(box.config)} >= {
        w_int.branch,
        w_alive.branch,
    }
    assert all(w.branch != w_done.branch for w in box.adapter.worktree_list(box.config))
    assert box.adapter.branch_exists(box.config, w_done.branch)  # history is kept

    much_later = box.manager.sweep(now=T0 + timedelta(seconds=601))
    assert much_later.removed == [w_int.run_dir] and w_alive.run_dir in much_later.retained
    assert w_alive.path.exists()


def test_orphans_and_quotas(tmp_path: Path):
    policy = WorktreePolicy(retention_seconds=60, max_per_project=2)
    box = make_sandbox(tmp_path, policy=policy, clock=fixed_clock(T0))
    box.manager.ensure(uuid.uuid4(), "alpha", "demo")
    stale = box.manager.path_for("alpha", uuid.uuid4())
    stale.mkdir(parents=True)
    old = (T0 - timedelta(seconds=120)).timestamp()
    os.utime(stale, (old, old))
    with pytest.raises(WorkspaceError) as exc:  # the stale directory counts against the quota
        box.manager.ensure(uuid.uuid4(), "alpha", "demo")
    assert exc.value.code == "worktree_quota_exceeded"
    report = box.manager.sweep(now=T0)
    assert report.orphans_removed == [stale] and not stale.exists()
    box.manager.ensure(uuid.uuid4(), "alpha", "demo")  # room again
    hungry = box.new_manager(policy=WorktreePolicy(min_free_bytes=1 << 60))
    with pytest.raises(WorkspaceError) as exc:
        hungry.ensure(uuid.uuid4(), "alpha", "demo")
    assert exc.value.code == "disk_space_low"


def test_remove_is_explicit_and_scoped(tmp_path: Path):
    box = make_sandbox(tmp_path)
    keep, drop = uuid.uuid4(), uuid.uuid4()
    w_keep = box.manager.ensure(keep, "alpha", "demo")
    w_drop = box.manager.ensure(drop, "alpha", "demo")
    assert box.manager.remove(drop, "alpha") is True
    assert box.manager.remove(drop, "alpha") is False
    assert not w_drop.path.exists() and w_keep.path.exists()
    assert git("worktree", "list", cwd=str(box.config.path), home=box.home).count("run/") == 1
    with pytest.raises(WorkspaceError):
        box.manager.release(drop, "alpha", status=RunLifecycleStatus.FAILED)

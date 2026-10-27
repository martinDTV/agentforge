"""The independent digest recomputation a human runs before approving publication."""

import json
import uuid
from pathlib import Path

from app.publishing import PublicationFile, PublicationPlan
from scripts.publication_digest import recompute
from tests.unit.test_git_publish import RUN, Bench
from tests.unit.test_mirror_service import policy_for


def test_recompute_agrees_with_the_shown_plan_and_detects_tampering(tmp_path: Path):
    bench = Bench(tmp_path)
    bench.write("src/app.py", "def greet(n):\n    return n\n")
    bench.write("tests/test_greet.py", "def test_ok():\n    assert 1\n")
    head = bench.publisher.commit_run_branch(
        bench.tree, bench.config, base_sha=bench.base, message="m"
    )
    mirror = bench.lab.mirror_path
    policy = policy_for()
    entries = bench.publisher.changed_entries(mirror, bench.base, head)
    files = [
        PublicationFile(
            path=e.path,
            change=e.change,
            sha256=e.sha256,
            size=e.size or 0,
            mode=e.new_mode or "100644",
        )
        for e in entries
    ]
    from app.publishing import scan_changes

    scan = scan_changes(
        entries, approved={"src/app.py": "edit", "tests/test_greet.py": "create"}, policy=policy
    )
    plan = PublicationPlan(
        run_id=RUN,
        repository_id=policy.repository_id,
        project_id=policy.project_id,
        owner=policy.remote.owner,  # type: ignore[union-attr]
        name=policy.remote.name,  # type: ignore[union-attr]
        github_repository_id=policy.remote.github_repository_id,  # type: ignore[union-attr]
        installation_id=policy.remote.installation_id,  # type: ignore[union-attr]
        base_branch="main",
        base_sha=bench.base,
        base_branch_head=bench.base,
        remote_branch=f"agentforge/{RUN}",
        head_sha=head,
        patch_digest=bench.publisher.patch_digest(mirror, bench.base, head),
        files=files,
        scope_digest="4" * 64,
        scan_digest=scan.digest(),
        title="[AgentForge] greet",
        prepared_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    shown = {
        **json.loads(plan.model_dump_json()),
        "repository": plan.full_name,
        "publish_digest": plan.digest(),
    }
    report = recompute(
        shown,
        git=bench.publisher,
        mirror=mirror,
        policy=policy,
        approved_scope={
            "digest": "4" * 64,
            "files": [
                {"path": "src/app.py", "action": "edit"},
                {"path": "tests/test_greet.py", "action": "create"},
            ],
        },
    )
    assert report["matches"] and report["recomputed_digest"] == plan.digest()
    assert report["head_matches"] and report["patch_matches"] and report["files_match"]
    # A different digest shown to the human (tampered proposal) is caught.
    tampered = {**shown, "publish_digest": "f" * 64}
    assert (
        recompute(tampered, git=bench.publisher, mirror=mirror, policy=policy)["matches"] is False
    )
    # Content changed after the plan was shown: head no longer matches.
    bench.write("src/app.py", "def greet(n):\n    return 'x'\n")
    from tests.unit.git_fixtures import git

    git("commit", "-q", "--amend", "--no-edit", "-a", cwd=bench.tree, home=bench.lab.home)
    moved = recompute(shown, git=bench.publisher, mirror=mirror, policy=policy)
    assert moved["head_matches"] is False and moved["matches"] is False
    assert uuid.UUID(moved["run_id"]) == RUN

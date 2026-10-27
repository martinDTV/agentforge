"""Recompute a publication digest independently of the Publisher (ADR 0011 §3).

Given the pending ``publish`` proposal as shown by ``GET /runs/{id}/decisions``
(saved to a JSON file) and the local mirror, this script re-derives the facts
the digest covers straight from Git (head of ``run/<run_id>``, patch digest,
files with their hashes) and rebuilds the plan, so a human can confirm that
the digest they are about to approve corresponds exactly to that content and
destination. Read-only; no token, no network.

    uv run python -m scripts.publication_digest --proposal pending.json \\
        --repository sandbox-github --project sandbox-github
"""

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from app.git import RepositoryRegistry
from app.git.refs import run_branch
from app.infrastructure.git import LocalGitAdapter
from app.infrastructure.git.publish import GitPublisher
from app.projects import ProjectPolicyRegistry, RepositoryPolicy
from app.publishing import PublicationFile, PublicationPlan, scan_changes


def recompute(
    proposal: dict[str, Any],
    *,
    git: GitPublisher,
    mirror: Path,
    policy: RepositoryPolicy,
    approved_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Facts from the mirror versus the proposal; ``matches`` is the verdict."""
    run_id = UUID(str(proposal["run_id"]))
    base_sha = str(proposal["base_sha"])
    shown_head = str(proposal["head_sha"])
    local_head = git.head_of(mirror, run_branch(run_id))
    report: dict[str, Any] = {
        "run_id": str(run_id),
        "shown_digest": proposal.get("publish_digest") or proposal.get("digest"),
        "shown_head_sha": shown_head,
        "local_head_sha": local_head,
        "head_matches": local_head == shown_head,
    }
    if local_head is None:
        report["matches"] = False
        return report
    parents = git.parent_of(mirror, local_head)
    report["parent_is_base"] = parents == [base_sha]
    entries = git.changed_entries(mirror, base_sha, local_head)
    files = [
        PublicationFile(
            path=e.path,
            change=e.change,
            sha256=e.sha256,
            size=e.size or 0,
            mode=e.new_mode or e.old_mode or "100644",
        )
        for e in entries
    ]
    shown_files = {f["path"]: f for f in proposal.get("files", [])}
    report["files_match"] = {f.path: (f.sha256, f.change) for f in files} == {
        p: (f.get("sha256"), f.get("change")) for p, f in shown_files.items()
    }
    patch = git.patch_digest(mirror, base_sha, local_head)
    report["patch_digest"] = patch
    report["patch_matches"] = patch == proposal.get("patch_digest")
    approved = approved_scope or {}
    actions = {f["path"]: f["action"] for f in approved.get("files", [])} or {
        p: ("create" if f.get("change") == "added" else "edit") for p, f in shown_files.items()
    }
    scan = scan_changes(entries, approved=actions, policy=policy)
    report["scan_blocking"] = scan.blocking
    report["scan_codes"] = scan.codes
    remote = policy.remote
    if remote is None:
        raise ValueError("la política no tiene remoto")
    rebuilt = PublicationPlan(
        run_id=run_id,
        repository_id=policy.repository_id,
        project_id=policy.project_id,
        owner=str(proposal["repository"]).split("/", 1)[0],
        name=str(proposal["repository"]).split("/", 1)[1],
        github_repository_id=int(
            proposal.get("github_repository_id") or remote.github_repository_id
        ),
        installation_id=int(proposal.get("installation_id") or remote.installation_id),
        base_branch=str(proposal["base_branch"]),
        base_sha=base_sha,
        base_branch_head=str(proposal.get("base_branch_head") or base_sha),
        remote_branch=str(proposal["remote_branch"]),
        head_sha=local_head,
        patch_digest=patch,
        files=files,
        scope_digest=str(proposal.get("scope_digest") or approved.get("digest") or "0" * 64),
        scan_digest=scan.digest(),
        title=str(proposal["title"]),
        prepared_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    report["recomputed_digest"] = rebuilt.digest()
    report["digest_matches"] = rebuilt.digest() == report["shown_digest"]
    report["matches"] = bool(
        report["head_matches"]
        and report["parent_is_base"]
        and report["files_match"]
        and report["patch_matches"]
        and not scan.blocking
        and report["digest_matches"]
    )
    report["proposal_sha256"] = hashlib.sha256(
        json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--proposal", required=True, help="JSON with the pending publish proposal")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--approved-scope", help="optional JSON with the approved scope (files, digest)"
    )
    args = parser.parse_args(argv)
    from app.settings import get_settings

    settings = get_settings()
    if settings.git_repositories_file is None or settings.git_repositories_root is None:
        print(json.dumps({"refused": "git_unconfigured"}), file=sys.stderr)
        return 2
    assert settings.projects_file is not None
    policy = ProjectPolicyRegistry.from_file(settings.projects_file).require(
        args.repository, args.project
    )
    config = RepositoryRegistry.from_file(
        settings.git_repositories_root, settings.git_repositories_file
    ).require(args.repository, args.project)
    proposal = json.loads(Path(args.proposal).read_text())
    approved = json.loads(Path(args.approved_scope).read_text()) if args.approved_scope else None
    git = GitPublisher(LocalGitAdapter(binary=settings.git_binary))
    report = recompute(
        proposal, git=git, mirror=config.path, policy=policy, approved_scope=approved
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("matches") else 1


if __name__ == "__main__":
    sys.exit(main())

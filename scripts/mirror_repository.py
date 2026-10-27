"""Operator tool: create, refresh or check the local mirror of a GitHub repository.

Runs on the platform host with the platform's configuration (``Settings``):
the GitHub App private key, the repositories file and the projects file.
It never prints a token; the JSON summary carries identities, the base commit
and the audit outcome. Exit codes: 0 ok, 1 refused or failed, 2 misuse.

    uv run python -m scripts.mirror_repository --repository sandbox-demo \\
        --project sandbox --create
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.git import RepositoryRegistry
from app.git.mirrors import MirrorError, MirrorSyncRepository
from app.infrastructure.db import Database
from app.infrastructure.db.mirror_syncs import SqlAlchemyMirrorSyncRepository
from app.infrastructure.git import LocalGitAdapter
from app.infrastructure.git.mirror import MirrorOutcome, MirrorService, audited
from app.infrastructure.github import (
    GitHubAppCredentials,
    GitHubClient,
    GitHubError,
    InstallationTokenProvider,
)
from app.projects import PolicyError, ProjectPolicyRegistry
from app.settings import Settings, get_settings

Operation = Callable[[str, str], MirrorOutcome]


@dataclass
class MirrorDeps:
    service: MirrorService
    syncs: MirrorSyncRepository
    out: Callable[[str], None] = lambda line: print(line)  # noqa: E731


class MirrorRefused(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def deps_from_settings(settings: Settings) -> MirrorDeps:
    if settings.github_app_id is None:
        raise MirrorRefused(
            "github_unconfigured", "define GITHUB_APP_ID y GITHUB_APP_PRIVATE_KEY_FILE"
        )
    if (
        settings.git_repositories_file is None
        or settings.git_repositories_root is None
        or settings.projects_file is None
    ):
        raise MirrorRefused(
            "git_unconfigured",
            "define GIT_REPOSITORIES_FILE, GIT_REPOSITORIES_ROOT y PROJECTS_FILE",
        )
    try:
        credentials = GitHubAppCredentials.from_settings(settings)
    except GitHubError as exc:
        raise MirrorRefused(exc.code, exc.detail) from exc
    assert credentials is not None
    github = GitHubClient(settings.github_api_url, timeout_seconds=settings.github_timeout_seconds)
    service = MirrorService(
        git=LocalGitAdapter(
            binary=settings.git_binary, timeout_seconds=settings.git_timeout_seconds
        ),
        repositories_root=settings.git_repositories_root,
        policies=ProjectPolicyRegistry.from_file(settings.projects_file),
        github=github,
        tokens=InstallationTokenProvider(github, credentials),
        registry=RepositoryRegistry.from_file(
            settings.git_repositories_root, settings.git_repositories_file
        ),
        fetch_timeout_seconds=settings.mirror_fetch_timeout_seconds,
    )
    database = Database.from_settings(settings)
    return MirrorDeps(
        service=service, syncs=SqlAlchemyMirrorSyncRepository(database.session_factory)
    )


async def run(
    deps: MirrorDeps, *, operation: str, repository_id: str, project_id: str
) -> dict[str, Any]:
    ops: dict[str, Operation] = {
        "create": deps.service.create,
        "refresh": deps.service.refresh,
        "check": deps.service.check,
    }
    if operation not in ops:
        raise MirrorRefused("operation", f"operación desconocida: {operation}")
    action = ops[operation]
    try:
        outcome = await audited(deps.syncs, lambda: action(repository_id, project_id))
    except (MirrorError, PolicyError) as exc:
        return {
            "operation": operation,
            "repository_id": repository_id,
            "project_id": project_id,
            "result": "failed",
            "code": exc.code,
            "detail": exc.detail,
        }
    record = outcome.record
    return {
        "operation": operation,
        "repository_id": repository_id,
        "project_id": project_id,
        "result": outcome.sync.result,
        "repository": f"{record.owner}/{record.name}",
        "github_repository_id": record.github_repository_id,
        "installation_id": record.installation_id,
        "default_branch": record.default_branch,
        "base_sha": record.base_sha,
        "previous_sha": outcome.sync.previous_sha,
        "changed": outcome.changed,
        "path": str(record.path),
        "last_synced_at": record.last_synced_at.isoformat() if record.last_synced_at else None,
        "duration_ms": outcome.sync.duration_ms,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repository", required=True, help="repository_id de projects.json")
    parser.add_argument("--project", required=True, help="project_id de la política")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_const", const="create", dest="operation")
    group.add_argument("--refresh", action="store_const", const="refresh", dest="operation")
    group.add_argument("--check", action="store_const", const="check", dest="operation")
    args = parser.parse_args(argv)
    try:
        deps = deps_from_settings(get_settings())
    except MirrorRefused as exc:
        print(json.dumps({"refused": exc.code, "detail": exc.detail}), file=sys.stderr)
        return 2
    summary = asyncio.run(
        run(deps, operation=args.operation, repository_id=args.repository, project_id=args.project)
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["result"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

"""Preflight of the real GitHub integration (Fase 7, Paso 2; ADR 0010). Read-only.

Checks, without creating or changing anything, that the platform host, the
GitHub App, its installation, the repository and the local mirror all agree
with the operator policy:

- Settings and the private key file (absolute path, outside every root, regular
  file, mode 0600, owned by this user; the key is never read into the report);
- App authentication (JWT) and the installation: restricted to selected
  repositories, read-only ``contents`` + ``metadata`` and nothing else, not suspended;
- a short-lived installation token narrowed to the repository;
- the repository identity: numeric id, ``owner/name``, default branch, private,
  not archived, reachable by the installation;
- the remote head of the default branch;
- the local mirror (when it exists): marker identity, ``origin`` URL, local head
  versus marker ``base_sha`` versus remote head;
- a leak scan: no ``ghs_`` token under the Git roots, in the marker, in the
  mirror's config or (with ``--scan-db``) in events, audit and checkpoints.

Prints one JSON report; exit 0 when every check passed, 1 otherwise, 2 on misuse.

    uv run python -m scripts.github_preflight --repository sandbox-demo --project sandbox-github
"""

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.git import RepositoryRegistry
from app.git.mirrors import MirrorError
from app.git.remote import RemoteSpec
from app.infrastructure.git import LocalGitAdapter
from app.infrastructure.git.mirror import MirrorService
from app.infrastructure.github import (
    READ_ONLY_PERMISSIONS,
    GitHubAppCredentials,
    GitHubClient,
    GitHubError,
    InstallationTokenProvider,
    verify_identity,
)
from app.projects import PolicyError, ProjectPolicyRegistry

TOKEN_PATTERN = re.compile(rb"ghs_[A-Za-z0-9]{30,}")
FORBIDDEN_PERMISSIONS = frozenset(
    {"workflows", "administration", "secrets", "actions", "members", "organization_administration"}
)
MAX_SCAN_FILE_BYTES = 8 * 1024 * 1024


@dataclass
class Check:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.name, "ok": self.ok, **self.detail}


@dataclass
class PreflightDeps:
    policies: ProjectPolicyRegistry
    registry: RepositoryRegistry | None
    credentials: GitHubAppCredentials | None
    client: GitHubClient | None
    tokens: InstallationTokenProvider | None
    service: MirrorService
    key_file: Path | None
    scan_roots: list[Path] = field(default_factory=list)
    scan_database: Callable[[], list[dict[str, Any]]] | None = None
    credentials_error: str | None = None


class PreflightRefused(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _key_file_check(path: Path | None, error: str | None) -> Check:
    if path is None:
        return Check("private_key", False, {"error": "GITHUB_APP_PRIVATE_KEY_FILE no definido"})
    try:
        info = path.lstat()
    except OSError as exc:
        return Check("private_key", False, {"path": str(path), "error": type(exc).__name__})
    detail = {
        "path": str(path),
        "regular_file": stat.S_ISREG(info.st_mode),
        "mode": oct(stat.S_IMODE(info.st_mode)),
        "owned_by_process": info.st_uid == os.getuid(),
        "size_bytes": info.st_size,
    }
    ok = (
        error is None
        and bool(detail["regular_file"])
        and detail["mode"] == "0o600"
        and bool(detail["owned_by_process"])
    )
    if error is not None:
        detail["error"] = error
    return Check("private_key", ok, detail)


def _scan_files(roots: Iterable[Path]) -> list[str]:
    hits: dict[str, None] = {}  # ordered, without duplicates when roots overlap
    for root in roots:
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                if path.stat().st_size > MAX_SCAN_FILE_BYTES:
                    continue
                if TOKEN_PATTERN.search(path.read_bytes()):
                    hits[str(path)] = None
            except OSError:
                continue
    return list(hits)


def run_preflight(deps: PreflightDeps, *, repository_id: str, project_id: str) -> dict[str, Any]:
    checks: list[Check] = []
    checks.append(_key_file_check(deps.key_file, deps.credentials_error))

    try:
        policy = deps.policies.require(repository_id, project_id)
    except PolicyError as exc:
        checks.append(Check("policy", False, {"error": exc.code}))
        return _report(repository_id, project_id, checks)
    spec: RemoteSpec | None = policy.remote
    if spec is None:
        checks.append(Check("policy", False, {"error": "mirror_not_remote"}))
        return _report(repository_id, project_id, checks)
    checks.append(
        Check(
            "policy",
            True,
            {
                "repository": spec.full_name,
                "github_repository_id": spec.github_repository_id,
                "installation_id": spec.installation_id,
                "default_branch": spec.default_branch,
                "clone_url": spec.clone_url,
                "writable_globs": list(policy.writable_globs),
            },
        )
    )

    mirror_path = deps.service.path_for(spec, project_id)
    if deps.registry is not None:
        config = deps.registry.get(repository_id)
        if config is None:
            checks.append(
                Check("registry", True, {"registered": False, "expected_path": str(mirror_path)})
            )
        else:
            same = config.path == mirror_path and config.default_branch == spec.default_branch
            checks.append(
                Check(
                    "registry",
                    same,
                    {
                        "registered": True,
                        "path": str(config.path),
                        "expected_path": str(mirror_path),
                        "default_branch": config.default_branch,
                    },
                )
            )

    if deps.credentials is None or deps.client is None or deps.tokens is None:
        checks.append(Check("app_auth", False, {"error": "github_unconfigured"}))
        return _report(repository_id, project_id, checks)

    remote_head: str | None = None
    try:
        info = deps.client.installation(deps.credentials.app_jwt(), spec.installation_id)
        unexpected = {
            k: v for k, v in info.permissions.items() if READ_ONLY_PERMISSIONS.get(k) != v
        }
        missing = sorted(
            k for k, v in READ_ONLY_PERMISSIONS.items() if info.permissions.get(k) != v
        )
        forbidden = sorted(set(info.permissions) & FORBIDDEN_PERMISSIONS)
        checks.append(
            Check(
                "app_auth",
                info.app_id == deps.credentials.app_id,
                {"app_id": info.app_id, "configured_app_id": deps.credentials.app_id},
            )
        )
        checks.append(
            Check(
                "installation_restricted",
                info.repository_selection == "selected"
                and not unexpected
                and not missing
                and not forbidden
                and not info.suspended,
                {
                    "installation_id": info.id,
                    "account": info.account,
                    "repository_selection": info.repository_selection,
                    "permissions": info.permissions,
                    "missing_permissions": missing,
                    "unexpected_permissions": unexpected,
                    "forbidden_permissions": forbidden,
                    "suspended": info.suspended,
                },
            )
        )
        token = deps.tokens.token_for(spec.installation_id, spec.name)
        checks.append(
            Check(
                "installation_token",
                True,
                {
                    "repository": token.repository,
                    "permissions": token.permissions,
                    "expires_at": token.expires_at.isoformat(),
                },
            )
        )
        observed = deps.client.repository(token.reveal(), spec.owner, spec.name)
        reachable = [r.github_id for r in deps.client.installation_repositories(token.reveal())]
        identity_detail = {
            "observed_id": observed.github_id,
            "observed_full_name": observed.full_name,
            "observed_default_branch": observed.default_branch,
            "private": observed.private,
            "archived": observed.archived,
            "reachable_by_installation": observed.github_id in reachable,
        }
        try:
            verify_identity(spec, observed, installation_repository_ids=reachable)
        except GitHubError as exc:
            identity_detail["error"] = exc.code
            checks.append(Check("repository_identity", False, identity_detail))
        else:
            checks.append(Check("repository_identity", observed.private, identity_detail))
        remote_head = deps.client.branch_head(
            token.reveal(), spec.owner, spec.name, spec.default_branch
        )
        checks.append(
            Check("remote_head", True, {"branch": spec.default_branch, "sha": remote_head})
        )
    except GitHubError as exc:
        checks.append(Check("github", False, {"error": exc.code, "detail": exc.detail}))

    checks.append(_mirror_check(deps, spec, project_id, mirror_path, remote_head))
    roots = list(deps.scan_roots)
    roots.append(mirror_path.parent)
    hits = _scan_files(roots)
    leak: dict[str, Any] = {"scanned": [str(r) for r in roots], "files_with_tokens": hits}
    if deps.scan_database is not None:
        rows = deps.scan_database()
        leak["database_rows_with_tokens"] = rows
        hits = hits + [str(r) for r in rows]
    checks.append(Check("no_token_leaks", not hits, leak))
    return _report(repository_id, project_id, checks)


def _mirror_check(
    deps: PreflightDeps, spec: RemoteSpec, project_id: str, path: Path, remote_head: str | None
) -> Check:
    try:
        record = deps.service.record(_repository_id_for(deps, spec, project_id), project_id)
    except (MirrorError, PolicyError) as exc:
        return Check("mirror", False, {"path": str(path), "error": exc.code})
    if record is None:
        return Check(
            "mirror", True, {"path": str(path), "exists": False, "note": "not created yet"}
        )
    detail: dict[str, Any] = {
        "path": str(path),
        "exists": True,
        "marker_base_sha": record.base_sha,
        "remote_head": remote_head,
        "last_synced_at": record.last_synced_at.isoformat() if record.last_synced_at else None,
    }
    try:
        outcome = deps.service.check(record.repository_id, project_id)
    except MirrorError as exc:
        detail["error"] = exc.code
        return Check("mirror", False, detail)
    detail["local_head"] = outcome.record.base_sha
    detail["in_sync_with_remote"] = remote_head is None or outcome.record.base_sha == remote_head
    config = path / "config"
    if config.is_file():
        text = config.read_text(errors="ignore")
        detail["origin_url_has_credentials"] = "@" in text.split("url = ", 1)[-1].split("\n", 1)[0]
    return Check("mirror", not detail.get("origin_url_has_credentials", False), detail)


def _repository_id_for(deps: PreflightDeps, spec: RemoteSpec, project_id: str) -> str:
    for repository_id in deps.policies.ids():
        policy = deps.policies.get(repository_id)
        if policy is not None and policy.project_id == project_id and policy.remote == spec:
            return repository_id
    raise PolicyError("repository_not_authorized", "política no encontrada")


def _report(repository_id: str, project_id: str, checks: list[Check]) -> dict[str, Any]:
    return {
        "repository_id": repository_id,
        "project_id": project_id,
        "ok": all(c.ok for c in checks),
        "checks": [c.as_dict() for c in checks],
    }


def deps_from_settings(*, scan_db: bool, extra_scan: Sequence[str]) -> PreflightDeps:
    from app.settings import get_settings

    settings = get_settings()
    if (
        settings.git_repositories_file is None
        or settings.git_repositories_root is None
        or settings.projects_file is None
    ):
        raise PreflightRefused(
            "git_unconfigured",
            "define GIT_REPOSITORIES_FILE, GIT_REPOSITORIES_ROOT y PROJECTS_FILE",
        )
    policies = ProjectPolicyRegistry.from_file(settings.projects_file)
    registry = RepositoryRegistry.from_file(
        settings.git_repositories_root, settings.git_repositories_file
    )
    credentials: GitHubAppCredentials | None = None
    error: str | None = None
    try:
        credentials = GitHubAppCredentials.from_settings(settings)
    except GitHubError as exc:
        error = exc.detail
    client = tokens = None
    if credentials is not None:
        client = GitHubClient(
            settings.github_api_url, timeout_seconds=settings.github_timeout_seconds
        )
        tokens = InstallationTokenProvider(client, credentials)
    service = MirrorService(
        git=LocalGitAdapter(
            binary=settings.git_binary, timeout_seconds=settings.git_timeout_seconds
        ),
        repositories_root=settings.git_repositories_root,
        policies=policies,
        github=client,
        tokens=tokens,
        registry=registry,
        fetch_timeout_seconds=settings.mirror_fetch_timeout_seconds,
    )
    roots = [settings.git_repositories_root]
    if settings.worktrees_root is not None:
        roots.append(settings.worktrees_root)
    roots.extend(Path(p) for p in extra_scan)
    scanner = (lambda: scan_database(settings.database_dsn.get_secret_value())) if scan_db else None
    return PreflightDeps(
        policies=policies,
        registry=registry,
        credentials=credentials,
        client=client,
        tokens=tokens,
        service=service,
        key_file=settings.github_app_private_key_file,
        scan_roots=roots,
        scan_database=scanner,
        credentials_error=error,
    )


def scan_database(dsn: str) -> list[dict[str, Any]]:
    """Rows of the business and checkpoint tables that contain a ``ghs_`` token."""
    import psycopg

    token = "'ghs_[A-Za-z0-9]{30,}'"
    queries = {
        "run_events": f"SELECT id::text FROM run_events WHERE payload::text ~ {token}",
        "mirror_syncs": (
            "SELECT id::text FROM mirror_syncs WHERE "
            f"concat_ws(' ', repository_id, project_id, code, actor_subject) ~ {token}"
        ),
        "tool_invocations": (
            "SELECT id::text FROM tool_invocations WHERE "
            f"concat_ws(' ', tool_name, step, error_code) ~ {token}"
        ),
        "runs": (
            "SELECT id::text FROM runs WHERE concat_ws(' ', task, plan::text, "
            f"architecture::text, error_message) ~ {token}"
        ),
        "checkpoints": (
            "SELECT thread_id FROM checkpoints WHERE "
            f"concat(checkpoint::text, metadata::text) ~ {token}"
        ),
        "checkpoint_blobs": (
            f"SELECT thread_id FROM checkpoint_blobs WHERE encode(blob, 'escape') ~ {token}"
        ),
        "checkpoint_writes": (
            f"SELECT thread_id FROM checkpoint_writes WHERE encode(blob, 'escape') ~ {token}"
        ),
    }
    hits: list[dict[str, Any]] = []
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        for table, sql in queries.items():
            try:
                rows = conn.execute(sql).fetchall()
            except psycopg.Error:
                conn.rollback()
                hits.append({"table": table, "error": "query_failed"})
                continue
            for (identifier,) in rows:
                hits.append({"table": table, "id": str(identifier)})
    return hits


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repository", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--scan-db", action="store_true", help="also scan PostgreSQL for tokens")
    parser.add_argument(
        "--scan", action="append", default=[], help="extra file or directory to scan"
    )
    args = parser.parse_args(argv)
    try:
        deps = deps_from_settings(scan_db=args.scan_db, extra_scan=args.scan)
    except PreflightRefused as exc:
        print(json.dumps({"refused": exc.code, "detail": exc.detail}), file=sys.stderr)
        return 2
    except ValueError as exc:  # Settings validation
        print(json.dumps({"refused": "settings_invalid", "detail": str(exc)}), file=sys.stderr)
        return 2
    report = run_preflight(deps, repository_id=args.repository, project_id=args.project)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

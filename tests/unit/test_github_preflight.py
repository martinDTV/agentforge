"""The read-only preflight for the real GitHub integration, with the simulated API
and a local stand-in remote: every check reports, failures name their cause,
nothing is created or modified, no token is printed."""

import json
import os
from pathlib import Path

import pytest

from app.git import RepositoryConfig, RepositoryRegistry
from app.projects import ProjectPolicyRegistry
from scripts.github_preflight import PreflightDeps, run_preflight
from tests.unit.git_fixtures import git
from tests.unit.github_fixtures import NAME, OWNER, REPOSITORY_ID
from tests.unit.test_mirror_service import PROJECT, REPO, Lab, policy_for


def deps_for(
    lab: Lab, *, key_mode: int = 0o600, configured: bool = True, **overrides
) -> PreflightDeps:
    key = lab.tmp / "app.pem"
    if not key.exists():
        key.write_bytes(lab.github.private_key)
    os.chmod(key, key_mode)
    lab.github.branch_heads = lambda branch: lab.remote_head(branch) if branch == "main" else None
    values = dict(
        policies=lab.policies,
        registry=None,
        credentials=lab.credentials if configured else None,
        client=lab.client if configured else None,
        tokens=lab.tokens if configured else None,
        service=lab.service,
        key_file=key,
        scan_roots=[lab.root],
        credentials_error=None if key_mode == 0o600 else "modo inseguro",
    )
    values.update(overrides)
    return PreflightDeps(**values)  # type: ignore[arg-type]


def by_name(report: dict) -> dict[str, dict]:
    return {c["check"]: c for c in report["checks"]}


def test_preflight_passes_before_and_after_the_mirror_exists(tmp_path: Path):
    lab = Lab(tmp_path)
    report = run_preflight(deps_for(lab), repository_id=REPO, project_id=PROJECT)
    checks = by_name(report)
    assert report["ok"], json.dumps(report, indent=1)
    assert checks["private_key"]["mode"] == "0o600" and checks["private_key"]["owned_by_process"]
    assert checks["policy"]["github_repository_id"] == REPOSITORY_ID
    assert checks["app_auth"]["app_id"] == lab.credentials.app_id
    assert checks["installation_restricted"]["repository_selection"] == "selected"
    assert checks["installation_restricted"]["permissions"] == {
        "contents": "read",
        "metadata": "read",
    }
    assert checks["installation_restricted"]["account"] == OWNER
    assert checks["installation_token"]["permissions"] == {"contents": "read", "metadata": "read"}
    assert checks["repository_identity"]["observed_full_name"] == f"{OWNER}/{NAME}"
    assert checks["repository_identity"]["reachable_by_installation"] is True
    assert checks["remote_head"]["sha"] == lab.remote_head()
    assert checks["mirror"] == {
        "check": "mirror",
        "ok": True,
        "path": str(lab.mirror_path),
        "exists": False,
        "note": "not created yet",
    }
    assert checks["no_token_leaks"]["files_with_tokens"] == []
    assert not lab.mirror_path.exists()  # preflight never creates the mirror
    dumped = json.dumps(report)
    for secret in lab.github.minted:
        assert secret not in dumped
    # After the mirror exists: in sync, then behind the remote (reported, not failed).
    lab.service.create(REPO, PROJECT)
    registry = RepositoryRegistry(
        lab.root, [RepositoryConfig(id=REPO, project_id=PROJECT, path=lab.mirror_path)]
    )
    checks = by_name(
        run_preflight(deps_for(lab, registry=registry), repository_id=REPO, project_id=PROJECT)
    )
    assert checks["registry"]["ok"] and checks["registry"]["registered"]
    assert checks["mirror"]["ok"] and checks["mirror"]["in_sync_with_remote"]
    assert checks["mirror"]["marker_base_sha"] == lab.remote_head()
    assert checks["mirror"]["origin_url_has_credentials"] is False
    lab.commit_remote("src/behind.py", "b\n")
    checks = by_name(run_preflight(deps_for(lab), repository_id=REPO, project_id=PROJECT))
    assert checks["mirror"]["ok"] and checks["mirror"]["in_sync_with_remote"] is False
    assert checks["remote_head"]["sha"] == lab.remote_head() != checks["mirror"]["local_head"]


def test_preflight_names_every_misconfiguration(tmp_path: Path):
    lab = Lab(tmp_path)
    # Insecure key file.
    checks = by_name(
        run_preflight(deps_for(lab, key_mode=0o644), repository_id=REPO, project_id=PROJECT)
    )
    assert not checks["private_key"]["ok"] and checks["private_key"]["mode"] == "0o644"
    # Installation on all repositories, with forbidden or extra permissions, or suspended.
    lab.github.repository_selection = "all"
    checks = by_name(run_preflight(deps_for(lab), repository_id=REPO, project_id=PROJECT))
    assert not checks["installation_restricted"]["ok"]
    lab.github.repository_selection = "selected"
    lab.github.app_permissions = {"contents": "read", "metadata": "read", "workflows": "write"}
    checks = by_name(run_preflight(deps_for(lab), repository_id=REPO, project_id=PROJECT))
    assert checks["installation_restricted"]["forbidden_permissions"] == ["workflows"]
    assert not checks["installation_restricted"]["ok"]
    lab.github.app_permissions = {"contents": "write", "metadata": "read"}
    checks = by_name(run_preflight(deps_for(lab), repository_id=REPO, project_id=PROJECT))
    assert checks["installation_restricted"]["unexpected_permissions"] == {"contents": "write"}
    assert checks["installation_restricted"]["missing_permissions"] == ["contents"]
    lab.github.app_permissions = {}  # created without repository permissions (real case 2026-10-26)
    checks = by_name(run_preflight(deps_for(lab), repository_id=REPO, project_id=PROJECT))
    assert not checks["installation_restricted"]["ok"]
    assert checks["installation_restricted"]["missing_permissions"] == ["contents", "metadata"]
    lab.github.app_permissions = {"contents": "read", "metadata": "read"}
    lab.github.suspended = True
    assert not by_name(run_preflight(deps_for(lab), repository_id=REPO, project_id=PROJECT))[
        "installation_restricted"
    ]["ok"]
    lab.github.suspended = False
    # Repository identity drift.
    lab.github.identity_now(github_id=REPOSITORY_ID + 1)
    checks = by_name(run_preflight(deps_for(lab), repository_id=REPO, project_id=PROJECT))
    assert checks["repository_identity"]["error"] == "repository_substituted"
    assert not checks["repository_identity"]["ok"] and checks["installation_restricted"]["ok"]
    lab.github.identity_now()
    # App rejected by GitHub.
    lab.github.reject_app_jwt = True
    checks = by_name(run_preflight(deps_for(lab), repository_id=REPO, project_id=PROJECT))
    assert checks["github"] == {
        "check": "github",
        "ok": False,
        "error": "app_auth_failed",
        "detail": checks["github"]["detail"],
    }
    lab.github.reject_app_jwt = False
    # Wrong installation id in the policy.
    lab.policies = ProjectPolicyRegistry(
        [policy_for(policy_for().remote.model_copy(update={"installation_id": 99}))]
    )  # type: ignore[union-attr]
    checks = by_name(
        run_preflight(
            deps_for(lab, policies=lab.policies, service=lab.make_service()),
            repository_id=REPO,
            project_id=PROJECT,
        )
    )
    assert checks["github"]["error"] == "installation_not_found"
    lab.policies = ProjectPolicyRegistry([policy_for()])
    lab.service = lab.make_service()
    # Mirror tampered with: origin URL changed.
    lab.service.create(REPO, PROJECT)
    git(
        "remote",
        "set-url",
        "origin",
        "https://github.com/x/y.git",
        cwd=lab.mirror_path,
        home=lab.home,
    )
    checks = by_name(run_preflight(deps_for(lab), repository_id=REPO, project_id=PROJECT))
    assert checks["mirror"]["error"] == "mirror_remote_mismatch" and not checks["mirror"]["ok"]
    # Registry pointing elsewhere.
    other = RepositoryRegistry(
        tmp_path, [RepositoryConfig(id=REPO, project_id=PROJECT, path=lab.remote)]
    )
    checks = by_name(
        run_preflight(deps_for(lab, registry=other), repository_id=REPO, project_id=PROJECT)
    )
    assert not checks["registry"]["ok"] and checks["registry"]["expected_path"] == str(
        lab.mirror_path
    )
    # Policy problems and an unconfigured App stop early.
    local_only = ProjectPolicyRegistry([policy_for(remote=None)])
    report = run_preflight(
        deps_for(lab, policies=local_only), repository_id=REPO, project_id=PROJECT
    )
    assert by_name(report)["policy"]["error"] == "mirror_not_remote" and not report["ok"]
    report = run_preflight(deps_for(lab), repository_id="nope", project_id=PROJECT)
    assert by_name(report)["policy"]["error"] == "repository_not_authorized"
    report = run_preflight(deps_for(lab, configured=False), repository_id=REPO, project_id=PROJECT)
    assert by_name(report)["app_auth"]["error"] == "github_unconfigured"


def test_leak_scan_reports_tokens_in_files_and_database_rows(tmp_path: Path):
    lab = Lab(tmp_path)
    leaked = lab.root / PROJECT / "notes.txt"
    leaked.parent.mkdir(parents=True)
    leaked.write_text("token: ghs_" + "a" * 36 + "\n")
    deps = deps_for(lab, scan_database=lambda: [{"table": "run_events", "id": "7"}])
    report = run_preflight(deps, repository_id=REPO, project_id=PROJECT)
    leaks = by_name(report)["no_token_leaks"]
    assert not report["ok"] and not leaks["ok"]
    assert leaks["files_with_tokens"] == [str(leaked)]
    assert leaks["database_rows_with_tokens"] == [{"table": "run_events", "id": "7"}]
    leaked.unlink()
    deps = deps_for(lab, scan_database=lambda: [])
    assert by_name(run_preflight(deps, repository_id=REPO, project_id=PROJECT))["no_token_leaks"][
        "ok"
    ]


def test_main_refuses_without_git_configuration(monkeypatch: pytest.MonkeyPatch, capsys):
    import scripts.github_preflight as preflight

    class Unconfigured:
        git_repositories_file = None
        git_repositories_root = None
        projects_file = None

    monkeypatch.setattr("app.settings.get_settings", lambda: Unconfigured())
    assert preflight.main(["--repository", REPO, "--project", PROJECT]) == 2
    assert "git_unconfigured" in capsys.readouterr().err


def test_expect_publish_requires_write_grants_the_flag_and_a_protected_base(tmp_path: Path):
    from app.infrastructure.github import InstallationTokenProvider

    lab = Lab(tmp_path)
    lab.service.create(REPO, PROJECT)
    # Read-only installation, flag off: publish expectations fail on both counts.
    report = run_preflight(deps_for(lab), repository_id=REPO, project_id=PROJECT, expect="publish")
    checks = by_name(report)
    assert not report["ok"]
    assert checks["write_flag"]["ok"] is False and checks["write_flag"]["expect"] == "publish"
    assert checks["installation_restricted"]["missing_permissions"] == ["contents", "pull_requests"]
    assert checks["installation_restricted"]["expected_permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "metadata": "read",
    }
    # A read token still reads the rules: none configured yet on the simulated repository.
    assert checks["base_branch_protected"]["ok"] is False
    assert checks["base_branch_protected"]["missing_rules"] == [
        "pull_request",
        "non_fast_forward",
        "deletion",
    ]
    # Grants and flag in place, but GitHub cannot expose rules (free plan): stop.
    lab.github.app_permissions = {"contents": "write", "pull_requests": "write", "metadata": "read"}
    lab.github.branch_rules = None
    lab.tokens = InstallationTokenProvider(
        lab.client, lab.credentials, write_enabled=True, clock=lab.clock
    )
    deps = deps_for(lab, write_enabled=True, allowed_repositories=frozenset({REPO}))
    checks = by_name(run_preflight(deps, repository_id=REPO, project_id=PROJECT, expect="publish"))
    assert checks["write_flag"]["ok"] and checks["installation_restricted"]["ok"]
    assert checks["base_branch_protected"]["ok"] is False
    assert checks["base_branch_protected"]["error"] == "branch_rules_unavailable"
    # Rules present but incomplete; then complete: ok.
    lab.github.branch_rules = {"main": ["pull_request"]}
    checks = by_name(run_preflight(deps, repository_id=REPO, project_id=PROJECT, expect="publish"))
    assert checks["base_branch_protected"]["missing_rules"] == ["non_fast_forward", "deletion"]
    lab.github.branch_rules = {"main": ["pull_request", "non_fast_forward", "deletion"]}
    report = run_preflight(deps, repository_id=REPO, project_id=PROJECT, expect="publish")
    assert report["ok"], json.dumps(report, indent=1)
    assert by_name(report)["base_branch_protected"]["rules"] == [
        "deletion",
        "non_fast_forward",
        "pull_request",
    ]
    # The repository must be in the allowlist when one is configured; in read mode the
    # write flag must be off.
    narrowed = deps_for(lab, write_enabled=True, allowed_repositories=frozenset({"other"}))
    assert not by_name(
        run_preflight(narrowed, repository_id=REPO, project_id=PROJECT, expect="publish")
    )["write_flag"]["ok"]
    assert not by_name(run_preflight(deps, repository_id=REPO, project_id=PROJECT))["write_flag"][
        "ok"
    ]
    with pytest.raises(Exception, match="modo desconocido"):
        run_preflight(deps, repository_id=REPO, project_id=PROJECT, expect="merge")

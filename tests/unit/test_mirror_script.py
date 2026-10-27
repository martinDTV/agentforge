"""The operator mirror script with fakes: it only works with a configured
GitHub App, reports identities and commits, and never prints a token."""

import json
from pathlib import Path

import pytest

from app.infrastructure.db import InMemoryMirrorSyncRepository
from scripts.mirror_repository import MirrorDeps, MirrorRefused, deps_from_settings, main, run
from tests.unit.test_mirror_service import PROJECT, REPO, Lab

pytestmark = pytest.mark.anyio


async def test_create_refresh_and_check_report_without_secrets(tmp_path: Path):
    lab = Lab(tmp_path)
    syncs = InMemoryMirrorSyncRepository()
    lines: list[str] = []
    deps = MirrorDeps(service=lab.service, syncs=syncs, out=lines.append)
    created = await run(deps, operation="create", repository_id=REPO, project_id=PROJECT)
    assert created["result"] == "ok" and created["changed"] is True
    assert created["base_sha"] == lab.remote_head() and created["path"] == str(lab.mirror_path)
    assert created["github_repository_id"] and created["installation_id"]
    lab.commit_remote("src/n.py", "n\n")
    refreshed = await run(deps, operation="refresh", repository_id=REPO, project_id=PROJECT)
    assert refreshed["changed"] and refreshed["previous_sha"] == created["base_sha"]
    checked = await run(deps, operation="check", repository_id=REPO, project_id=PROJECT)
    assert checked["result"] == "ok" and checked["base_sha"] == refreshed["base_sha"]
    assert [r.operation for r in syncs.rows] == ["create", "refresh", "check"]
    lab.github.identity_now(github_id=1)
    failed = await run(deps, operation="refresh", repository_id=REPO, project_id=PROJECT)
    assert failed == {
        "operation": "refresh",
        "repository_id": REPO,
        "project_id": PROJECT,
        "result": "failed",
        "code": "repository_substituted",
        "detail": failed["detail"],
    }
    assert syncs.rows[-1].result == "failed" and syncs.rows[-1].code == "repository_substituted"
    for summary in (created, refreshed, checked, failed):
        dumped = json.dumps(summary)
        for secret in lab.github.minted:
            assert secret not in dumped
    with pytest.raises(MirrorRefused):
        await run(deps, operation="push", repository_id=REPO, project_id=PROJECT)


def test_the_script_refuses_without_the_github_app_or_git_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    class NoApp:
        github_app_id = None
        github_app_private_key_file = None

    with pytest.raises(MirrorRefused) as exc:
        deps_from_settings(NoApp())  # type: ignore[arg-type]
    assert exc.value.code == "github_unconfigured"

    class NoGit(NoApp):
        github_app_id = 1
        github_app_private_key_file = Path("/k.pem")
        git_repositories_file = None
        git_repositories_root = None
        projects_file = None

    with pytest.raises(MirrorRefused) as exc:
        deps_from_settings(NoGit())  # type: ignore[arg-type]
    assert exc.value.code == "git_unconfigured"
    import scripts.mirror_repository as script

    monkeypatch.setattr(script, "get_settings", lambda: NoApp())
    assert main(["--repository", REPO, "--project", PROJECT, "--create"]) == 2
    assert '"refused": "github_unconfigured"' in capsys.readouterr().err

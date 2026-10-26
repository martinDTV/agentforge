"""Local mirrors (Fase 7, Paso 2) against a local stand-in for GitHub's Git
endpoint (a repository on disk reached over ``file://``) and the simulated REST
API. Identity, remote, base commit, locks, idempotency, token renewal,
interruptions and credential cleanup are all exercised without network."""

import fcntl
import json
import logging
import os
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.git import GitOperationError, RepositoryConfig, RepositoryRegistry
from app.git.mirrors import MirrorError
from app.git.remote import RemoteSpec
from app.infrastructure.git import LocalGitAdapter
from app.infrastructure.git.mirror import MirrorService
from app.infrastructure.github import (
    GitHubAppCredentials,
    GitHubClient,
    InstallationTokenProvider,
)
from app.projects import PolicyError, ProjectPolicyRegistry, RepositoryPolicy
from tests.unit.git_fixtures import git, seed_repository
from tests.unit.github_fixtures import (
    APP_ID,
    INSTALLATION_ID,
    NAME,
    OWNER,
    REPOSITORY_ID,
    FakeGitHub,
)

PROJECT = "sandbox-gh"
REPO = "sandbox-demo"
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def remote_spec(**overrides) -> RemoteSpec:
    base = dict(
        owner=OWNER,
        name=NAME,
        github_repository_id=REPOSITORY_ID,
        installation_id=INSTALLATION_ID,
        default_branch="main",
    )
    return RemoteSpec(**{**base, **overrides})


def policy_for(remote: RemoteSpec | None = remote_spec(), **overrides) -> RepositoryPolicy:
    return RepositoryPolicy(
        repository_id=REPO,
        project_id=PROJECT,
        writable_globs=["src/**", "tests/**"],
        remote=remote,
        **overrides,
    )


class Lab:
    """Remote repository on disk + simulated GitHub + a configured mirror service."""

    def __init__(self, tmp_path: Path, *, policy: RepositoryPolicy | None = None) -> None:
        self.tmp = tmp_path
        tmp_path.mkdir(exist_ok=True)
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.remote = seed_repository(tmp_path / "remote", home=self.home)
        self.root = tmp_path / "repos"
        self.askpass = tmp_path / "askpass"
        self.askpass.mkdir()
        self.clock = Clock()
        self.github = FakeGitHub(clock=self.clock)
        self.client = GitHubClient(
            "https://api.github.test", transport=self.github.transport(), sleep=lambda s: None
        )
        self.credentials = GitHubAppCredentials(APP_ID, self.github.private_key, clock=self.clock)
        self.tokens = InstallationTokenProvider(self.client, self.credentials, clock=self.clock)
        self.policy = policy or policy_for()
        self.policies = ProjectPolicyRegistry([self.policy])
        self.adapter = LocalGitAdapter(scratch_home=tmp_path / "git-home")
        self.service = self.make_service()

    def make_service(self, *, github: bool = True, **overrides) -> MirrorService:
        kwargs = dict(
            git=self.adapter,
            repositories_root=self.root,
            policies=self.policies,
            github=self.client if github else None,
            tokens=self.tokens if github else None,
            clock=self.clock,
            url_for=lambda spec: self.remote.as_uri(),
            allow_file_remotes=True,
            askpass_dir=self.askpass,
        )
        kwargs.update(overrides)
        return MirrorService(**kwargs)  # type: ignore[arg-type]

    # remote-side actions (what a developer would push to GitHub)
    def remote_head(self, branch: str = "main") -> str:
        return git("rev-parse", branch, cwd=self.remote, home=self.home).strip()

    def commit_remote(self, relative: str, content: str, *, branch: str | None = None) -> str:
        if branch:
            git("checkout", "-q", "-B", branch, cwd=self.remote, home=self.home)
        (self.remote / relative).parent.mkdir(parents=True, exist_ok=True)
        (self.remote / relative).write_text(content)
        git("add", "-A", cwd=self.remote, home=self.home)
        git("commit", "-q", "-m", f"edit {relative}", cwd=self.remote, home=self.home)
        return self.remote_head(branch or "HEAD")

    @property
    def mirror_path(self) -> Path:
        return self.root / PROJECT / f"{NAME}.git"

    @property
    def marker(self) -> Path:
        return self.root / PROJECT / f"{NAME}.mirror.json"

    def files_containing(self, needle: str) -> list[Path]:
        hits = []
        for path in self.tmp.rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    if needle in path.read_bytes().decode("utf-8", "ignore"):
                        hits.append(path)
                except OSError:
                    pass
        return hits


def test_create_builds_a_bare_mirror_pinned_to_the_verified_identity(tmp_path: Path):
    lab = Lab(tmp_path)
    git("tag", "v1", cwd=lab.remote, home=lab.home)  # tags are never mirrored
    outcome = lab.service.create(REPO, PROJECT)
    record = outcome.record
    assert outcome.changed and record.base_sha == lab.remote_head()
    assert record.path == lab.mirror_path and (lab.mirror_path / "HEAD").is_file()
    assert not (lab.mirror_path / "refs" / "tags" / "v1").exists()
    assert lab.adapter.branch_heads(lab.mirror_path) == {"main": lab.remote_head()}
    assert lab.adapter.remote_url(lab.mirror_path) == lab.remote.as_uri()
    assert stat.S_IMODE(lab.marker.stat().st_mode) == 0o600
    marker = json.loads(lab.marker.read_text())
    assert marker["github_repository_id"] == REPOSITORY_ID
    assert marker["installation_id"] == INSTALLATION_ID and marker["base_sha"] == record.base_sha
    assert marker["remote_url"] == lab.remote.as_uri()
    sync = outcome.sync
    assert (sync.operation, sync.result, sync.changed, sync.previous_sha) == (
        "create",
        "ok",
        True,
        None,
    )
    assert sync.actor_subject == "agentforge:platform" and sync.code is None
    assert sync.token_expires_at == T0 + timedelta(hours=1)
    # GitHub saw: one token mint narrowed to the repository, then identity reads.
    methods = [(r.method, r.url.path) for r in lab.github.requests]
    assert methods == [
        ("POST", f"/app/installations/{INSTALLATION_ID}/access_tokens"),
        ("GET", f"/repos/{OWNER}/{NAME}"),
        ("GET", "/installation/repositories"),
    ]
    assert json.loads(lab.github.requests[0].read())["permissions"] == {
        "contents": "read",
        "metadata": "read",
    }
    assert list(lab.askpass.iterdir()) == []  # the credential directory is gone
    assert lab.service.record(REPO, PROJECT) == record


def test_refresh_is_idempotent_tracks_the_remote_head_and_prunes_branches(tmp_path: Path):
    lab = Lab(tmp_path)
    first = lab.service.create(REPO, PROJECT).record
    again = lab.service.refresh(REPO, PROJECT)
    assert not again.changed and again.record.base_sha == first.base_sha
    assert again.sync.previous_sha == first.base_sha
    assert lab.service.create(REPO, PROJECT).record.base_sha == first.base_sha  # idempotent create
    new_head = lab.commit_remote("src/new.py", "x = 1\n")
    lab.commit_remote("src/feature.py", "y = 2\n", branch="feature")
    git("checkout", "-q", "main", cwd=lab.remote, home=lab.home)
    moved = lab.service.refresh(REPO, PROJECT)
    assert moved.changed and moved.record.base_sha == new_head
    assert moved.sync.previous_sha == first.base_sha
    assert set(lab.adapter.branch_heads(lab.mirror_path)) == {"main", "feature"}
    git("branch", "-D", "feature", cwd=lab.remote, home=lab.home)
    lab.service.refresh(REPO, PROJECT)
    assert set(lab.adapter.branch_heads(lab.mirror_path)) == {"main"}  # pruned
    # A later check agrees with the marker without fetching.
    checked = lab.service.check(REPO, PROJECT)
    assert checked.record.base_sha == new_head and checked.sync.operation == "check"
    assert lab.tokens.minted == 1  # one token served every operation within its lifetime


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"github_id": REPOSITORY_ID + 1}, "repository_substituted"),
        ({"owner": "someone-else"}, "repository_transferred"),
        ({"name": "renamed"}, "repository_transferred"),
        ({"default_branch": "develop"}, "default_branch_changed"),
        ({"archived": True}, "repository_archived"),
    ],
)
def test_identity_changes_at_github_are_refused_before_any_fetch(
    tmp_path: Path, change: dict, code: str
):
    lab = Lab(tmp_path)
    base = lab.service.create(REPO, PROJECT).record.base_sha
    lab.commit_remote("src/later.py", "z = 3\n")
    lab.github.identity_now(**change)
    with pytest.raises(MirrorError) as exc:
        lab.service.refresh(REPO, PROJECT)
    assert exc.value.code == code
    assert exc.value.sync is not None and exc.value.sync.result == "failed"
    assert exc.value.sync.code == code and exc.value.sync.operation == "refresh"
    assert lab.adapter.branch_heads(lab.mirror_path)["main"] == base  # nothing fetched
    assert json.loads(lab.marker.read_text())["base_sha"] == base
    assert list(lab.askpass.iterdir()) == []


def test_a_repository_outside_the_installation_is_refused(tmp_path: Path):
    lab = Lab(tmp_path)
    lab.service.create(REPO, PROJECT)
    lab.github.installation_lists_nothing = True  # membership listing no longer shows the repo
    with pytest.raises(MirrorError) as exc:
        lab.service.refresh(REPO, PROJECT)
    assert exc.value.code == "repository_not_in_installation"


def test_remote_and_marker_tampering_are_detected(tmp_path: Path):
    lab = Lab(tmp_path)
    base = lab.service.create(REPO, PROJECT).record.base_sha
    # Someone points the mirror at another remote.
    other = seed_repository(tmp_path / "other", home=lab.home, files={"evil.py": "x\n"})
    git("remote", "set-url", "origin", other.as_uri(), cwd=lab.mirror_path, home=lab.home)
    with pytest.raises(MirrorError) as exc:
        lab.service.refresh(REPO, PROJECT)
    assert exc.value.code == "mirror_remote_mismatch"
    assert lab.adapter.branch_heads(lab.mirror_path)["main"] == base
    git("remote", "set-url", "origin", lab.remote.as_uri(), cwd=lab.mirror_path, home=lab.home)
    assert lab.service.refresh(REPO, PROJECT).record.base_sha == base
    # The marker names a different repository id.
    marker = json.loads(lab.marker.read_text())
    lab.marker.write_text(json.dumps({**marker, "github_repository_id": REPOSITORY_ID + 1}))
    with pytest.raises(MirrorError) as exc:
        lab.service.refresh(REPO, PROJECT)
    assert exc.value.code == "mirror_identity_mismatch"
    lab.marker.write_text("{not json")
    with pytest.raises(MirrorError) as exc:
        lab.service.check(REPO, PROJECT)
    assert exc.value.code == "mirror_marker_invalid"
    lab.marker.write_text(json.dumps(marker))
    # The operator changes the policy to another installation: recreate on purpose.
    lab.policies = ProjectPolicyRegistry([policy_for(remote_spec(installation_id=56))])
    lab.github.installation_id = 56
    changed = lab.make_service()
    with pytest.raises(MirrorError) as exc:
        changed.refresh(REPO, PROJECT)
    assert exc.value.code == "mirror_identity_mismatch"
    with pytest.raises(MirrorError) as exc:
        changed.create(REPO, PROJECT)  # never silently re-pins an existing mirror
    assert exc.value.code == "mirror_identity_mismatch"
    # A local move of the mirrored branch is caught by check.
    lab.github.installation_id = INSTALLATION_ID
    lab.policies = ProjectPolicyRegistry([policy_for()])
    service = lab.make_service()
    git("update-ref", "refs/heads/main", f"{base}^{{commit}}", cwd=lab.mirror_path, home=lab.home)
    orphan = lab.commit_remote("src/o.py", "o\n")
    git("fetch", "-q", lab.remote.as_uri(), "main", cwd=lab.mirror_path, home=lab.home)
    git("update-ref", "refs/heads/main", orphan, cwd=lab.mirror_path, home=lab.home)
    with pytest.raises(MirrorError) as exc:
        service.check(REPO, PROJECT)
    assert exc.value.code == "mirror_base_mismatch"
    lab.marker.unlink()
    with pytest.raises(MirrorError) as exc:
        service.refresh(REPO, PROJECT)
    assert exc.value.code == "mirror_missing"


def test_concurrent_synchronisations_are_serialised_by_a_per_repository_lock(tmp_path: Path):
    lab = Lab(tmp_path)
    lab.service.create(REPO, PROJECT)
    lock_path = lab.root / PROJECT / f"{NAME}.mirror.lock"
    fd = os.open(lock_path, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another process holds it
    try:
        with pytest.raises(MirrorError) as exc:
            lab.service.refresh(REPO, PROJECT)
        assert exc.value.code == "mirror_locked"
        assert lab.github.requests[-1].url.path == "/installation/repositories"  # no new calls
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert lab.service.refresh(REPO, PROJECT).sync.result == "ok"
    # Two threads: exactly one of them may win a slot at a time; nothing is corrupted.
    lab.commit_remote("src/race.py", "r\n")
    results: list[str] = []

    def worker() -> None:
        try:
            results.append(lab.service.refresh(REPO, PROJECT).sync.result)
        except MirrorError as error:
            results.append(error.code)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(results) <= {"ok", "mirror_locked"} and "ok" in results
    assert lab.service.check(REPO, PROJECT).record.base_sha == lab.remote_head()


def test_expired_and_revoked_tokens_are_renewed_once_and_auth_failures_stop_the_sync(
    tmp_path: Path,
):
    lab = Lab(tmp_path)
    lab.service.create(REPO, PROJECT)
    first = lab.github.minted[-1]
    lab.clock.now += timedelta(hours=2)  # the cached token has expired
    lab.service.refresh(REPO, PROJECT)
    assert lab.tokens.minted == 2 and lab.github.minted[-1] != first
    current = lab.github.minted[-1]
    lab.github.revoked.add(current)  # revoked at GitHub while still cached here
    lab.service.refresh(REPO, PROJECT)
    assert lab.tokens.minted == 3 and lab.github.minted[-1] != current
    lab.github.reject_app_jwt = True  # the App itself is no longer accepted
    lab.clock.now += timedelta(hours=2)
    lab.commit_remote("src/x.py", "x\n")
    with pytest.raises(MirrorError) as exc:
        lab.service.refresh(REPO, PROJECT)
    assert exc.value.code == "app_auth_failed"
    assert lab.adapter.branch_heads(lab.mirror_path)["main"] != lab.remote_head()


def test_an_interrupted_fetch_keeps_the_previous_base_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    lab = Lab(tmp_path)
    base = lab.service.create(REPO, PROJECT).record.base_sha
    lab.commit_remote("src/y.py", "y\n")
    original = lab.adapter.fetch_heads

    def interrupted(*args, **kwargs):
        raise GitOperationError("git_timeout", "git fetch superó 1s")

    monkeypatch.setattr(lab.adapter, "fetch_heads", interrupted)
    with pytest.raises(MirrorError) as exc:
        lab.service.refresh(REPO, PROJECT)
    assert exc.value.code == "mirror_sync_failed" and exc.value.sync is not None
    assert json.loads(lab.marker.read_text())["base_sha"] == base
    assert list(lab.askpass.iterdir()) == []  # credential removed despite the failure
    attempts = {"n": 0}

    def flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise GitOperationError("git_failed", "fatal: Authentication failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(lab.adapter, "fetch_heads", flaky)
    recovered = lab.service.refresh(REPO, PROJECT)  # one fresh token, one retry
    assert recovered.changed and recovered.record.base_sha == lab.remote_head()
    assert attempts["n"] == 2 and lab.tokens.minted == 2
    # A create that fails midway leaves no half mirror behind.
    lab2 = Lab(tmp_path / "second")
    monkeypatch.setattr(lab2.adapter, "fetch_heads", interrupted)
    with pytest.raises(MirrorError):
        lab2.service.create(REPO, PROJECT)
    assert not lab2.mirror_path.exists() and not lab2.marker.exists()


def test_credentials_never_land_on_disk_or_in_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    lab = Lab(tmp_path)
    with caplog.at_level(logging.DEBUG):
        created = lab.service.create(REPO, PROJECT)
        lab.commit_remote("src/z.py", "z\n")
        refreshed = lab.service.refresh(REPO, PROJECT)
    secrets = lab.github.minted
    assert secrets
    for secret in secrets:
        assert lab.files_containing(secret) == []
        assert secret not in caplog.text
        for sync in (created.sync, refreshed.sync):
            assert secret not in sync.model_dump_json()
        assert secret not in created.record.model_dump_json()
    config = (lab.mirror_path / "config").read_text()
    assert "x-access-token" not in config and "@" not in config.split("url = ")[1].split("\n")[0]


def test_unconfigured_or_misconfigured_mirrors_are_refused(tmp_path: Path):
    lab = Lab(tmp_path)
    offline = lab.make_service(github=False)
    assert not offline.configured
    with pytest.raises(MirrorError) as exc:
        offline.create(REPO, PROJECT)
    assert exc.value.code == "mirror_unconfigured"
    with pytest.raises(PolicyError):
        lab.service.refresh("unknown", PROJECT)
    local_only = ProjectPolicyRegistry([policy_for(remote=None)])
    with pytest.raises(MirrorError) as exc:
        lab.make_service(policies=local_only).refresh(REPO, PROJECT)
    assert exc.value.code == "mirror_not_remote"
    # The repositories file must point at the mirror path, or the run would use another repo.
    lab.service.create(REPO, PROJECT)
    elsewhere = seed_repository(tmp_path / "elsewhere", home=lab.home)
    registry = RepositoryRegistry(
        tmp_path, [RepositoryConfig(id=REPO, project_id=PROJECT, path=elsewhere)]
    )
    with pytest.raises(MirrorError) as exc:
        lab.make_service(registry=registry).refresh(REPO, PROJECT)
    assert exc.value.code == "mirror_path_mismatch"
    good = RepositoryRegistry(
        lab.root, [RepositoryConfig(id=REPO, project_id=PROJECT, path=lab.mirror_path)]
    )
    assert lab.make_service(registry=good).check(REPO, PROJECT).sync.result == "ok"
    # Offline check still validates marker, remote and base locally.
    assert offline.check(REPO, PROJECT).sync.result == "ok"
    # A directory that is not ours is never adopted; a missing default branch aborts.
    lab3 = Lab(tmp_path / "third")
    lab3.mirror_path.mkdir(parents=True)
    with pytest.raises(MirrorError) as exc:
        lab3.service.create(REPO, PROJECT)
    assert exc.value.code == "mirror_exists"
    lab3.mirror_path.rmdir()
    git("branch", "-m", "main", "trunk", cwd=lab3.remote, home=lab3.home)
    with pytest.raises(MirrorError) as exc:
        lab3.service.create(REPO, PROJECT)
    assert exc.value.code == "mirror_base_missing" and not lab3.mirror_path.exists()

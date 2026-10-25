"""Idempotency, concurrency, cancellation, timeouts, isolation refusal and the Podman shape."""

import io
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentforge_runner.auth import token_digest
from agentforge_runner.backend import ContainerState, RunOutcome, RunSpec
from agentforge_runner.fake_backend import FakeBackend, completed
from agentforge_runner.podman import (
    BOOTSTRAP,
    PROBE_LIMITS,
    Completed,
    PodmanBackend,
    build_run_argv,
    evaluate_probe,
)
from agentforge_runner.protocol import SandboxLimits, SandboxStatus
from agentforge_runner.service import create_runner_app
from agentforge_runner.store import ExecutionStore, request_digest

from tests.unit.runner_fixtures import (
    AUTH,
    DEFAULT_ARCHIVE,
    IMAGE,
    RunnerHarness,
    make_harness,
    make_request,
    make_settings,
    multipart,
)

ADMIN_TOKEN = "admin-token-abcdefghijklmnopqrstuvwxyz0123456789"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def post(client: Any, request: Any, archive: bytes = DEFAULT_ARCHIVE) -> Any:
    return client.post("/v1/executions", files=multipart(request, archive), headers=AUTH)


def test_the_same_execution_twice_returns_the_stored_result_and_runs_once(tmp_path: Path):
    harness = make_harness(tmp_path)
    request = make_request()
    with harness.client() as client:
        first = post(client, request)
        second = post(client, request)
        shown = client.get(f"/v1/executions/{request.execution_id}", headers=AUTH)
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    assert len(harness.backend.runs) == 1
    assert shown.status_code == 200 and shown.json()["state"] == "finished"
    # Same id, different content: a conflict, never a silent overwrite.
    tampered = request.model_copy(update={"argv": [*request.argv[:-1], "."]})
    with harness.client() as client:
        conflict = post(client, tampered)
    assert conflict.status_code == 409 and conflict.json()["code"] == "execution_conflict"
    assert len(harness.backend.runs) == 1


def test_concurrent_identical_requests_launch_one_container(tmp_path: Path):
    harness = make_harness(tmp_path, max_concurrent=4)
    harness.backend.hold.set()
    request = make_request()
    with harness.client() as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(post, client, request)
            assert harness.backend.started.wait(10)
            second = post(client, request)
            assert second.status_code == 409 and second.json()["code"] == "execution_in_progress"
            harness.backend.release.set()
            done = first.result(timeout=10)
    assert done.status_code == 200 and done.json()["status"] == "completed"
    assert len(harness.backend.runs) == 1


def test_capacity_is_bounded_and_reported_as_busy(tmp_path: Path):
    harness = make_harness(tmp_path, max_concurrent=1)
    harness.backend.hold.set()
    with harness.client() as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(post, client, make_request())
            assert harness.backend.started.wait(10)
            busy = post(client, make_request())
            assert busy.status_code == 503 and busy.json()["code"] == "runner_busy"
            harness.backend.release.set()
            assert running.result(timeout=10).status_code == 200
        after = post(client, make_request())  # the slot was released
        assert after.status_code == 200
    assert len(harness.backend.runs) == 2


def test_cancellation_kills_the_real_container_and_is_recorded(tmp_path: Path):
    harness = make_harness(tmp_path)
    harness.backend.hold.set()
    request = make_request()
    with harness.client() as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(post, client, request)
            assert harness.backend.started.wait(10)
            cancelled = client.delete(f"/v1/executions/{request.execution_id}", headers=AUTH)
            assert cancelled.status_code == 204
            result = running.result(timeout=10)
        assert result.json()["status"] == "cancelled"
        assert harness.backend.killed == [f"agentforge-{request.execution_id}"]
        record = harness.store.get(request.execution_id)
        assert record is not None and record.cancel_requested and record.state == "finished"
        again = client.delete(f"/v1/executions/{request.execution_id}", headers=AUTH)
        assert again.status_code == 409 and again.json()["code"] == "execution_finished"
        unknown = client.delete(f"/v1/executions/{uuid.uuid4()}", headers=AUTH)
        assert unknown.status_code == 404


def test_backend_outcomes_are_persisted_as_results(tmp_path: Path):
    harness = make_harness(tmp_path)

    def timed_out(spec: RunSpec) -> RunOutcome:
        base = completed(spec, exit_code=137, stdout="partial", stderr="")
        return RunOutcome(
            **{**base.__dict__, "status": SandboxStatus.TIMED_OUT, "stdout_truncated": True}
        )

    harness.backend.outcome = timed_out
    request = make_request()
    with harness.client() as client:
        response = post(client, request)
    body = response.json()
    assert (
        body["status"] == "timed_out"
        and body["stdout_truncated"] is True
        and body["exit_code"] == 137
    )
    record = harness.store.get(request.execution_id)
    assert record is not None and record.error_code == "timed_out"

    def oom(spec: RunSpec) -> RunOutcome:
        base = completed(spec, exit_code=137)
        return RunOutcome(**{**base.__dict__, "status": SandboxStatus.KILLED, "oom_killed": True})

    harness.backend.outcome = oom
    with harness.client() as client:
        killed = post(client, make_request()).json()
    assert killed["status"] == "killed" and killed["usage"]["oom_killed"] is True


def _crashed_store(tmp_path: Path, container: str) -> tuple[ExecutionStore, object]:
    settings = make_settings(tmp_path, admin_token_sha256=token_digest(ADMIN_TOKEN))
    store = ExecutionStore(settings.state_dir / "executions.sqlite3")
    request = make_request()
    claimed, _ = store.claim(request, request_digest(request))
    assert claimed and store.mark_running(request.execution_id, container)
    store.close()
    return ExecutionStore(settings.state_dir / "executions.sqlite3"), (settings, request)


def test_rows_left_open_by_a_crash_become_uncertain_and_never_rerun(tmp_path: Path):
    reopened, (settings, request) = _crashed_store(tmp_path, "agentforge-x")
    backend = FakeBackend()  # the engine knows nothing about agentforge-x
    backend.existing = ["agentforge-orphan"]
    harness = RunnerHarness(
        settings, backend, reopened, create_runner_app(settings, backend, reopened)
    )  # type: ignore[arg-type]
    with harness.client() as client:
        record = reopened.get(request.execution_id)  # type: ignore[attr-defined]
        assert record is not None and record.state == "uncertain"
        assert record.error_code == "container_missing"
        assert backend.removed == ["agentforge-orphan"]  # leftovers nobody claims are removed
        again = post(client, request)  # type: ignore[arg-type]
        assert again.status_code == 409 and again.json()["code"] == "execution_uncertain"
        shown = client.get(f"/v1/executions/{request.execution_id}", headers=AUTH).json()  # type: ignore[attr-defined]
        assert shown["state"] == "uncertain"
    assert backend.runs == []


def test_startup_reconciles_with_engine_evidence_instead_of_guessing(tmp_path: Path):
    exited_store, (settings, request) = _crashed_store(tmp_path / "a", "agentforge-exited")
    backend = FakeBackend()
    backend.states["agentforge-exited"] = ContainerState(
        name="agentforge-exited",
        running=False,
        exit_code=3,
        oom_killed=False,
        finished_at=datetime.now(UTC),
    )
    harness = RunnerHarness(
        settings, backend, exited_store, create_runner_app(settings, backend, exited_store)
    )  # type: ignore[arg-type]
    with harness.client() as client:
        record = exited_store.get(request.execution_id)  # type: ignore[attr-defined]
        assert record is not None and record.state == "finished" and record.result is not None
        assert record.result.status is SandboxStatus.COMPLETED and record.result.exit_code == 3
        assert record.resolution == "engine:exit=3,oom=False" and record.result.stdout_truncated
        assert backend.removed == ["agentforge-exited"]
        replay = post(client, request)  # type: ignore[arg-type]
        assert (
            replay.status_code == 200 and replay.json()["exit_code"] == 3
        )  # the evidence is the answer
    running_store, (settings2, request2) = _crashed_store(tmp_path / "b", "agentforge-live")
    backend2 = FakeBackend()
    backend2.states["agentforge-live"] = ContainerState(
        name="agentforge-live", running=True, exit_code=None, oom_killed=False, finished_at=None
    )
    create_runner_app(settings2, backend2, running_store)
    harness2 = RunnerHarness(
        settings2, backend2, running_store, create_runner_app(settings2, backend2, running_store)
    )  # type: ignore[arg-type]
    with harness2.client():
        record2 = running_store.get(request2.execution_id)  # type: ignore[attr-defined]
        assert record2 is not None and record2.state == "finished" and record2.result is not None
        assert (
            record2.result.status is SandboxStatus.CANCELLED
            and record2.error_code == "runner_restarted"
        )
        assert backend2.killed == ["agentforge-live"] and "agentforge-live" in backend2.removed


def test_operators_inspect_and_close_uncertain_executions_but_never_mark_them_successful(
    tmp_path: Path,
):
    reopened, (settings, request) = _crashed_store(tmp_path, "agentforge-gone")
    backend = FakeBackend()
    harness = RunnerHarness(
        settings, backend, reopened, create_runner_app(settings, backend, reopened)
    )  # type: ignore[arg-type]
    rid = request.execution_id  # type: ignore[attr-defined]
    with harness.client() as client:
        assert (
            client.get("/v1/admin/executions", headers=AUTH).status_code == 401
        )  # API token is not admin
        listed = client.get("/v1/admin/executions", headers=ADMIN)
        assert listed.status_code == 200 and [e["execution_id"] for e in listed.json()] == [
            str(rid)
        ]
        detail = client.get(f"/v1/admin/executions/{rid}", headers=ADMIN).json()
        assert detail["state"] == "uncertain" and detail["error_code"] == "container_missing"
        no_evidence = client.post(f"/v1/admin/executions/{rid}/reconcile", headers=ADMIN)
        assert no_evidence.status_code == 409 and no_evidence.json()["code"] == "no_evidence"
        for body in (
            {"outcome": "completed", "reason": "looked fine"},
            {"outcome": "failed"},
            {"outcome": "failed", "reason": "x", "exit_code": 0},
        ):
            assert (
                client.post(
                    f"/v1/admin/executions/{rid}/resolve", json=body, headers=ADMIN
                ).status_code
                == 422
            ), body
        resolved = client.post(
            f"/v1/admin/executions/{rid}/resolve",
            json={"outcome": "failed", "reason": "VM rebooted"},
            headers=ADMIN,
        )
        assert resolved.status_code == 200 and resolved.json()["state"] == "finished"
        assert (
            resolved.json()["resolution"] == "operator:failed"
            and resolved.json()["result"]["status"] == "failed"
        )
        again = client.post(
            f"/v1/admin/executions/{rid}/resolve",
            json={"outcome": "failed", "reason": "twice"},
            headers=ADMIN,
        )
        assert again.status_code == 409 and again.json()["code"] == "not_uncertain"
        replay = post(client, request)  # type: ignore[arg-type]
        assert (
            replay.status_code == 200 and replay.json()["status"] == "failed"
        )  # the verdict is the answer
    # Reconcile with evidence when the engine still has the container.
    reopened2, (settings2, request2) = _crashed_store(tmp_path / "evidence", "agentforge-kept")
    backend2 = FakeBackend()
    harness2 = RunnerHarness(
        settings2, backend2, reopened2, create_runner_app(settings2, backend2, reopened2)
    )  # type: ignore[arg-type]
    with harness2.client() as client:
        assert reopened2.get(request2.execution_id).state == "uncertain"  # type: ignore[union-attr,attr-defined]
        backend2.states["agentforge-kept"] = ContainerState(
            name="agentforge-kept",
            running=False,
            exit_code=0,
            oom_killed=True,
            finished_at=datetime.now(UTC),
        )
        reconciled = client.post(
            f"/v1/admin/executions/{request2.execution_id}/reconcile", headers=ADMIN
        )  # type: ignore[attr-defined]
        assert reconciled.status_code == 200
        body = reconciled.json()
        assert body["state"] == "finished" and body["result"]["status"] == "killed"
        assert body["result"]["usage"]["oom_killed"] is True and body["resolution"].startswith(
            "engine:"
        )
    no_admin = make_harness(tmp_path / "noadmin")
    with no_admin.client() as client:
        assert client.get("/v1/admin/executions", headers=ADMIN).status_code == 404


def test_isolation_that_cannot_be_proven_refuses_all_work(tmp_path: Path):
    harness = make_harness(tmp_path)
    harness.backend.isolation_ok = False
    with harness.client() as client:
        health = client.get("/v1/health", headers=AUTH).json()
        assert health["ready"] is False and any(not c["ok"] for c in health["checks"])
        refused = post(client, make_request())
    assert refused.status_code == 503 and refused.json()["code"] == "isolation_unavailable"
    assert "cgroups_v2" in refused.json()["detail"] and harness.backend.runs == []
    harness.backend.isolation_ok, harness.backend.rootless = True, False
    with harness.client() as client:
        assert post(client, make_request()).json()["code"] == "isolation_unavailable"
        assert client.get("/v1/health", headers=AUTH).json()["rootless"] is False


# -- Podman backend: the container shape and preflight, with a scripted podman -------------


class ScriptedPodman:
    def __init__(self, info: dict[str, Any], *, images_present: bool = True) -> None:
        self.info = info
        self.images_present = images_present
        self.calls: list[list[str]] = []

    probe_ok = True

    def run(self, argv: Any, *, timeout: float, stdin: bytes | None = None) -> Completed:
        argv = list(argv)
        self.calls.append(argv)
        if argv[1:3] == ["info", "--format"]:
            return Completed(0, json.dumps(self.info), "")
        if argv[1:3] == ["image", "exists"]:
            return Completed(0 if self.images_present else 1, "", "")
        if argv[1] == "run":  # the isolation probe (never a real workload here)
            assert stdin == b"" and "--network" in argv and "none" in argv
            return Completed(0, json.dumps(probe_payload(ok=self.probe_ok)), "")
        if argv[1] in ("kill", "rm", "ps"):
            return Completed(0, "", "")
        if argv[1] == "inspect":
            return Completed(0, "false false 0 2026-10-25T00:00:00Z", "")
        return Completed(125, "", "unexpected")

    def start(self, argv: Any) -> Any:
        raise AssertionError("no container may be started in the unit suite")


def probe_payload(*, ok: bool = True) -> dict[str, Any]:
    limits = PROBE_LIMITS
    return {
        "uid_unprivileged": True,
        "root_readonly": True,
        "work_writable": True,
        "work_bytes": limits.storage_bytes,
        "network_blocked": ok,
        "interfaces": ["lo"] if ok else ["eth0", "lo"],
        "no_capabilities": True,
        "no_new_privs": True,
        "memory_max": str(limits.memory_bytes + limits.storage_bytes),
        "pids_max": str(limits.pids),
        "cpu_max": "100000 100000",
    }


ROOTLESS_INFO = {
    "host": {
        "security": {"rootless": True},
        "cgroupVersion": "v2",
        "cgroupControllers": ["cpu", "memory", "pids", "io"],
    },
    "version": {"Version": "4.9.3"},
}


def test_podman_preflight_requires_rootless_cgroups_v2_controllers_and_pinned_images():
    ok = PodmanBackend("podman", runner=ScriptedPodman(ROOTLESS_INFO), uid=1000, gid=1000)
    report = ok.preflight({"python-3.13": IMAGE})
    assert report.ok and report.rootless and report.backend == "podman-rootless"
    names = {c.name for c in report.checks}
    assert {
        "probe:root_readonly",
        "probe:work_quota",
        "probe:network_blocked",
        "probe:no_capabilities",
        "probe:memory_max",
    } <= names
    probe_runs = [c for c in ok._runner.calls if c[1] == "run"]  # type: ignore[attr-defined]
    assert len(probe_runs) == 1 and "--rm" not in probe_runs[0]
    probe_name = probe_runs[0][probe_runs[0].index("--name") + 1]
    assert probe_name.startswith("agentforge-probe-")
    removals = [c for c in ok._runner.calls if c[1] == "rm"]  # type: ignore[attr-defined]
    assert removals and removals[-1][-1] == probe_name  # the probe never lingers
    leaky = ScriptedPodman(ROOTLESS_INFO)
    leaky.probe_ok = False
    leaked = PodmanBackend("podman", runner=leaky, uid=1000, gid=1000).preflight({"p": IMAGE})
    assert not leaked.ok
    assert not next(c for c in leaked.checks if c.name == "probe:network_blocked").ok
    assert {c.name for c in report.checks} >= {
        "rootless",
        "cgroups_v2",
        "cgroup_controllers",
        "podman_version",
        "image:python-3.13",
        "unprivileged_user",
    }
    for broken, failing in (
        (
            {**ROOTLESS_INFO, "host": {**ROOTLESS_INFO["host"], "security": {"rootless": False}}},
            "rootless",
        ),
        ({**ROOTLESS_INFO, "host": {**ROOTLESS_INFO["host"], "cgroupVersion": "v1"}}, "cgroups_v2"),
        (
            {**ROOTLESS_INFO, "host": {**ROOTLESS_INFO["host"], "cgroupControllers": ["cpu"]}},
            "cgroup_controllers",
        ),
        ({**ROOTLESS_INFO, "version": {"Version": "3.4.4"}}, "podman_version"),
    ):
        report = PodmanBackend(
            "podman", runner=ScriptedPodman(broken), uid=1000, gid=1000
        ).preflight({"p": IMAGE})
        assert not report.ok and not next(c for c in report.checks if c.name == failing).ok, failing
    missing = PodmanBackend(
        "podman", runner=ScriptedPodman(ROOTLESS_INFO, images_present=False), uid=1000, gid=1000
    )
    assert not missing.preflight({"p": IMAGE}).ok
    as_root = PodmanBackend("podman", runner=ScriptedPodman(ROOTLESS_INFO), uid=0, gid=0)
    assert not as_root.preflight({"p": IMAGE}).ok


def test_the_container_shape_is_fixed_and_hardened():
    spec = RunSpec(
        container_name="agentforge-x",
        image=IMAGE,
        argv=["ruff", "check", "--", "."],
        env={"CI": "1", "LANG": "C.UTF-8"},
        archive=DEFAULT_ARCHIVE,
        limits=SandboxLimits(
            cpu_seconds=30,
            memory_bytes=256 * 1024 * 1024,
            pids=64,
            wall_seconds=45,
            storage_bytes=64 * 1024 * 1024,
        ),
        output_cap=1000,
    )
    argv = build_run_argv("/usr/bin/podman", spec, uid=1000, gid=1000)
    text = " ".join(argv)
    assert argv[:3] == ["/usr/bin/podman", "run", "--name"]
    for flag in (
        "--interactive",
        "--network none",
        "--no-hosts",
        "--read-only",
        "--mount type=tmpfs,dst=/work,tmpfs-size=67108864,tmpfs-mode=1777,noexec,nosuid,nodev",
        "--mount type=tmpfs,dst=/tmp,tmpfs-size=67108864,tmpfs-mode=1777,noexec,nosuid,nodev",
        "--userns=keep-id",
        "--user 1000:1000",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 64",
        f"--memory {256 * 1024 * 1024 + 64 * 1024 * 1024}",  # tmpfs pages count against the cgroup
        f"--memory-swap {256 * 1024 * 1024 + 64 * 1024 * 1024}",
        "--ulimit cpu=30:30",
        "--timeout 50",  # wall 45 + 5 s of grace: the runner's own timer fires first
        "--log-driver none",
        "--pull never",
        "--env HOME=/tmp",
        "--env CI=1",
        "--env LANG=C.UTF-8",
    ):
        assert flag in text, flag
    for forbidden in (
        "--privileged",
        "docker.sock",
        "podman.sock",
        "--cap-add",
        "--network host",
        "type=bind",
        "--volume",
        " -v ",
    ):
        assert forbidden not in text, forbidden
    image_at = argv.index(IMAGE)
    assert argv[image_at + 1 : image_at + 4] == ["python", "-I", "-c"]
    assert argv[image_at + 4] == BOOTSTRAP and argv[image_at + 5 :] == ["ruff", "check", "--", "."]
    assert "filter='data'" in BOOTSTRAP and "execvp" in BOOTSTRAP and "/work" in BOOTSTRAP
    assert "@sha256:" in argv[image_at]


def test_probe_evaluation_demands_every_property():
    good = evaluate_probe(json.dumps(probe_payload()), PROBE_LIMITS)
    assert all(c.ok for c in good) and len(good) == 9
    for key, bad in (
        ("root_readonly", False),
        ("work_bytes", 1),
        ("interfaces", ["eth0", "lo"]),
        ("no_capabilities", False),
        ("no_new_privs", False),
        ("memory_max", "max"),
        ("pids_max", "max"),
        ("uid_unprivileged", False),
    ):
        payload = {**probe_payload(), key: bad}
        assert not all(c.ok for c in evaluate_probe(json.dumps(payload), PROBE_LIMITS)), key
    assert not any(c.ok for c in evaluate_probe("garbage", PROBE_LIMITS))
    assert not any(c.ok for c in evaluate_probe("", PROBE_LIMITS))


def test_podman_run_failures_are_reported_without_a_fallback(tmp_path: Path):
    class Failing(ScriptedPodman):
        def start(self, argv: Any) -> Any:
            raise OSError("podman missing")

    backend = PodmanBackend(
        "podman", runner=Failing(ROOTLESS_INFO), uid=1000, gid=1000, probe=False
    )
    spec = RunSpec("agentforge-y", IMAGE, ["ruff"], {}, DEFAULT_ARCHIVE, SandboxLimits(), 100)
    outcome = backend.run(spec)
    assert outcome.status is SandboxStatus.FAILED and outcome.exit_code is None
    assert "podman" in outcome.detail
    assert backend.kill("agentforge-y") is True  # scripted kill answers 0
    assert datetime.now(UTC) >= outcome.started_at
    assert isinstance(threading.current_thread(), threading.Thread)


class FakeProcess:
    """Stands in for the podman client process: sleeps, then exits."""

    def __init__(self, seconds: float, returncode: int) -> None:
        self._deadline = time.monotonic() + seconds
        self._returncode = returncode
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b"partial output\n")
        self.stderr = io.BytesIO()
        self.killed = False

    def poll(self) -> int | None:
        return self._returncode if time.monotonic() >= self._deadline or self.killed else None

    def wait(self, timeout: float | None = None) -> int:
        while self.poll() is None:
            time.sleep(0.01)
        return 137 if self.killed else self._returncode

    def kill(self) -> None:
        self.killed = True


class TimingPodman(ScriptedPodman):
    def __init__(self, process: FakeProcess, *, inspect_exit: str = "0") -> None:
        super().__init__(ROOTLESS_INFO)
        self.process = process
        self.inspect_exit = inspect_exit

    def start(self, argv: Any) -> Any:
        return self.process

    def run(self, argv: Any, *, timeout: float, stdin: bytes | None = None) -> Completed:
        argv = list(argv)
        if argv[1] == "kill":
            self.process.killed = True
            self.calls.append(argv)
            return Completed(0, "", "")
        if argv[1] == "inspect":
            return Completed(0, f"false false {self.inspect_exit} 2026-10-25T00:00:00Z", "")
        return super().run(argv, timeout=timeout, stdin=stdin)


def test_the_runner_enforces_the_wall_clock_and_kills_the_real_container():
    process = FakeProcess(seconds=5.0, returncode=0)
    podman = TimingPodman(process, inspect_exit="137")
    backend = PodmanBackend(
        "podman", runner=podman, uid=1000, gid=1000, probe=False, kill_grace_seconds=1
    )
    spec = RunSpec(
        "agentforge-t", IMAGE, ["ruff"], {}, DEFAULT_ARCHIVE, SandboxLimits(wall_seconds=0.2), 100
    )
    started = time.monotonic()
    outcome = backend.run(spec)
    assert outcome.status is SandboxStatus.TIMED_OUT and time.monotonic() - started < 3
    assert any(call[1] == "kill" and call[-1] == "agentforge-t" for call in podman.calls)
    assert process.killed and outcome.stdout == "partial output\n"


def test_an_engine_side_timeout_is_still_reported_as_a_timeout():
    # podman's --timeout stopped the container (exit -1) right at the deadline.
    process = FakeProcess(seconds=0.25, returncode=255)
    podman = TimingPodman(process, inspect_exit="-1")
    backend = PodmanBackend(
        "podman", runner=podman, uid=1000, gid=1000, probe=False, kill_grace_seconds=1
    )
    spec = RunSpec(
        "agentforge-e", IMAGE, ["ruff"], {}, DEFAULT_ARCHIVE, SandboxLimits(wall_seconds=0.25), 100
    )
    outcome = backend.run(spec)
    assert outcome.status is SandboxStatus.TIMED_OUT
    quick = FakeProcess(seconds=0.01, returncode=0)
    fine = PodmanBackend(
        "podman", runner=TimingPodman(quick, inspect_exit="0"), uid=1000, gid=1000, probe=False
    )
    assert (
        fine.run(
            RunSpec(
                "agentforge-f",
                IMAGE,
                ["ruff"],
                {},
                DEFAULT_ARCHIVE,
                SandboxLimits(wall_seconds=5),
                100,
            )
        ).status
        is SandboxStatus.COMPLETED
    )

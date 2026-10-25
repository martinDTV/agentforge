"""Executed only on a Linux host with Podman rootless (see conftest)."""

import gzip
import io
import tarfile
import threading
import time
import uuid
from pathlib import Path

from agentforge_runner.backend import RunSpec
from agentforge_runner.podman import PodmanBackend
from agentforge_runner.protocol import SandboxLimits, SandboxStatus


def pack(workspace: Path) -> bytes:
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        for path in sorted(p for p in workspace.rglob("*") if p.is_file()):
            tar.add(path, arcname=path.relative_to(workspace).as_posix())
    return buffer.getvalue()


def spec(image: str, workspace: Path, argv: list[str], **limits: object) -> RunSpec:
    return RunSpec(
        container_name=f"agentforge-{uuid.uuid4()}",
        image=image,
        argv=argv,
        env={"PYTHONDONTWRITEBYTECODE": "1", "CI": "1"},
        archive=pack(workspace),
        limits=SandboxLimits(**limits),  # type: ignore[arg-type]
        output_cap=64_000,
    )


def test_ruff_check_runs_inside_the_sandbox(backend: PodmanBackend, image: str, workspace: Path):
    outcome = backend.run(
        spec(
            image,
            workspace,
            ["ruff", "check", "--no-cache", "--output-format", "concise", "--", "src"],
        )
    )
    assert (
        outcome.status is SandboxStatus.COMPLETED and outcome.exit_code == 1
    )  # bad.py has findings
    assert "bad.py" in outcome.stdout and not outcome.stdout_truncated


def test_pytest_sees_no_network_and_a_read_only_root(
    backend: PodmanBackend, image: str, workspace: Path
):
    outcome = backend.run(
        spec(
            image,
            workspace,
            ["python", "-I", "-P", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--", "tests"],
        )
    )
    assert outcome.status is SandboxStatus.COMPLETED and outcome.exit_code == 0, (
        outcome.stdout + outcome.stderr
    )


def test_memory_limit_kills_the_process(backend: PodmanBackend, image: str, workspace: Path):
    # The cgroup limit is memory_bytes + storage_bytes (tmpfs pages are charged
    # to it), so keep the tmpfs small and really touch the pages: a zeroed
    # bytearray is lazily mapped and would never be counted.
    (workspace / "tests" / "test_hog.py").write_text(
        "def test_hog():\n    blob = b'x' * (256 * 1024 * 1024)\n    assert blob\n"
    )
    outcome = backend.run(
        spec(
            image,
            workspace,
            [
                "python",
                "-I",
                "-P",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--",
                "tests/test_hog.py",
            ],
            memory_bytes=128 * 1024 * 1024,
            storage_bytes=16 * 1024 * 1024,
        )
    )
    assert (
        outcome.status is SandboxStatus.KILLED
        or outcome.oom_killed
        or outcome.exit_code not in (0, None)
    ), outcome


def test_wall_clock_timeout_terminates_the_container(
    backend: PodmanBackend, image: str, workspace: Path
):
    (workspace / "tests" / "test_sleep.py").write_text(
        "import time\ndef test_sleep():\n    time.sleep(60)\n"
    )
    started = time.monotonic()
    outcome = backend.run(
        spec(
            image,
            workspace,
            [
                "python",
                "-I",
                "-P",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--",
                "tests/test_sleep.py",
            ],
            wall_seconds=5,
        )
    )
    assert outcome.status is SandboxStatus.TIMED_OUT and time.monotonic() - started < 40


def test_kill_stops_a_running_container(backend: PodmanBackend, image: str, workspace: Path):
    (workspace / "tests" / "test_sleep.py").write_text(
        "import time\ndef test_sleep():\n    time.sleep(60)\n"
    )
    run = spec(
        image,
        workspace,
        [
            "python",
            "-I",
            "-P",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--",
            "tests/test_sleep.py",
        ],
        wall_seconds=120,
    )
    results: list[object] = []
    thread = threading.Thread(target=lambda: results.append(backend.run(run)))
    thread.start()
    time.sleep(5)
    assert backend.kill(run.container_name)
    thread.join(timeout=30)
    assert results and getattr(results[0], "exit_code", 0) not in (0, None)


def test_work_quota_is_enforced_by_the_tmpfs(backend: PodmanBackend, image: str, workspace: Path):
    (workspace / "tests" / "test_fill.py").write_text(
        "def test_fill():\n"
        "    try:\n        open('/work/blob', 'wb').write(b'x' * (40 * 1024 * 1024))\n"
        "    except OSError:\n        return\n"
        "    raise AssertionError('la cuota de /work no se aplicó')\n"
    )
    outcome = backend.run(
        spec(
            image,
            workspace,
            [
                "python",
                "-I",
                "-P",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--",
                "tests/test_fill.py",
            ],
            storage_bytes=16 * 1024 * 1024,
        )
    )
    assert outcome.status is SandboxStatus.COMPLETED and outcome.exit_code == 0, (
        outcome.stdout + outcome.stderr
    )


def test_each_container_gets_its_own_workspace(backend: PodmanBackend, image: str, workspace: Path):
    (workspace / "tests" / "test_marker.py").write_text(
        "import os\n"
        "def test_marker():\n"
        "    assert not os.path.exists('/work/marker')\n"
        "    open('/work/marker', 'w').write('x')\n"
    )
    argv = [
        "python",
        "-I",
        "-P",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--",
        "tests/test_marker.py",
    ]
    first = backend.run(spec(image, workspace, argv))
    second = backend.run(spec(image, workspace, argv))
    assert first.exit_code == 0 and second.exit_code == 0, first.stdout + second.stdout


def test_no_containers_are_left_behind(backend: PodmanBackend):
    """Runs last: every container created above (and the probe) is gone."""
    assert backend.list_containers() == []

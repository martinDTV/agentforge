"""Controlled cleanup failure checks against the installed Runner package.

All Podman operations are intercepted by ScriptedPodman. This script neither
connects to Podman nor reads Runner credentials or configuration.
"""

import io

from agentforge_runner.backend import RunSpec
from agentforge_runner.podman import Completed, PodmanBackend
from agentforge_runner.protocol import SandboxLimits, SandboxStatus


class FinishedProcess:
    def __init__(self) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b"ok\n")
        self.stderr = io.BytesIO()

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int:
        return 0

    def kill(self) -> None:
        raise AssertionError("no se debería matar el proceso simulado")


class ScriptedPodman:
    def start(self, argv: list[str]) -> FinishedProcess:
        assert argv[1] == "run"
        return FinishedProcess()

    def run(self, argv: list[str], *, timeout: float, stdin: bytes | None = None) -> Completed:
        operation = argv[1]
        if operation in {"rm", "ps", "run"}:
            return Completed(125, "", "simulated failure")
        if operation == "inspect":
            return Completed(0, "false false 0 2026-10-26T00:00:00Z", "")
        raise AssertionError(f"operación inesperada: {operation}")


def main() -> None:
    backend = PodmanBackend("podman", runner=ScriptedPodman(), uid=110, gid=110, probe=False)
    assert backend.remove("agentforge-simulated") is False
    try:
        backend.list_containers()
    except RuntimeError:
        pass
    else:
        raise AssertionError("podman ps fallido fue aceptado")
    checks = backend._run_probe("localhost/test@sha256:" + "0" * 64)
    assert any(check.name == "probe:cleanup" and not check.ok for check in checks)
    outcome = backend.run(
        RunSpec(
            container_name="agentforge-simulated",
            image="localhost/test@sha256:" + "0" * 64,
            argv=["ruff", "check", "--", "src"],
            env={},
            archive=b"",
            limits=SandboxLimits(wall_seconds=5),
            output_cap=1024,
        )
    )
    assert outcome.status is SandboxStatus.FAILED
    assert outcome.detail == "container_cleanup_failed"
    print("controlled_cleanup_failures_detected=4")


if __name__ == "__main__":
    main()

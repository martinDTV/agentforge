"""Scripted backend for the runner's own tests: no containers, no processes.

It is **not** evidence of isolation; it exercises the service around a backend.
"""

import threading
from collections.abc import Callable
from datetime import UTC, datetime

from .backend import ContainerState, IsolationReport, RunOutcome, RunSpec
from .protocol import IsolationCheck, SandboxStatus

Outcome = Callable[[RunSpec], RunOutcome]


def completed(
    spec: RunSpec, *, exit_code: int = 0, stdout: str = "ok\n", stderr: str = ""
) -> RunOutcome:
    now = datetime.now(UTC)
    return RunOutcome(
        status=SandboxStatus.COMPLETED,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=False,
        stderr_truncated=False,
        duration_ms=7,
        started_at=now,
        finished_at=now,
    )


class FakeBackend:
    name = "podman-rootless"

    def __init__(self, *, isolation_ok: bool = True, rootless: bool = True) -> None:
        self.isolation_ok = isolation_ok
        self.rootless = rootless
        self.runs: list[RunSpec] = []
        self.killed: list[str] = []
        self.removed: list[str] = []
        self.states: dict[str, ContainerState] = {}  # what ``inspect`` answers
        self.existing: list[str] = []  # what ``list_containers`` answers
        self.outcome: Outcome = completed
        self.hold = threading.Event()  # when set, runs block until ``release`` is set
        self.release = threading.Event()
        self.started = threading.Event()

    def preflight(self, images: dict[str, str]) -> IsolationReport:
        checks = [
            IsolationCheck(name="rootless", ok=self.rootless, detail=str(self.rootless)),
            IsolationCheck(
                name="cgroups_v2", ok=self.isolation_ok, detail="v2" if self.isolation_ok else "v1"
            ),
            IsolationCheck(name="probe:simulated", ok=self.isolation_ok, detail="backend simulado"),
        ]
        return IsolationReport(
            ok=self.isolation_ok and self.rootless,
            rootless=self.rootless,
            backend=self.name,
            checks=checks,
        )

    def run(self, spec: RunSpec) -> RunOutcome:
        self.runs.append(spec)
        self.started.set()
        if self.hold.is_set():
            self.release.wait(timeout=30)
            if spec.container_name in self.killed:
                now = datetime.now(UTC)
                return RunOutcome(
                    status=SandboxStatus.CANCELLED,
                    exit_code=137,
                    stdout="",
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                    duration_ms=1,
                    started_at=now,
                    finished_at=now,
                )
        return self.outcome(spec)

    def kill(self, container_name: str) -> bool:
        self.killed.append(container_name)
        self.release.set()
        state = self.states.get(container_name)
        if state is not None and state.running:
            self.states[container_name] = ContainerState(
                name=container_name,
                running=False,
                exit_code=137,
                oom_killed=False,
                finished_at=datetime.now(UTC),
            )
        return True

    def inspect(self, container_name: str) -> ContainerState | None:
        return self.states.get(container_name)

    def remove(self, container_name: str) -> bool:
        self.removed.append(container_name)
        self.states.pop(container_name, None)
        if container_name in self.existing:
            self.existing.remove(container_name)
        return True

    def list_containers(self) -> list[str]:
        return list(self.existing) + [n for n in self.states if n not in self.existing]

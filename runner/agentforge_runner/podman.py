"""Podman rootless backend.

Every container gets the same non-negotiable shape (``build_run_argv``):

- no network, no ``/etc/hosts`` from the host, read-only root;
- ``/work`` is a **tmpfs sized to ``storage_bytes``** created by the engine:
  the workspace is streamed through stdin and extracted there by a fixed
  bootstrap, so the host disk never holds the untrusted tree, every
  container has its own directory, the quota is enforced by the kernel and
  everything vanishes with the container; ``/tmp`` is a second capped tmpfs;
- tmpfs pages are charged to the memory cgroup, so the cgroup limit is
  ``memory_bytes + storage_bytes`` and the process still gets ``memory_bytes``;
- all capabilities dropped, no new privileges, the runner's own unprivileged
  uid inside the user namespace, pids/CPU limits, an RLIMIT_CPU ceiling and
  a wall-clock timeout enforced by podman *and* by the runner.

``preflight`` proves the shape works on this host by running a probe
container with the same flags and checking, from inside, that the root is
read-only, ``/work`` is a tmpfs of the requested size, there is no network,
no capability is effective, ``no_new_privs`` is set and the cgroup limits
are the ones requested. If anything cannot be proven, no work is accepted.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Protocol

from .backend import ContainerState, IsolationReport, RunOutcome, RunSpec
from .protocol import IsolationCheck, SandboxLimits, SandboxStatus

logger = logging.getLogger(__name__)

CONTAINER_PREFIX = "agentforge-"
REQUIRED_CONTROLLERS = frozenset({"memory", "pids", "cpu"})
BOOTSTRAP_FAILED_MARKER = "AGENTFORGE_BOOTSTRAP_FAILED"
BOOTSTRAP_EXIT = 97
_PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "CONTAINERS_CONF",
)
MiB = 1024 * 1024

# Runs inside the container as PID 1 with ``python -I -c``: extracts the
# archive from stdin into /work with the stdlib "data" filter (a second net
# after the runner's own scan) and replaces itself with the program.
BOOTSTRAP = (
    "import os, sys, tarfile\n"
    "if not hasattr(tarfile, 'data_filter'):\n"
    f"    sys.stderr.write('{BOOTSTRAP_FAILED_MARKER}: tarfile data filter unavailable\\n')\n"
    f"    os._exit({BOOTSTRAP_EXIT})\n"
    "os.umask(0o022)\n"
    "try:\n"
    "    with tarfile.open(fileobj=sys.stdin.buffer, mode='r|gz') as tar:\n"
    "        tar.extractall('/work', filter='data')\n"
    "except Exception as exc:\n"
    f"    sys.stderr.write('{BOOTSTRAP_FAILED_MARKER}: ' + type(exc).__name__ + '\\n')\n"
    f"    os._exit({BOOTSTRAP_EXIT})\n"
    "os.chdir('/work')\n"
    "sys.stdout.flush(); sys.stderr.flush()\n"
    "os.execvp(sys.argv[1], sys.argv[1:])\n"
)

# Runs inside a probe container with the same shape; prints one JSON line.
PROBE = (
    "import json, os, socket\n"
    "r = {}\n"
    "r['uid_unprivileged'] = os.getuid() != 0\n"
    "try:\n    open('/agentforge-probe', 'w'); r['root_readonly'] = False\n"
    "except OSError:\n    r['root_readonly'] = True\n"
    "try:\n    open('/work/probe', 'w').write('x'); r['work_writable'] = True\n"
    "except OSError:\n    r['work_writable'] = False\n"
    "st = os.statvfs('/work'); r['work_bytes'] = st.f_frsize * st.f_blocks\n"
    "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=1); r['network_blocked'] = False\n"
    "except OSError:\n    r['network_blocked'] = True\n"
    "r['interfaces'] = sorted(os.listdir('/sys/class/net'))"
    " if os.path.isdir('/sys/class/net') else []\n"
    "status = open('/proc/self/status').read()\n"
    "caps = [l.split()[1] for l in status.splitlines() if l.startswith('CapEff:')]\n"
    "r['no_capabilities'] = bool(caps) and int(caps[0], 16) == 0\n"
    "nnp = [l.split()[1] for l in status.splitlines() if l.startswith('NoNewPrivs:')]\n"
    "r['no_new_privs'] = nnp == ['1']\n"
    "def cg(name):\n"
    "    try:\n        return open('/sys/fs/cgroup/' + name).read().strip()\n"
    "    except OSError:\n        return None\n"
    "r['memory_max'] = cg('memory.max'); r['pids_max'] = cg('pids.max')\n"
    "r['cpu_max'] = cg('cpu.max')\n"
    "print(json.dumps(r))\n"
)


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str


class Process(Protocol):
    stdin: IO[bytes] | None
    stdout: IO[bytes] | None
    stderr: IO[bytes] | None

    def wait(self, timeout: float | None = None) -> int: ...

    def poll(self) -> int | None: ...

    def kill(self) -> None: ...


class ProcessRunner(Protocol):
    """How the backend talks to the ``podman`` binary; swapped by tests."""

    def run(
        self, argv: Sequence[str], *, timeout: float, stdin: bytes | None = None
    ) -> Completed: ...

    def start(self, argv: Sequence[str]) -> Process: ...


class SubprocessRunner:
    def __init__(self) -> None:
        self._env = {k: v for k, v in os.environ.items() if k in _PASSTHROUGH_ENV}

    def run(self, argv: Sequence[str], *, timeout: float, stdin: bytes | None = None) -> Completed:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            timeout=timeout,
            env=self._env,
            input=stdin if stdin is not None else b"",
            check=False,
        )
        return Completed(
            completed.returncode,
            completed.stdout.decode("utf-8", errors="replace"),
            completed.stderr.decode("utf-8", errors="replace"),
        )

    def start(self, argv: Sequence[str]) -> Process:
        return subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env,
        )


def container_flags(
    name: str,
    limits: SandboxLimits,
    env: dict[str, str],
    *,
    uid: int,
    gid: int,
    engine_timeout_seconds: float = 5.0,
) -> list[str]:
    """Everything between ``podman run`` and the image. Nothing here comes from
    the request except the numeric limits (already capped) and the
    allowlisted environment. The runner kills the container itself at
    ``wall_seconds``; podman's ``--timeout`` is a backstop a few seconds later."""
    cpu = max(1, int(limits.cpu_seconds))
    memory = limits.memory_bytes + limits.storage_bytes  # tmpfs pages are charged to the cgroup
    engine_timeout = max(1, int(limits.wall_seconds + engine_timeout_seconds))
    flags = [
        "--name",
        name,
        "--rm=false",
        "--interactive",
        "--network",
        "none",
        "--no-hosts",
        "--read-only",
        "--read-only-tmpfs=false",
        "--mount",
        f"type=tmpfs,dst=/work,tmpfs-size={limits.storage_bytes},tmpfs-mode=1777,noexec,nosuid,nodev",
        "--mount",
        f"type=tmpfs,dst=/tmp,tmpfs-size={limits.storage_bytes},tmpfs-mode=1777,noexec,nosuid,nodev",
        "--workdir",
        "/work",
        "--userns=keep-id",
        "--user",
        f"{uid}:{gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(limits.pids),
        "--memory",
        str(memory),
        "--memory-swap",
        str(memory),
        "--cpus",
        "1.0",
        "--ulimit",
        f"cpu={cpu}:{cpu}",
        "--ulimit",
        "nofile=1024:1024",
        "--timeout",
        str(engine_timeout),
        "--log-driver",
        "none",
        "--env-host=false",
        "--pull",
        "never",
        "--env",
        "HOME=/tmp",
        "--env",
        "TMPDIR=/tmp",
    ]
    for key in sorted(env):
        flags += ["--env", f"{key}={env[key]}"]
    return flags


def build_run_argv(
    podman: str, spec: RunSpec, *, uid: int, gid: int, grace_seconds: float = 5.0
) -> list[str]:
    return [
        podman,
        "run",
        *container_flags(
            spec.container_name,
            spec.limits,
            spec.env,
            uid=uid,
            gid=gid,
            engine_timeout_seconds=grace_seconds,
        ),
        spec.image,
        "python",
        "-I",
        "-c",
        BOOTSTRAP,
        *spec.argv,
    ]


def build_probe_argv(
    podman: str, image: str, limits: SandboxLimits, *, uid: int, gid: int, name: str
) -> list[str]:
    """Same shape as a workload (``--rm=false`` included): the caller removes
    the container explicitly, exactly like it does after a workload."""
    return [
        podman,
        "run",
        *container_flags(name, limits, {}, uid=uid, gid=gid),
        image,
        "python",
        "-I",
        "-c",
        PROBE,
    ]


PROBE_LIMITS = SandboxLimits(
    cpu_seconds=10, memory_bytes=128 * MiB, pids=32, wall_seconds=30, storage_bytes=32 * MiB
)


def evaluate_probe(stdout: str, limits: SandboxLimits) -> list[IsolationCheck]:
    """Turn the probe's JSON into checks; anything missing counts as failed."""
    try:
        data = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (ValueError, IndexError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    memory = str(limits.memory_bytes + limits.storage_bytes)
    checks = [
        IsolationCheck(
            name="probe:uid_unprivileged",
            ok=bool(data.get("uid_unprivileged")),
            detail=str(data.get("uid_unprivileged")),
        ),
        IsolationCheck(
            name="probe:root_readonly",
            ok=bool(data.get("root_readonly")),
            detail=str(data.get("root_readonly")),
        ),
        IsolationCheck(
            name="probe:work_writable",
            ok=bool(data.get("work_writable")),
            detail=str(data.get("work_writable")),
        ),
        IsolationCheck(
            name="probe:work_quota",
            ok=isinstance(data.get("work_bytes"), int)
            and abs(int(data["work_bytes"]) - limits.storage_bytes) <= limits.storage_bytes // 8,
            detail=f"{data.get('work_bytes')} bytes (esperados {limits.storage_bytes})",
        ),
        IsolationCheck(
            name="probe:network_blocked",
            ok=bool(data.get("network_blocked")) and set(data.get("interfaces") or []) <= {"lo"},
            detail=f"interfaces={data.get('interfaces')}",
        ),
        IsolationCheck(
            name="probe:no_capabilities",
            ok=bool(data.get("no_capabilities")),
            detail=str(data.get("no_capabilities")),
        ),
        IsolationCheck(
            name="probe:no_new_privs",
            ok=bool(data.get("no_new_privs")),
            detail=str(data.get("no_new_privs")),
        ),
        IsolationCheck(
            name="probe:memory_max",
            ok=str(data.get("memory_max")) == memory,
            detail=f"{data.get('memory_max')} (esperado {memory})",
        ),
        IsolationCheck(
            name="probe:pids_max",
            ok=str(data.get("pids_max")) == str(limits.pids),
            detail=f"{data.get('pids_max')} (esperado {limits.pids})",
        ),
    ]
    return checks


class PodmanBackend:
    name = "podman-rootless"

    def __init__(
        self,
        binary: str = "podman",
        *,
        runner: ProcessRunner | None = None,
        kill_grace_seconds: float = 5.0,
        uid: int | None = None,
        gid: int | None = None,
        probe: bool = True,
    ) -> None:
        self._podman = binary
        self._runner: ProcessRunner = runner or SubprocessRunner()
        self._grace = kill_grace_seconds
        self._uid = os.getuid() if uid is None else uid
        self._gid = os.getgid() if gid is None else gid
        self._probe = probe

    # -- preflight ------------------------------------------------------------------------

    def preflight(self, images: dict[str, str]) -> IsolationReport:
        checks: list[IsolationCheck] = []
        binary = shutil.which(self._podman) if not os.path.isabs(self._podman) else self._podman
        if binary is None or not os.path.exists(binary):
            checks.append(IsolationCheck(name="podman", ok=False, detail="binario no encontrado"))
            return IsolationReport(ok=False, rootless=False, backend=self.name, checks=checks)
        checks.append(
            IsolationCheck(name="unprivileged_user", ok=self._uid != 0, detail=f"uid {self._uid}")
        )
        try:
            info = self._runner.run([self._podman, "info", "--format", "json"], timeout=30)
            if info.returncode != 0:
                import logging

                logging.getLogger("agentforge_runner").error(
                    "podman info failed: exit=%s stderr=%s",
                    info.returncode,
                    info.stderr[-500:],
                )
            data = json.loads(info.stdout) if info.returncode == 0 else {}
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            checks.append(IsolationCheck(name="podman_info", ok=False, detail=type(exc).__name__))
            return IsolationReport(ok=False, rootless=False, backend=self.name, checks=checks)
        host = data.get("host", {}) if isinstance(data, dict) else {}
        rootless = bool(host.get("security", {}).get("rootless", False))
        checks.append(IsolationCheck(name="rootless", ok=rootless, detail=str(rootless)))
        cgroup_version = str(host.get("cgroupVersion", ""))
        checks.append(
            IsolationCheck(
                name="cgroups_v2", ok=cgroup_version == "v2", detail=cgroup_version or "?"
            )
        )
        controllers = set(host.get("cgroupControllers", []) or [])
        missing = sorted(REQUIRED_CONTROLLERS - controllers)
        checks.append(
            IsolationCheck(
                name="cgroup_controllers",
                ok=not missing,
                detail="faltan: " + ", ".join(missing) if missing else "memory, pids, cpu",
            )
        )
        version = str(data.get("version", {}).get("Version", "")) if isinstance(data, dict) else ""
        checks.append(
            IsolationCheck(
                name="podman_version", ok=_at_least(version, (4, 0)), detail=version or "?"
            )
        )
        for profile, reference in sorted(images.items()):
            exists = self._runner.run([self._podman, "image", "exists", reference], timeout=30)
            checks.append(
                IsolationCheck(
                    name=f"image:{profile}",
                    ok=exists.returncode == 0,
                    detail=reference.split("@", 1)[-1][:19]
                    if exists.returncode == 0
                    else "ausente",
                )
            )
        if all(check.ok for check in checks) and self._probe:
            checks.extend(self._run_probe(next(iter(sorted(images.values())))))
        ok = all(check.ok for check in checks)
        return IsolationReport(ok=ok, rootless=rootless, backend=self.name, checks=checks)

    def _run_probe(self, image: str) -> list[IsolationCheck]:
        name = f"{CONTAINER_PREFIX}probe-{os.getpid()}-{int(time.time())}"
        argv = build_probe_argv(
            self._podman, image, PROBE_LIMITS, uid=self._uid, gid=self._gid, name=name
        )
        try:
            completed = self._runner.run(argv, timeout=PROBE_LIMITS.wall_seconds + 30, stdin=b"")
        except (OSError, subprocess.TimeoutExpired) as exc:
            removed = self.remove(name)
            return [
                IsolationCheck(name="probe", ok=False, detail=type(exc).__name__),
                IsolationCheck(name="probe:cleanup", ok=removed, detail="podman rm"),
            ]
        removed = self.remove(name)
        cleanup = IsolationCheck(name="probe:cleanup", ok=removed, detail="podman rm")
        if completed.returncode != 0:
            return [
                IsolationCheck(
                    name="probe",
                    ok=False,
                    detail=f"exit {completed.returncode}: {completed.stderr[-200:]}",
                ),
                cleanup,
            ]
        return [*evaluate_probe(completed.stdout, PROBE_LIMITS), cleanup]

    # -- execution ------------------------------------------------------------------------

    def run(self, spec: RunSpec) -> RunOutcome:
        argv = build_run_argv(
            self._podman, spec, uid=self._uid, gid=self._gid, grace_seconds=self._grace
        )
        started_at = datetime.now(UTC)
        t0 = time.monotonic()
        try:
            process = self._runner.start(argv)
        except OSError as exc:
            return self._failed(started_at, t0, f"no se pudo lanzar podman: {type(exc).__name__}")
        feeder = threading.Thread(target=_feed_stdin, args=(process, spec.archive), daemon=True)
        feeder.start()
        out = _Collector(process.stdout, spec.output_cap)
        err = _Collector(process.stderr, spec.output_cap)
        timed_out = threading.Event()

        def enforce_wall() -> None:
            if process.poll() is None:
                timed_out.set()
                self.kill(spec.container_name)
                try:
                    process.wait(timeout=self._grace)
                except Exception:  # the podman client is stuck: last resort
                    process.kill()

        # The runner owns the wall clock: it kills at wall_seconds and only
        # then does podman's own --timeout act as a backstop.
        timer = threading.Timer(spec.limits.wall_seconds, enforce_wall)
        timer.daemon = True
        timer.start()
        try:
            returncode = process.wait()
        finally:
            timer.cancel()
        feeder.join(timeout=5)
        out.join()
        err.join()
        elapsed = time.monotonic() - t0
        duration_ms = int(elapsed * 1000)
        finished_at = datetime.now(UTC)
        state = self.inspect(spec.container_name)
        removed = self.remove(spec.container_name)
        oom_killed = state.oom_killed if state is not None else False
        inspected_exit = state.exit_code if state is not None else None
        exit_code = inspected_exit if inspected_exit is not None else returncode
        # The engine stopped it at its own deadline (exit -1 or SIGKILL) just
        # before our timer could: still a timeout, never a completion.
        engine_timed_out = elapsed >= spec.limits.wall_seconds * 0.95 and (
            inspected_exit in (-1, 137) or returncode in (137, 255)
        )
        if not removed:
            status = SandboxStatus.FAILED
        elif timed_out.is_set() or engine_timed_out:
            status = SandboxStatus.TIMED_OUT
        elif oom_killed:
            status = SandboxStatus.KILLED
        elif returncode == 125 or (
            exit_code == BOOTSTRAP_EXIT and BOOTSTRAP_FAILED_MARKER in err.text
        ):
            status = SandboxStatus.FAILED
        else:
            status = SandboxStatus.COMPLETED
        detail = (
            "container_cleanup_failed"
            if not removed
            else (err.text[-500:] if status is SandboxStatus.FAILED else "")
        )
        return RunOutcome(
            status=status,
            exit_code=None if status is SandboxStatus.FAILED else exit_code,
            stdout=out.text,
            stderr=err.text,
            stdout_truncated=out.truncated,
            stderr_truncated=err.truncated,
            duration_ms=duration_ms,
            started_at=started_at,
            finished_at=finished_at,
            oom_killed=oom_killed,
            detail=detail,
        )

    def kill(self, container_name: str) -> bool:
        try:
            completed = self._runner.run(
                [self._podman, "kill", "--signal", "KILL", container_name], timeout=30
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def inspect(self, container_name: str) -> ContainerState | None:
        try:
            completed = self._runner.run(
                [
                    self._podman,
                    "inspect",
                    "--format",
                    "{{.State.Running}} {{.State.OOMKilled}} {{.State.ExitCode}} "
                    "{{.State.FinishedAt}}",
                    container_name,
                ],
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        parts = completed.stdout.split(None, 3)
        if len(parts) < 3:
            return None
        running = parts[0].lower() == "true"
        oom = parts[1].lower() == "true"
        try:
            exit_code: int | None = int(parts[2])
        except ValueError:
            exit_code = None
        finished = _parse_time(parts[3]) if len(parts) == 4 else None
        return ContainerState(
            name=container_name,
            running=running,
            exit_code=None if running else exit_code,
            oom_killed=oom,
            finished_at=finished,
        )

    def remove(self, container_name: str) -> bool:
        try:
            removed = self._runner.run(
                [self._podman, "rm", "-f", "--volumes", container_name], timeout=30
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "No se pudo eliminar el contenedor %s: %s", container_name, type(exc).__name__
            )
            return False
        if removed.returncode != 0:
            logger.warning(
                "No se pudo eliminar el contenedor %s: podman rm exit=%d",
                container_name,
                removed.returncode,
            )
            return False
        return True

    def list_containers(self) -> list[str]:
        try:
            listed = self._runner.run(
                [
                    self._podman,
                    "ps",
                    "-a",
                    "--filter",
                    f"name=^{CONTAINER_PREFIX}",
                    "--format",
                    "{{.Names}}",
                ],
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("no se pudieron enumerar los contenedores de AgentForge") from exc
        if listed.returncode != 0:
            raise RuntimeError(
                "no se pudieron enumerar los contenedores de AgentForge: "
                f"podman ps exit={listed.returncode}"
            )
        return [n for n in listed.stdout.split() if n.startswith(CONTAINER_PREFIX)]

    # -- internals ------------------------------------------------------------------------

    @staticmethod
    def _failed(started_at: datetime, t0: float, detail: str) -> RunOutcome:
        return RunOutcome(
            status=SandboxStatus.FAILED,
            exit_code=None,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=int((time.monotonic() - t0) * 1000),
            started_at=started_at,
            finished_at=datetime.now(UTC),
            detail=detail,
        )


def _feed_stdin(process: Process, data: bytes) -> None:
    stream = process.stdin
    if stream is None:
        return
    try:
        stream.write(data)
        stream.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


class _Collector:
    """Drains a pipe in a thread, keeping at most ``cap`` bytes."""

    def __init__(self, stream: IO[bytes] | None, cap: int) -> None:
        self._stream = stream
        self._cap = cap
        self._chunks: list[bytes] = []
        self._kept = 0
        self.truncated = False
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        if self._stream is None:
            return
        while True:
            chunk = self._stream.read(65536)
            if not chunk:
                break
            if self._kept < self._cap:
                room = self._cap - self._kept
                self._chunks.append(chunk[:room])
                self._kept += min(len(chunk), room)
                if len(chunk) > room:
                    self.truncated = True
            else:
                self.truncated = True

    def join(self) -> None:
        self._thread.join(timeout=30)

    @property
    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


def _at_least(version: str, minimum: tuple[int, int]) -> bool:
    try:
        parts = tuple(int(p) for p in version.split("-")[0].split(".")[:2])
    except ValueError:
        return False
    return len(parts) == 2 and parts >= minimum


def _parse_time(value: str) -> datetime | None:
    value = value.strip()
    if not value or value.startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")[:32])
    except ValueError:
        return None

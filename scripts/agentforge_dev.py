"""Start, stop and inspect the local AgentForge stack from a MacBook, in one command.

    uv run python -m scripts.agentforge_dev up         # API (+ notification worker) in background
    uv run python -m scripts.agentforge_dev status     # processes, health, migrations, outbox
    uv run python -m scripts.agentforge_dev logs       # tail the API log
    uv run python -m scripts.agentforge_dev down       # stop what `up` started

The API survives closing the terminal: it runs detached (``setsid``/``nohup``)
with its pid and log under ``.local/run/``. Nothing is installed as a system
service; a reboot stops it and ``up`` starts it again. Settings come from
``.env``; secrets never pass through this script.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / ".local" / "run"
PID_FILE = RUN_DIR / "api.pid"
LOG_FILE = RUN_DIR / "api.log"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8011


def _pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def _health(port: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"http://{DEFAULT_HOST}:{port}/health/ready", timeout=3) as r:  # noqa: S310
            body = json.loads(r.read().decode() or "{}")
            return {"http": r.status, **{k: v for k, v in body.items() if isinstance(v, str)}}
    except Exception as exc:  # noqa: BLE001
        return {"http": 0, "error": type(exc).__name__}


def _alembic_current() -> str:
    out = subprocess.run(
        ["uv", "run", "alembic", "current"], cwd=ROOT, capture_output=True, text=True
    )
    lines = [line for line in out.stdout.splitlines() if line and not line.startswith("INFO")]
    if lines:
        return lines[-1]
    return _first_problem(out.stderr)


def _first_problem(stderr: str) -> str:
    """The first error line of a failed subprocess (never a dump of its environment)."""
    problems = [line for line in stderr.splitlines() if "rror" in line]
    return (problems[0] if problems else stderr.strip()[-200:] or "unknown")[:200]


def up(port: int) -> int:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if _pid() is not None:
        print(json.dumps({"status": "already_running", "pid": _pid(), "health": _health(port)}))
        return 0
    current = _alembic_current()
    if "(head)" not in current:
        print(json.dumps({"status": "refused", "reason": "migrations_pending", "alembic": current}))
        print(
            "Ejecuta `uv run alembic upgrade head` (paso de despliegue) y repite.", file=sys.stderr
        )
        return 2
    log = open(LOG_FILE, "ab")  # noqa: SIM115 - handed to the child
    proc = subprocess.Popen(  # noqa: S603 - argv built here
        [
            "uv",
            "run",
            "uvicorn",
            "app.main:app",
            "--host",
            DEFAULT_HOST,
            "--port",
            str(port),
            "--log-level",
            "info",
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # survives the terminal that started it
    )
    PID_FILE.write_text(str(proc.pid))
    for _ in range(40):
        time.sleep(0.5)
        if _health(port)["http"] == 200:
            break
        if proc.poll() is not None:
            print(json.dumps({"status": "exited", "code": proc.returncode, "log": str(LOG_FILE)}))
            return 1
    print(
        json.dumps(
            {
                "status": "running",
                "pid": proc.pid,
                "port": port,
                "health": _health(port),
                "log": str(LOG_FILE),
            }
        )
    )
    return 0 if _health(port)["http"] == 200 else 1


def down() -> int:
    pid = _pid()
    if pid is None:
        print(json.dumps({"status": "not_running"}))
        PID_FILE.unlink(missing_ok=True)
        return 0
    os.killpg(os.getpgid(pid), signal.SIGTERM)
    for _ in range(40):
        time.sleep(0.5)
        if _pid() is None:
            break
    PID_FILE.unlink(missing_ok=True)
    print(json.dumps({"status": "stopped", "pid": pid}))
    return 0


def _worker_state() -> str:
    """What the current log says about the in-process notification worker."""
    try:
        text = LOG_FILE.read_text(errors="replace")
    except OSError:
        return "unknown"
    started = text.rfind("notification worker started")
    stopped = text.rfind("Application shutdown complete")
    if started < 0:
        return "not started (AGENTFORGE_EMAIL_ENABLED=false?)"
    return "running" if started > stopped else "stopped"


def status(port: int) -> int:
    pid = _pid()
    report = {
        "api": {"pid": pid, "running": pid is not None, "health": _health(port) if pid else None},
        "alembic": _alembic_current(),
        "worker": (_health(port).get("notifications") if pid else None) or _worker_state(),
        "log": str(LOG_FILE),
    }
    try:
        out = subprocess.run(
            ["uv", "run", "python", "-m", "scripts.notifications", "status"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        report["outbox"] = (
            json.loads(out.stdout)
            if out.returncode == 0 and out.stdout.strip()
            else {"error": _first_problem(out.stderr)}
        )
    except Exception as exc:  # noqa: BLE001
        report["outbox"] = {"error": type(exc).__name__}
    print(json.dumps(report, indent=1))
    return 0 if pid is not None else 1


def logs(lines: int) -> int:
    if not LOG_FILE.exists():
        print("sin log todavía")
        return 1
    print("".join(LOG_FILE.read_text(errors="replace").splitlines(keepends=True)[-lines:]))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("up", "status"):
        p = sub.add_parser(name)
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("down")
    tail = sub.add_parser("logs")
    tail.add_argument("-n", type=int, default=60)
    args = parser.parse_args(argv)
    if args.command == "up":
        return up(args.port)
    if args.command == "down":
        return down()
    if args.command == "status":
        return status(args.port)
    return logs(args.n)


if __name__ == "__main__":
    sys.exit(main())

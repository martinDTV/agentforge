"""``agentforge-runner`` command line: serve, preflight, token."""

import argparse
import json
import sys

from .auth import generate_token, token_digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentforge-runner")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="arranca el servicio con la configuración RUNNER_*")
    sub.add_parser(
        "preflight", help="comprueba el aislamiento (podman rootless, cgroups v2, imágenes)"
    )
    sub.add_parser("token", help="genera un token nuevo y su hash para RUNNER_TOKEN_SHA256")
    args = parser.parse_args(argv)
    if args.command == "token":
        token = generate_token()
        print(json.dumps({"token": token, "token_sha256": token_digest(token)}, indent=2))
        print(
            "Guarda el token en la API (SANDBOX_RUNNER_TOKEN) y solo el hash aquí.", file=sys.stderr
        )
        return 0
    from .config import RunnerSettings
    from .podman import PodmanBackend

    settings = RunnerSettings()  # env-driven
    backend = PodmanBackend(settings.podman_binary, kill_grace_seconds=settings.kill_grace_seconds)
    if args.command == "preflight":
        report = backend.preflight(settings.images)
        for check in report.checks:
            print(f"{'OK ' if check.ok else 'FAIL'} {check.name}: {check.detail}")
        print("ready" if report.ok else "NOT READY: no se aceptará trabajo")
        return 0 if report.ok else 1
    import uvicorn

    from .service import create_runner_app
    from .store import ExecutionStore

    store = ExecutionStore(settings.state_dir / "executions.sqlite3")
    app = create_runner_app(settings, backend, store)
    uvicorn.run(
        app, host=settings.bind_host, port=settings.port, log_level="info", access_log=False
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

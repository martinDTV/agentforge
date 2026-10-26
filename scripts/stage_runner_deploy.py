"""Build, on the operator's machine, the staging directory for a Runner file deployment.

    uv run python -m scripts.stage_runner_deploy --out /tmp/agentforge-runner-deploy \\
        --file protocol.py --file programs.py \\
        --selftest runner_programs_selftest.py --marker programs_selftest_ok=true

It copies the files from the working tree, records their SHA-256 (``desired``)
next to the SHA-256 of the committed version (``base``, what is installed on
the VM unless the manifest says otherwise), lists untouched modules the change
relies on (``compat``) and prints the exact commands for the operator. It never
connects anywhere and never reads credentials.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "runner/agentforge_runner"
SCRIPTS = REPO / "scripts"
REMOTE_PYTHON = "/home/agentforge-runner/app/.venv/bin/python"
RUNUSER = (
    "/usr/sbin/runuser -u agentforge-runner -- /usr/bin/env HOME=/home/agentforge-runner "
    "XDG_RUNTIME_DIR=/run/user/110 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/110/bus"
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def committed(path: Path, ref: str) -> bytes:
    relative = path.relative_to(REPO).as_posix()
    return subprocess.run(
        ["git", "show", f"{ref}:{relative}"], cwd=REPO, check=True, capture_output=True
    ).stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--file", action="append", required=True, help="módulo del paquete")
    parser.add_argument("--compat", action="append", default=[], help="módulo que no cambia")
    parser.add_argument("--selftest", required=True, help="script en scripts/")
    parser.add_argument("--marker", required=True, help="línea que debe imprimir el self-test")
    parser.add_argument("--base-ref", default="HEAD", help="revisión instalada en la VM")
    args = parser.parse_args(argv)
    out: Path = args.out
    if out.exists() and any(out.iterdir()):
        print(f"el directorio {out} no está vacío", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o755)
    files = list(dict.fromkeys(args.file))
    manifest: dict[str, Any] = {
        "files": files,
        "base": {},
        "desired": {},
        "compat": {},
        "selftest": {},
    }
    for name in files:
        source = PACKAGE / name
        manifest["base"][name] = digest(committed(source, args.base_ref))
        manifest["desired"][name] = digest(source.read_bytes())
        shutil.copyfile(source, out / name)
        (out / name).chmod(0o644)
    for name in args.compat:
        manifest["compat"][name] = digest((PACKAGE / name).read_bytes())
    selftest = SCRIPTS / args.selftest
    shutil.copyfile(selftest, out / args.selftest)
    (out / args.selftest).chmod(0o644)
    manifest["selftest"] = {
        "name": args.selftest,
        "sha256": digest(selftest.read_bytes()),
        "marker": args.marker,
    }
    shutil.copyfile(SCRIPTS / "deploy_runner_files.py", out / "deploy_runner_files.py")
    (out / "deploy_runner_files.py").chmod(0o644)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out / "manifest.json").chmod(0o644)
    unchanged = [n for n in files if manifest["base"][n] == manifest["desired"][n]]
    print(json.dumps(manifest, indent=2, sort_keys=True))
    if unchanged:
        print(f"aviso: sin cambios respecto a {args.base_ref}: {unchanged}", file=sys.stderr)
    remote = f"/tmp/{out.name}"
    print(
        "\n# 1) copiar la preparación a la VM (usuario SSH, no el del runner):\n"
        f"scp -r {out} agentforge-runner:{remote}\n"
        f"ssh agentforge-runner 'chmod 0755 {remote} && chmod 0644 {remote}/*'\n"
        "# 2) en una sesión root de la VM, una sola invocación como UID 110:\n"
        f"cd /tmp && {RUNUSER} {REMOTE_PYTHON} {remote}/deploy_runner_files.py install\n"
        "# 3) si hiciera falta volver atrás (nombre del respaldo impreso en el paso 2):\n"
        f"cd /tmp && {RUNUSER} {REMOTE_PYTHON} {remote}/deploy_runner_files.py rollback <respaldo>",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Install reviewed source files of the Runner package as the service user.

Generalisation of the one-off installer used for the container-cleanup fix.
The operator stages this script, ``manifest.json``, the files to install and
a self-test script in a directory owned by the SSH user, then runs exactly one
supervised invocation as ``agentforge-runner`` (UID 110) from ``/tmp``::

    runuser -u agentforge-runner -- env HOME=/home/agentforge-runner \\
        XDG_RUNTIME_DIR=/run/user/110 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/110/bus \\
        /home/agentforge-runner/app/.venv/bin/python <stage>/deploy_runner_files.py install

The manifest (built locally by ``scripts/stage_runner_deploy.py``) names the
files and their SHA-256 before (``base``) and after (``desired``) the change,
the untouched modules the change relies on (``compat``) and the self-test
with the exact line it must print. Nothing is replaced unless every installed
file matches ``base``; the service unit and ``runner.env`` are checked for
accidental changes but never written; a failed restart or self-test rolls
back; no credential is read, printed or passed as an argument.
"""

import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUNNER_USER = "agentforge-runner"
RUNNER_UID = 110
APP = Path("/home/agentforge-runner/app")
PACKAGE = APP / "runner/agentforge_runner"
BACKUP_ROOT = Path("/home/agentforge-runner/.local/share/agentforge-runner/deploy-backups")
UNIT = Path("/home/agentforge-runner/.config/systemd/user/agentforge-runner.service")
ENV = Path("/home/agentforge-runner/.config/agentforge-runner/runner.env")
SERVICE = "agentforge-runner.service"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[a-z_]+\.py\Z")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_file(path: Path, owner: int) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
        raise ValueError(f"tipo o propietario inesperado: {path.name}")
    if info.st_mode & 0o022:
        raise ValueError(f"archivo modificable por grupo u otros: {path.name}")
    return path.read_bytes()


def safe_config(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, RUNNER_UID} or info.st_mode & 0o022:
        raise ValueError(f"configuración insegura: {path.name}")
    return path.read_bytes()


def systemctl(action: str, *, timeout: int = 40) -> bool:
    result = subprocess.run(
        ["/usr/bin/systemctl", "--user", action, SERVICE],
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return result.returncode == 0


def active() -> bool:
    for _ in range(20):
        if systemctl("is-active", timeout=10):
            return True
        time.sleep(0.5)
    return False


def atomic_replace(path: Path, data: bytes) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != RUNNER_UID:
        raise ValueError(f"destino inseguro: {path.name}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_manifest(stage: Path, owner: int) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(safe_file(stage / "manifest.json", owner))
    if set(manifest) != {"files", "base", "desired", "compat", "selftest"}:
        raise ValueError("manifest inválido")
    files = manifest["files"]
    if (
        not isinstance(files, list)
        or not files
        or len(files) > 8
        or any(not isinstance(n, str) or not NAME.fullmatch(n) for n in files)
        or len(set(files)) != len(files)
    ):
        raise ValueError("lista de archivos inválida")
    if set(manifest["base"]) != set(files) or set(manifest["desired"]) != set(files):
        raise ValueError("digests base/desired no coinciden con la lista de archivos")
    compat = manifest["compat"]
    if not isinstance(compat, dict) or any(not NAME.fullmatch(n) or n in files for n in compat):
        raise ValueError("dependencias de compatibilidad inválidas")
    selftest = manifest["selftest"]
    if (
        not isinstance(selftest, dict)
        or set(selftest) != {"name", "sha256", "marker"}
        or not NAME.fullmatch(str(selftest["name"]))
        or not isinstance(selftest["marker"], str)
        or not selftest["marker"]
    ):
        raise ValueError("self-test inválido")
    for group in (manifest["base"], manifest["desired"], compat, {"s": selftest["sha256"]}):
        if any(not isinstance(v, str) or not HEX64.fullmatch(v) for v in group.values()):
            raise ValueError("digest inválido")
    return manifest


def verify_runtime(files: list[str]) -> None:
    python = APP / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("no se encontró el Python del entorno instalado")
    unit = subprocess.run(
        ["/usr/bin/systemctl", "--user", "show", "-p", "ExecStart", "--value", SERVICE],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    ).stdout
    if str(APP / ".venv/bin/agentforge-runner") not in unit:
        raise ValueError("ExecStart no apunta al entorno del proyecto esperado")
    modules = [name[:-3] for name in files]
    code = "; ".join(f"import agentforge_runner.{m} as m{i}" for i, m in enumerate(modules))
    code += "; " + "; ".join(f"print(m{i}.__file__)" for i in range(len(modules)))
    origins = subprocess.run(
        [str(python), "-c", code], capture_output=True, text=True, timeout=15, check=True
    ).stdout.splitlines()
    if origins != [str(PACKAGE / name) for name in files]:
        raise ValueError("el servicio no importa los archivos de destino esperados")


def run_selftest(stage: Path, selftest: dict[str, Any]) -> None:
    completed = subprocess.run(
        [str(APP / ".venv/bin/python"), str(stage / selftest["name"])],
        cwd="/tmp",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0 or selftest["marker"] not in completed.stdout:
        raise RuntimeError(
            f"self-test fallido (exit {completed.returncode}): {completed.stderr[-300:]}"
        )
    print(selftest["marker"], flush=True)


def install() -> None:
    stage = Path(__file__).resolve().parent
    stage_info = stage.lstat()
    if not stat.S_ISDIR(stage_info.st_mode) or stage_info.st_mode & 0o022:
        raise ValueError("directorio de preparación inseguro")
    stage_owner = stage_info.st_uid
    if stage_owner in (RUNNER_UID, 0):
        raise ValueError("la preparación debe pertenecer al usuario SSH")
    manifest = load_manifest(stage, stage_owner)
    files: list[str] = manifest["files"]
    if not systemctl("is-active"):
        raise ValueError("el servicio debe estar activo antes del despliegue")
    verify_runtime(files)
    staged = {name: safe_file(stage / name, stage_owner) for name in files}
    selftest = manifest["selftest"]
    if digest(safe_file(stage / selftest["name"], stage_owner)) != selftest["sha256"]:
        raise ValueError("self-test alterado")
    current = {name: safe_file(PACKAGE / name, RUNNER_UID) for name in files}
    for name in files:
        if digest(staged[name]) != manifest["desired"][name]:
            raise ValueError(f"payload alterado: {name}")
        if digest(current[name]) != manifest["base"][name]:
            print(f"baseline_match=False file={name}", flush=True)
            raise ValueError("código instalado diferente; no se reemplazó nada")
    for name, expected in manifest["compat"].items():
        if digest(safe_file(PACKAGE / name, RUNNER_UID)) != expected:
            raise ValueError(f"dependencia instalada diferente: {name}")
    config_before = {path: digest(safe_config(path)) for path in (UNIT, ENV)}
    print("baseline_match=True; payload_verified=True; runtime_path_verified=True", flush=True)
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_info = BACKUP_ROOT.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != RUNNER_UID
        or stat.S_IMODE(root_info.st_mode) != 0o700
    ):
        raise ValueError("directorio de respaldos inseguro")
    backup = BACKUP_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(mode=0o700)
    for name in files:
        target = backup / name
        target.write_bytes(current[name])
        target.chmod(stat.S_IMODE((PACKAGE / name).stat().st_mode))
        if digest(target.read_bytes()) != manifest["base"][name]:
            raise RuntimeError("el respaldo no se pudo verificar")
    (backup / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    (backup / "manifest.json").chmod(0o600)
    print(f"backup_verified=True; backup={backup}", flush=True)
    try:
        for name in files:
            atomic_replace(PACKAGE / name, staged[name])
        if not systemctl("restart") or not active():
            raise RuntimeError("el servicio no inició tras instalar los archivos")
        run_selftest(stage, selftest)
        if any(digest(safe_config(path)) != before for path, before in config_before.items()):
            raise RuntimeError("la configuración cambió durante el despliegue")
    except Exception:
        for name in files:
            atomic_replace(PACKAGE / name, current[name])
        restored = systemctl("restart") and active()
        print(f"install_failed=True; rollback_restored={restored}", flush=True)
        raise
    print("install_applied=True; runner_service_active=True; config_unchanged=True", flush=True)


def rollback(backup_name: str) -> None:
    if not re.fullmatch(r"\d{8}T\d{6}Z", backup_name):
        raise ValueError("nombre de respaldo inválido")
    backup = BACKUP_ROOT / backup_name
    if backup.lstat().st_uid != RUNNER_UID or not backup.is_dir():
        raise ValueError("respaldo inválido")
    manifest = json.loads(safe_file(backup / "manifest.json", RUNNER_UID))
    files: list[str] = manifest["files"]
    original = {name: safe_file(backup / name, RUNNER_UID) for name in files}
    for name in files:
        if digest(original[name]) != manifest["base"][name]:
            raise ValueError("respaldo alterado")
        if digest(safe_file(PACKAGE / name, RUNNER_UID)) != manifest["desired"][name]:
            raise ValueError("el archivo instalado cambió después del despliegue")
    for name in files:
        atomic_replace(PACKAGE / name, original[name])
    if not systemctl("restart") or not active():
        raise RuntimeError("archivos restaurados pero el servicio no inició")
    print("rollback_restored=True; runner_service_active=True", flush=True)


def main() -> int:
    if os.geteuid() != pwd.getpwnam(RUNNER_USER).pw_uid or os.geteuid() != RUNNER_UID:
        raise ValueError("ejecutar exclusivamente como agentforge-runner (UID 110)")
    if len(sys.argv) == 2 and sys.argv[1] == "install":
        install()
    elif len(sys.argv) == 3 and sys.argv[1] == "rollback":
        rollback(sys.argv[2])
    else:
        raise ValueError("uso: deploy_runner_files.py install | rollback <respaldo>")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"deployment_error={type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

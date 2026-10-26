"""One-time, operator-authorized rotation of the Runner's two token hashes.

Stage this script and a mode-0600 JSON file containing only SHA-256 digests in
the admin user's private directory on the VM. The operator runs it once with
``sudo python3`` in an interactive SSH session. It never reads bearer tokens,
prints hashes, changes sudoers, or touches any service other than the Runner.

The script refuses an unexpected path, owner, mode, file type, missing or
duplicate key, or malformed digest before changing the environment file.
"""

import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER_USER = "agentforge-runner"
RUNNER_UID = 110
HASH_FILE_NAME = "hashes.json"
ENV_FILE = Path("/home/agentforge-runner/.config/agentforge-runner/runner.env")
KEYS = ("RUNNER_TOKEN_SHA256", "RUNNER_ADMIN_TOKEN_SHA256")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def read_private_file(path: Path, owner: int) -> tuple[bytes, os.stat_result]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
            raise ValueError("propietario o tipo de archivo inesperado")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("el archivo debe tener modo 0600")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("archivo demasiado grande")
        return data, info
    finally:
        os.close(fd)


def replace_hashes(original: bytes, hashes: dict[str, str]) -> bytes:
    lines = original.decode("utf-8").splitlines(keepends=True)
    seen: dict[str, int] = {key: 0 for key in KEYS}
    updated: list[str] = []
    for line in lines:
        match = re.match(r"^(RUNNER_TOKEN_SHA256|RUNNER_ADMIN_TOKEN_SHA256)=", line)
        if match is None:
            updated.append(line)
            continue
        key = match.group(1)
        seen[key] += 1
        ending = "\r\n" if line.endswith("\r\n") else "\n"
        updated.append(f"{key}={hashes[key]}{ending}")
    if any(count != 1 for count in seen.values()):
        raise ValueError("se requiere exactamente una entrada por token")
    return "".join(updated).encode("utf-8")


def atomic_write(path: Path, content: bytes, info: os.stat_result) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".runner.env.rotation-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def runner_systemctl(action: str) -> bool:
    result = subprocess.run(
        [
            "/usr/sbin/runuser",
            "-u",
            RUNNER_USER,
            "--",
            "/usr/bin/env",
            f"XDG_RUNTIME_DIR=/run/user/{RUNNER_UID}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{RUNNER_UID}/bus",
            "/usr/bin/systemctl",
            "--user",
            action,
            "agentforge-runner.service",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


def apply(admin_user: str, stage_dir: Path) -> None:
    """``admin_user`` is the SSH administrator who staged the hashes (never root or
    the runner); ``stage_dir`` is their private, mode-0700 staging directory."""
    if os.geteuid() != 0:
        raise ValueError("se requiere sudo interactivo")
    admin = pwd.getpwnam(admin_user)
    admin_uid = admin.pw_uid
    if admin_uid in (0, RUNNER_UID):
        raise ValueError("el administrador debe ser un usuario distinto de root y del Runner")
    if not stage_dir.is_absolute() or not stage_dir.is_relative_to(Path(admin.pw_dir)):
        raise ValueError("la preparación debe estar dentro del directorio del administrador")
    hash_file = stage_dir / HASH_FILE_NAME
    runner_uid = pwd.getpwnam(RUNNER_USER).pw_uid
    if runner_uid != RUNNER_UID:
        raise ValueError("UID del Runner inesperado")
    stage_info = stage_dir.lstat()
    if not stat.S_ISDIR(stage_info.st_mode) or stage_info.st_uid != admin_uid:
        raise ValueError("directorio de preparación inesperado")
    if stat.S_IMODE(stage_info.st_mode) != 0o700:
        raise ValueError("el directorio de preparación debe tener modo 0700")
    hash_data, _ = read_private_file(hash_file, admin_uid)
    hashes = json.loads(hash_data)
    if not isinstance(hashes, dict) or set(hashes) != set(KEYS):
        raise ValueError("archivo de hashes inválido")
    if any(
        not isinstance(value, str) or HEX64.fullmatch(value) is None for value in hashes.values()
    ):
        raise ValueError("digest SHA-256 inválido")
    original, info = read_private_file(ENV_FILE, runner_uid)
    updated = replace_hashes(original, hashes)
    if updated == original:
        raise ValueError("los hashes nuevos son idénticos a los anteriores")
    if not runner_systemctl("is-active"):
        raise ValueError("el servicio Runner debe estar activo antes de la rotación")
    atomic_write(ENV_FILE, updated, info)
    if not runner_systemctl("restart") or not runner_systemctl("is-active"):
        atomic_write(ENV_FILE, original, info)
        runner_systemctl("restart")
        raise RuntimeError("reinicio fallido; se restauró la configuración anterior")
    hash_file.unlink()
    print("rotation_applied=True; runner_service_active=True; staged_hashes_removed=True")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--admin-user",
        default=os.environ.get("SUDO_USER"),
        help="usuario SSH administrador que preparó los hashes (por defecto SUDO_USER)",
    )
    parser.add_argument(
        "--stage-dir",
        type=Path,
        default=None,
        help="directorio privado 0700 con hashes.json (por defecto ~admin/.agentforge-rotation)",
    )
    args = parser.parse_args(argv)
    if not args.admin_user:
        raise ValueError("indica --admin-user o ejecuta con sudo")
    stage = args.stage_dir or Path(pwd.getpwnam(args.admin_user).pw_dir) / ".agentforge-rotation"
    apply(args.admin_user, stage)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"rotation_applied=False; error={type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None

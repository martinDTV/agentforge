"""Read-only post-deployment check as the Runner's system user.

Checks source digests, the single user service and Podman's AgentForge
container inventory. It does not read credentials or modify the VM.
"""

import hashlib
import json
import os
import pwd
import stat
import subprocess
from pathlib import Path

APP_PACKAGE = Path("/home/agentforge-runner/app/runner/agentforge_runner")
FILES = ("podman.py", "service.py")


def digest(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 110:
        raise ValueError(f"archivo instalado inesperado: {path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    if os.geteuid() != pwd.getpwnam("agentforge-runner").pw_uid or os.geteuid() != 110:
        raise ValueError("ejecutar como agentforge-runner (UID 110)")
    stage = Path(__file__).resolve().parent
    manifest = json.loads((stage / "manifest.json").read_text())
    matches = {name: digest(APP_PACKAGE / name) == manifest["desired"][name] for name in FILES}
    service = subprocess.run(
        ["/usr/bin/systemctl", "--user", "is-active", "agentforge-runner.service"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    containers = subprocess.run(
        [
            "/usr/bin/podman",
            "ps",
            "-a",
            "--filter",
            "name=^agentforge-",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    count = len([line for line in containers.stdout.splitlines() if line.strip()])
    print("deployed_hashes_match=" + str(all(matches.values())).lower())
    print("runner_service_active=" + str(service.returncode == 0).lower())
    print("podman_ps_ok=" + str(containers.returncode == 0).lower())
    print(f"residual_container_count={count}")
    if not all(matches.values()) or service.returncode != 0 or containers.returncode != 0 or count:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

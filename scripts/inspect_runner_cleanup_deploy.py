"""Read-only review of two installed Runner modules as the service user.

Compares installed source with the local Git baseline staged beside this file.
The report contains only changed lines; lines mentioning credentials or long
opaque values are redacted. It also checks whether the reviewed patch applies
to a temporary copy. No Runner configuration or credential file is opened.
"""

import difflib
import hashlib
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

PACKAGE = Path("/home/agentforge-runner/app/runner/agentforge_runner")
FILES = ("podman.py", "service.py")
SENSITIVE = re.compile(
    r"token|password|passwd|secret|credential|api.key|auth|bearer|private|hash|"
    r"[a-fA-F0-9]{32,}|[A-Za-z0-9+/]{48,}",
    re.IGNORECASE,
)


def read_source(path: Path, owner: int) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
        raise ValueError(f"archivo instalado inesperado: {path.name}")
    return path.read_bytes()


def redacted(line: str) -> str:
    if line.startswith(("---", "+++", "@@")):
        return line
    if SENSITIVE.search(line):
        return line[:1] + "<línea reservada>\n"
    return line


def main() -> None:
    uid = pwd.getpwnam("agentforge-runner").pw_uid
    if os.geteuid() != uid or uid != 110:
        raise ValueError("ejecutar como agentforge-runner (UID 110)")
    stage = Path(__file__).resolve().parent
    report_dir = Path(tempfile.mkdtemp(prefix="agentforge-cleanup-review-", dir="/tmp"))
    temporary = report_dir / "tree"
    package = temporary / "runner/agentforge_runner"
    package.mkdir(parents=True)
    lines: list[str] = []
    for name in FILES:
        baseline = (stage / f"baseline-{name}").read_bytes()
        installed = read_source(PACKAGE / name, uid)
        (package / name).write_bytes(installed)
        lines.append(
            f"{name}: baseline_sha256={hashlib.sha256(baseline).hexdigest()} "
            f"installed_sha256={hashlib.sha256(installed).hexdigest()}\n"
        )
        diff = difflib.unified_diff(
            baseline.decode().splitlines(keepends=True),
            installed.decode().splitlines(keepends=True),
            fromfile=f"baseline/{name}",
            tofile=f"installed/{name}",
        )
        lines.extend(redacted(line) for line in diff)
    result = subprocess.run(
        ["/usr/bin/git", "apply", "--check", str(stage / "cleanup.patch")],
        cwd=temporary,
        capture_output=True,
        timeout=15,
        check=False,
    )
    lines.append(f"reviewed_patch_applies={result.returncode == 0}\n")
    report = report_dir / "comparison.txt"
    report.write_text("".join(lines))
    report.chmod(0o644)
    shutil.rmtree(temporary)
    report_dir.chmod(0o755)
    print(f"review_report={report}")
    print(f"reviewed_patch_applies={result.returncode == 0}")


if __name__ == "__main__":
    main()

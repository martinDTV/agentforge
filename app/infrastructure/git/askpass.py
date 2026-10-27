"""Hand a credential to exactly one Git subprocess, through ``GIT_ASKPASS``.

Git never sees the token in ``argv`` or in the remote URL. For the duration of
one ``fetch`` a private directory (mode 0700) holds a helper executable (0700)
and the secret (0600). The helper is started by Git with the prompt as its
only argument; it answers ``x-access-token`` to a username prompt and the
secret to a password prompt, after checking that the secret file is a regular
file owned by the same user with mode 0600. Everything is removed in
``finally``, whatever Git did. The agents' Git adapter never uses this module:
its environment keeps ``GIT_ASKPASS=false``.
"""

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.git.mirrors import MirrorError

SECRET_ENV = "AGENTFORGE_ASKPASS_FILE"
MAX_SHEBANG = 120  # kernels truncate longer shebang lines

_HELPER_TEMPLATE = """#!{python}
import os
import stat
import sys


def main() -> int:
    prompt = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    path = os.environ.get("{env}")
    if not path or not prompt:
        return 1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return 1
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            return 1
        secret = os.read(fd, 8192).decode("utf-8", "strict").strip()
    finally:
        os.close(fd)
    if prompt.startswith("username"):
        sys.stdout.write("x-access-token\\n")
        return 0
    if prompt.startswith("password"):
        sys.stdout.write(secret + "\\n")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
"""


@dataclass(frozen=True)
class AskpassCredential:
    directory: Path
    helper: Path
    secret_file: Path

    @property
    def env(self) -> dict[str, str]:
        return {"GIT_ASKPASS": str(self.helper), SECRET_ENV: str(self.secret_file)}


def _write_private(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def askpass_credential(token: str, *, base_dir: Path | None = None) -> Iterator[AskpassCredential]:
    """Materialise ``token`` for one Git invocation and clean up afterwards."""
    if not token or "\n" in token or "\x00" in token:
        raise MirrorError("askpass_unavailable", "credencial inválida para GIT_ASKPASS")
    python = sys.executable
    if not python or len(python) + 2 > MAX_SHEBANG or " " in python:
        raise MirrorError(
            "askpass_unavailable", "el intérprete actual no sirve como shebang del ayudante"
        )
    directory = Path(tempfile.mkdtemp(prefix="agentforge-askpass-", dir=base_dir))
    try:
        os.chmod(directory, 0o700)
        helper = directory / "askpass"
        secret_file = directory / "credential"
        _write_private(
            helper, _HELPER_TEMPLATE.format(python=python, env=SECRET_ENV).encode(), 0o700
        )
        _write_private(secret_file, (token + "\n").encode(), 0o600)
        yield AskpassCredential(directory=directory, helper=helper, secret_file=secret_file)
    finally:
        try:
            if secret_file.exists():  # best effort: overwrite before unlinking
                with open(secret_file, "r+b") as stream:
                    stream.write(b"\0" * (len(token) + 1))
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError:
            pass
        shutil.rmtree(directory, ignore_errors=True)

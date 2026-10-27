"""Store the Gmail app password in the local ``.env`` without it touching the
terminal, the shell history, the chat or any log.

    uv run python -m scripts.set_smtp_password            # prompts (hidden input)
    uv run python -m scripts.set_smtp_password --env-file /path/.env

Gmail shows app passwords as four groups of four letters; spaces are removed.
The file is rewritten atomically with mode 0600 and the value is never echoed.
"""

import argparse
import getpass
import json
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

KEY = "AGENTFORGE_SMTP_APP_PASSWORD"
_LINE = re.compile(rf"^\s*#?\s*{KEY}\s*=.*$")


def _normalise(raw: str) -> str:
    value = raw.strip().replace(" ", "")
    if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
        value = value[1:-1]
    return value


def write_password(env_file: Path, value: str) -> None:
    """Replace (or append) the ``KEY=`` line, keeping every other line untouched."""
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    replaced = False
    for index, line in enumerate(lines):
        if _LINE.match(line):
            lines[index] = f"{KEY}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{KEY}={value}")
    env_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=env_file.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.chmod(env_file, 0o600)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        print("Ejecuta este comando en una terminal interactiva (la entrada es oculta).")
        return 2
    first = _normalise(getpass.getpass("Contraseña de aplicación (no se muestra): "))
    second = _normalise(getpass.getpass("Repítela: "))
    if first != second:
        print("Las dos entradas no coinciden; nada se ha escrito.")
        return 1
    if len(first) != 16 or not first.isalpha():
        print("Una contraseña de aplicación de Gmail tiene 16 letras; nada se ha escrito.")
        return 1
    write_password(args.env_file, first)
    print(
        json.dumps(
            {"written": True, "env_file": str(args.env_file), "mode": "0600", "key": KEY},
            ensure_ascii=False,
        )
    )
    print("Comprueba con: uv run python -m scripts.email_smoke   (no envía nada)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

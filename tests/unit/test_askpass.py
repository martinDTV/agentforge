"""``GIT_ASKPASS`` handoff: the credential exists only inside a private directory
for the duration of one Git call, is readable only through a helper that checks
the file's mode, and disappears afterwards."""

import os
import runpy
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app.git.mirrors import MirrorError
from app.infrastructure.git.askpass import SECRET_ENV, askpass_credential

TOKEN = "ghs_0123456789abcdefghijklmnopqrstuvwxyzABCD"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_credential_files_are_private_and_removed_even_on_failure(tmp_path: Path):
    with askpass_credential(TOKEN, base_dir=tmp_path) as credential:
        assert credential.directory.parent == tmp_path
        assert _mode(credential.directory) == 0o700
        assert _mode(credential.helper) == 0o700 and _mode(credential.secret_file) == 0o600
        assert credential.secret_file.read_text() == TOKEN + "\n"
        assert credential.env == {
            "GIT_ASKPASS": str(credential.helper),
            SECRET_ENV: str(credential.secret_file),
        }
        helper_text = credential.helper.read_text()
        assert helper_text.startswith(f"#!{sys.executable}\n") and TOKEN not in helper_text
        kept = credential.directory
    assert not kept.exists() and list(tmp_path.iterdir()) == []
    with pytest.raises(RuntimeError), askpass_credential(TOKEN, base_dir=tmp_path) as credential:
        kept = credential.directory
        raise RuntimeError("git blew up")
    assert not kept.exists() and list(tmp_path.iterdir()) == []


def test_invalid_tokens_are_refused_before_anything_touches_disk(tmp_path: Path):
    for bad in ("", "with\nnewline", "nul\x00byte"):
        with pytest.raises(MirrorError) as exc, askpass_credential(bad, base_dir=tmp_path):
            pass
        assert exc.value.code == "askpass_unavailable"
    assert list(tmp_path.iterdir()) == []


def _run_helper(helper: Path, prompt: str, env: dict[str, str], capsys) -> tuple[int, str]:
    argv, environ = sys.argv, dict(os.environ)
    sys.argv = [str(helper), prompt]
    os.environ.clear()
    os.environ.update(env)
    try:
        runpy.run_path(str(helper), run_name="__main__")
    except SystemExit as exc:
        code = int(exc.code or 0)
    else:
        code = 0
    finally:
        sys.argv = argv
        os.environ.clear()
        os.environ.update(environ)
    return code, capsys.readouterr().out


def test_the_helper_answers_only_git_prompts_from_a_private_regular_file(tmp_path: Path, capsys):
    with askpass_credential(TOKEN, base_dir=tmp_path) as credential:
        env = credential.env
        assert _run_helper(
            credential.helper, "Username for 'https://github.com': ", env, capsys
        ) == (
            0,
            "x-access-token\n",
        )
        assert _run_helper(
            credential.helper, "Password for 'https://x@github.com': ", env, capsys
        ) == (
            0,
            TOKEN + "\n",
        )
        assert _run_helper(credential.helper, "Passphrase for key: ", env, capsys)[0] == 1
        assert _run_helper(credential.helper, "", env, capsys)[0] == 1
        assert _run_helper(credential.helper, "Password: ", {}, capsys)[0] == 1  # no file named
        # A file somebody else could read is never used as the credential.
        os.chmod(credential.secret_file, 0o644)
        assert _run_helper(credential.helper, "Password: ", env, capsys)[0] == 1
        os.chmod(credential.secret_file, 0o600)
        # Nor a symlink in place of the file.
        link = tmp_path / "link"
        link.symlink_to(credential.secret_file)
        assert _run_helper(credential.helper, "Password: ", {SECRET_ENV: str(link)}, capsys)[0] == 1


def test_git_itself_obtains_the_credential_through_the_helper(tmp_path: Path):
    """``git credential fill`` with no helpers configured asks GIT_ASKPASS, as fetch would."""
    home = tmp_path / "home"
    home.mkdir()
    with askpass_credential(TOKEN, base_dir=tmp_path) as credential:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            **credential.env,
        }
        completed = subprocess.run(
            ["git", "-c", "credential.helper=", "credential", "fill"],
            input="protocol=https\nhost=github.com\npath=org/repo.git\n\n",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        lines = dict(line.split("=", 1) for line in completed.stdout.strip().splitlines())
        assert lines["username"] == "x-access-token" and lines["password"] == TOKEN
        # Without the secret file git gets nothing and gives up (no terminal prompt).
        os.unlink(credential.secret_file)
        failed = subprocess.run(
            ["git", "-c", "credential.helper=", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert failed.returncode != 0 and TOKEN not in failed.stdout + failed.stderr

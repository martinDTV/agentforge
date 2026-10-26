"""Security checks for the one-time Runner token rotation helper."""

import os
from pathlib import Path

import pytest

from scripts.runner_token_rotation import atomic_write, read_private_file, replace_hashes


def test_rotation_changes_only_two_hashes_and_preserves_other_configuration():
    original = (
        b"# private configuration\n"
        b"RUNNER_RUNNER_ID=agentforge-runner-01\n"
        b"RUNNER_TOKEN_SHA256=old-api\n"
        b"RUNNER_ADMIN_TOKEN_SHA256=old-admin\n"
        b'RUNNER_IMAGES={"python-3.13":"image@sha256:demo"}\n'
    )
    hashes = {"RUNNER_TOKEN_SHA256": "a" * 64, "RUNNER_ADMIN_TOKEN_SHA256": "b" * 64}
    updated = replace_hashes(original, hashes)
    assert b"RUNNER_TOKEN_SHA256=" + b"a" * 64 in updated
    assert b"RUNNER_ADMIN_TOKEN_SHA256=" + b"b" * 64 in updated
    assert b"RUNNER_RUNNER_ID=agentforge-runner-01" in updated
    assert b'RUNNER_IMAGES={"python-3.13":"image@sha256:demo"}' in updated
    assert b"old-api" not in updated and b"old-admin" not in updated


@pytest.mark.parametrize(
    "original",
    [
        b"RUNNER_TOKEN_SHA256=old-api\n",
        b"RUNNER_TOKEN_SHA256=one\nRUNNER_TOKEN_SHA256=two\nRUNNER_ADMIN_TOKEN_SHA256=old\n",
    ],
)
def test_rotation_refuses_missing_or_duplicate_keys(original: bytes):
    hashes = {"RUNNER_TOKEN_SHA256": "a" * 64, "RUNNER_ADMIN_TOKEN_SHA256": "b" * 64}
    with pytest.raises(ValueError, match="exactamente una"):
        replace_hashes(original, hashes)


def test_private_file_rejects_symlink_and_wrong_mode(tmp_path: Path):
    target = tmp_path / "runner.env"
    target.write_text("private")
    target.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        read_private_file(target, os.getuid())
    target.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    with pytest.raises(OSError):
        read_private_file(alias, os.getuid())


def test_atomic_write_preserves_owner_and_private_mode(tmp_path: Path):
    target = tmp_path / "runner.env"
    target.write_bytes(b"old")
    target.chmod(0o600)
    original, info = read_private_file(target, os.getuid())
    assert original == b"old"
    atomic_write(target, b"new", info)
    updated, after = read_private_file(target, os.getuid())
    assert updated == b"new"
    assert (after.st_uid, after.st_gid, after.st_mode & 0o777) == (
        info.st_uid,
        info.st_gid,
        0o600,
    )

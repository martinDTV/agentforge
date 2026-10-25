"""Scanning of workspace archives: nothing is ever written on the runner host."""

import hashlib
import tarfile
from pathlib import Path

import pytest
from agentforge_runner.errors import RunnerError
from agentforge_runner.extract import ExtractionLimits, scan_archive, verify_archive

from tests.unit.runner_fixtures import DEFAULT_ARCHIVE, build_archive

LIMITS = ExtractionLimits(max_files=100, max_file_bytes=4096, max_total_bytes=16_384)


def test_regular_files_and_directories_are_described_without_touching_the_disk(tmp_path: Path):
    archive = build_archive(
        [("README.md", b"# demo\n"), ("src/app.py", b"x\n")],
        raw=[
            {"name": "bin/run.sh", "data": b"#!/bin/sh\n", "mode": 0o4777, "uid": 0, "gid": 0},
            {"name": "empty", "type": tarfile.DIRTYPE, "mode": 0o777},
            {"name": "./dot/./file.txt", "data": b"x"},
        ],
    )
    manifest = scan_archive(archive, LIMITS)
    assert manifest.files == {"README.md", "src/app.py", "bin/run.sh", "dot/file.txt"}
    assert manifest.directories == {"src", "bin", "empty", "dot"}
    assert manifest.executables == {"bin/run.sh"} and manifest.bytes == 20
    assert manifest.contains("src") and manifest.contains("src/app.py") and manifest.contains(".")
    assert not manifest.contains("srcx") and not manifest.contains("missing")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "member",
    [
        {"name": "/etc/passwd", "data": b"x"},
        {"name": "../escape.txt", "data": b"x"},
        {"name": "src/../../escape.txt", "data": b"x"},
        {"name": "link", "type": tarfile.SYMTYPE, "linkname": "/etc/passwd"},
        {"name": "hard", "type": tarfile.LNKTYPE, "linkname": "README.md"},
        {"name": "dev", "type": tarfile.CHRTYPE},
        {"name": "fifo", "type": tarfile.FIFOTYPE},
        {"name": "big.bin", "data": b"\x00" * 5000},
        {"name": "C:\\windows\\x", "data": b"x"},
        {"name": "ok.txt", "type": tarfile.DIRTYPE},  # same name as the file below
    ],
)
def test_hostile_members_are_refused(member: dict[str, object]):
    archive = build_archive([("ok.txt", b"ok")], raw=[member])
    with pytest.raises(RunnerError) as exc:
        scan_archive(archive, LIMITS)
    assert exc.value.code == "archive_rejected" and exc.value.status == 422


def test_duplicates_counts_and_decompression_bombs_are_bounded():
    with pytest.raises(RunnerError, match="duplicada"):
        scan_archive(build_archive([("a.txt", b"1"), ("a.txt", b"2")]), LIMITS)
    with pytest.raises(RunnerError, match="entradas"):
        scan_archive(build_archive([(f"f{i}.txt", b"x") for i in range(101)]), LIMITS)
    bomb = build_archive([(f"z{i}.bin", b"\x00" * 4000) for i in range(10)])  # 40 KB, tiny gzip
    assert len(bomb) < 1024
    with pytest.raises(RunnerError, match="descomprimido"):
        scan_archive(bomb, LIMITS)
    with pytest.raises(RunnerError, match="ilegible"):
        scan_archive(b"not a tarball", LIMITS)
    assert scan_archive(DEFAULT_ARCHIVE, LIMITS).count == 5


def test_archive_verification_checks_size_digest_and_cap():
    digest = hashlib.sha256(DEFAULT_ARCHIVE).hexdigest()
    size = len(DEFAULT_ARCHIVE)
    verify_archive(DEFAULT_ARCHIVE, expected_sha256=digest, expected_size=size, max_bytes=10_000)
    with pytest.raises(RunnerError) as exc:
        verify_archive(
            DEFAULT_ARCHIVE, expected_sha256=digest, expected_size=size + 1, max_bytes=10_000
        )
    assert exc.value.code == "archive_mismatch"
    with pytest.raises(RunnerError) as exc:
        verify_archive(
            DEFAULT_ARCHIVE, expected_sha256="0" * 64, expected_size=size, max_bytes=10_000
        )
    assert exc.value.code == "archive_mismatch"
    with pytest.raises(RunnerError) as exc:
        verify_archive(DEFAULT_ARCHIVE, expected_sha256=digest, expected_size=size, max_bytes=10)
    assert exc.value.code == "archive_too_large" and exc.value.status == 413

"""Pre-publication scan: every rule has a positive and a negative case, and the
scan fails closed on errors or undetermined facts (ADR 0011 §8)."""

import pytest

from app.projects import RepositoryPolicy
from app.publishing import SECRET_PATTERNS, ChangedEntry, ScanReport, scan_changes

POLICY = RepositoryPolicy(
    repository_id="sandbox-github",
    project_id="sandbox-github",
    writable_globs=["src/**", "tests/**", "**/*"],
    max_file_bytes=2048,
    max_change_bytes=4096,
)
APPROVED = {"src/app.py": "edit", "tests/test_app.py": "create"}


def entry(path="src/app.py", change="modified", **overrides) -> ChangedEntry:
    base = dict(
        path=path,
        change=change,
        old_mode="100644" if change != "added" else None,
        new_mode="100644" if change != "deleted" else None,
        size=100,
        is_binary=False,
        added_lines=("def f():", "    return 1"),
        sha256="a" * 64,
    )
    base.update(overrides)
    return ChangedEntry(**base)  # type: ignore[arg-type]


def codes(report: ScanReport) -> list[str]:
    return report.codes


def test_a_clean_change_inside_the_scope_passes():
    report = scan_changes(
        [entry(), entry("tests/test_app.py", "added")], approved=APPROVED, policy=POLICY
    )
    assert not report.blocking and report.findings == [] and report.entries == 2
    assert report.total_bytes == 200 and len(report.digest()) == 64


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([], ["empty_change"]),
        ([entry("src/other.py")], ["path_not_in_scope"]),
        ([entry("src/app.py", "deleted", size=None, sha256=None)], ["deletion_not_allowed"]),
        ([entry("src/app.py", "added", old_mode=None)], ["action_mismatch"]),
        ([entry("tests/test_app.py", "modified")], ["action_mismatch"]),
        ([entry(new_mode="120000")], ["symlink"]),
        ([entry(new_mode="160000")], ["submodule"]),
        ([entry(new_mode="100755")], ["executable_bit_added"]),
        ([entry(is_binary=True)], ["binary_file"]),
        ([entry(is_binary=None)], ["binary_undetermined"]),
        ([entry(size=None)], ["size_undetermined"]),
        ([entry(sha256=None)], ["hash_undetermined"]),
        ([entry(size=4000)], ["file_too_large"]),
        ([entry(added_lines=("x = 1\r",))], ["crlf_introduced"]),
        ([entry(), entry()], ["duplicate_path"]),
        (
            [
                entry(size=2048),
                entry("tests/test_app.py", "added", size=2048),
                entry("src/b.py", size=1),
            ],
            ["change_too_large", "path_not_in_scope"],
        ),
    ],
)
def test_each_rule_blocks_with_its_code(entries: list[ChangedEntry], expected: list[str]):
    report = scan_changes(entries, approved=APPROVED, policy=POLICY)
    assert report.blocking and codes(report) == sorted(expected), report.findings


def test_policy_denials_and_sensitive_names_block_even_inside_the_scope():
    approved = {".github/workflows/ci.yml": "create", "src/.env": "create", "AGENTS.md": "edit"}
    report = scan_changes(
        [
            entry(".github/workflows/ci.yml", "added"),
            entry("src/.env", "added"),
            entry("AGENTS.md"),
        ],
        approved=approved,
        policy=POLICY,
    )
    assert report.blocking
    assert "path_denied_by_policy" in codes(report)
    assert "path_sensitive_or_reserved" in codes(report)
    assert "path_excluded_context" in codes(report)


@pytest.mark.parametrize(
    ("name", "line"),
    [
        ("github_token", "token = 'ghs_" + "A" * 36 + "'"),
        ("github_token", "github_pat_" + "x" * 30),
        ("aws_access_key", "AKIAABCDEFGHIJKLMNOP"),
        ("google_api_key", "AIza" + "a" * 35),
        ("slack_token", "xoxb-" + "1" * 20),
        ("tailscale_key", "tskey-auth-" + "k" * 20),
        ("private_key", "-----BEGIN RSA PRIVATE KEY-----"),
        ("jwt", "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20),
        ("url_credentials", "https://user:pass@github.com/x.git"),
        ("secret_assignment", "PASSWORD = 'hunter2hunter2'"),
        ("secret_assignment", 'api_key: "0123456789abcdef"'),
    ],
)
def test_secrets_in_added_lines_block_the_publication(name: str, line: str):
    report = scan_changes([entry(added_lines=("ok", line))], approved=APPROVED, policy=POLICY)
    finding = next(f for f in report.findings if f.code == "secret_detected")
    assert finding.detail == name and finding.line == 2 and finding.path == "src/app.py"
    assert dict(SECRET_PATTERNS)[name].search(line)


def test_benign_lines_do_not_trip_the_secret_rules():
    lines = (
        "password_policy = 'min 8 chars'",
        "API_KEY_HEADER = 'X-Api-Key'",
        "url = 'https://github.com/martinDTV/agentforge-sandbox-demo.git'",
        "token = os.environ['GITHUB_TOKEN']",
        "assert 'ghs_' not in text",
    )
    report = scan_changes([entry(added_lines=lines)], approved=APPROVED, policy=POLICY)
    assert not report.blocking


def test_the_scan_fails_closed_on_unexpected_errors(monkeypatch: pytest.MonkeyPatch):
    def exploding(*_args, **_kwargs):
        raise RuntimeError("disk gone")

    monkeypatch.setattr("app.publishing.scan.check_against_policy", exploding)
    report = scan_changes([entry()], approved=APPROVED, policy=POLICY)
    assert report.blocking and codes(report) == ["scan_error"]
    assert "RuntimeError" in report.findings[0].detail

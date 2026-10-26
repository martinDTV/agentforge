"""Post-install check of the Runner's program allowlist (runs on the VM as UID 110).

Imports the *installed* ``agentforge_runner`` package and verifies, without
touching Podman or any credential, that ``ruff_check_diff`` is accepted with
its fixed prefix, that ``--fix`` is still refused everywhere and that the three
original programs keep their exact prefixes.
"""

from agentforge_runner.errors import RunnerError  # type: ignore[import-untyped]
from agentforge_runner.programs import PREFIXES, validate_argv  # type: ignore[import-untyped]
from agentforge_runner.protocol import ProgramId  # type: ignore[import-untyped]

EXPECTED = {
    "ruff_check": ("ruff", "check", "--no-cache", "--output-format", "concise"),
    "ruff_check_diff": ("ruff", "check", "--no-cache", "--diff"),
    "ruff_format_check": ("ruff", "format", "--check", "--no-cache"),
    "pytest": ("python", "-I", "-P", "-m", "pytest", "-q", "-p", "no:cacheprovider"),
}


def refused(program: ProgramId, argv: list[str]) -> bool:
    try:
        validate_argv(program, argv)
    except RunnerError as exc:
        return bool(exc.code == "argv_rejected")
    return False


def main() -> None:
    assert {p.value: PREFIXES[p] for p in PREFIXES} == EXPECTED, "prefijos inesperados"
    assert [p.value for p in ProgramId] == list(EXPECTED), "programas inesperados"
    diff = ProgramId("ruff_check_diff")
    assert validate_argv(diff, [*PREFIXES[diff], "--", "src", "tests"]) == ["src", "tests"]
    assert refused(diff, [*PREFIXES[diff], "--fix", "--", "src"])
    assert refused(diff, ["ruff", "check", "--no-cache", "--fix", "--", "src"])
    assert refused(diff, [*PREFIXES[diff], "src"])
    assert refused(diff, [*PREFIXES[diff], "--", "../etc"])
    for program in ProgramId:
        assert refused(program, ["bash", "-c", "id"])
    print("programs_selftest_ok=true; programs=4; ruff_check_diff=accepted; fix=refused")


if __name__ == "__main__":
    main()

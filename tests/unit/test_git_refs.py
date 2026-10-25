import uuid

import pytest

from app.git import (
    DEFAULT_PROTECTED_REFS,
    GitOperationError,
    is_protected,
    is_run_branch,
    run_branch,
    validate_branch_name,
    validate_pathspec,
    validate_ref,
)


@pytest.mark.parametrize(
    "name",
    [
        "--output=/tmp/x",
        "-f",
        "main..dev",
        "HEAD@{1}",
        "a//b",
        "feature/",
        "x.lock",
        "refs/heads/x",
        "a\nb",
        "a b",
        "",
    ],
)
def test_branch_names_that_could_inject_options_or_syntax_are_invalid(name: str):
    with pytest.raises(GitOperationError) as exc:
        validate_branch_name(name)
    assert exc.value.code == "ref_invalid"


def test_run_branch_names_are_the_only_writable_shape():
    run_id = uuid.uuid4()
    branch = run_branch(run_id)
    assert branch == f"run/{run_id}" and is_run_branch(branch)
    assert validate_branch_name(branch) == branch
    assert not is_run_branch("run/not-a-uuid") and not is_run_branch("main")
    assert not is_run_branch(f"run/{run_id}/x")


def test_read_refs_allow_revision_syntax_but_not_options():
    assert validate_ref("HEAD~2") == "HEAD~2" and validate_ref("main^") == "main^"
    assert validate_ref("a1b2c3d") == "a1b2c3d"
    for bad in ("--all", "HEAD@{upstream}", "main..dev", ":/fix", "-"):
        with pytest.raises(GitOperationError):
            validate_ref(bad)


def test_pathspecs_stay_relative_and_option_free():
    assert validate_pathspec("src/app.py") == "src/app.py"
    for bad in ("-rf", "/etc/passwd", "../x", "~/x", ":(glob)*", "a\x00b", ""):
        with pytest.raises(GitOperationError) as exc:
            validate_pathspec(bad)
        assert exc.value.code == "pathspec_invalid"


def test_protected_patterns_cover_defaults_tags_and_full_ref_names():
    patterns = DEFAULT_PROTECTED_REFS | {"develop"}
    for ref in (
        "main",
        "master",
        "release/1.0",
        "refs/heads/main",
        "refs/tags/v1",
        "tags/v2",
        "develop",
    ):
        assert is_protected(ref, patterns), ref
    assert not is_protected(run_branch(uuid.uuid4()), patterns)
    assert not is_protected("feature/x", patterns)

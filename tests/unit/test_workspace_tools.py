import os
from pathlib import Path

import pytest

from app.contracts.tools import AgentKind, ToolCall, ToolOutcome, ToolResult
from app.tools import ToolLimits, workspace_tools
from app.tools.paths import is_sensitive_name
from tests.unit.tool_fixtures import make_context, make_gateway

pytestmark = pytest.mark.anyio


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# Demo\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "src" / "config.py").write_text('token = "abcdefghijklmnop"\nname = "x"\n')
    (root / ".env").write_text("SECRET=1\n")
    (root / ".env.example").write_text("SECRET=\n")
    (root / "keys").mkdir()
    (root / "keys" / "server.pem").write_text(
        "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"
    )
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    (root / "big.txt").write_text("a" * 5000)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    os.symlink(outside, root / "escape")
    os.symlink(root / "README.md", root / "inside_link")
    os.symlink(tmp_path, root / "escape_dir")
    return root


async def call(
    workspace: Path | None, name: str, limits: ToolLimits | None = None, **arguments: object
) -> ToolResult:
    gateway, _ = make_gateway(workspace_tools(limits or ToolLimits()), limits=limits)
    return await gateway.invoke(
        make_context(AgentKind.DEVELOPER, workspace=workspace),
        ToolCall(name=name, arguments=dict(arguments)),
        step="read",
    )


def code(result: ToolResult) -> str:
    assert result.outcome is ToolOutcome.FAILED and result.error is not None, result
    return result.error.code


async def test_list_dir_is_sorted_bounded_and_hides_vcs_internals(workspace: Path):
    result = await call(workspace, "workspace.list_dir")
    assert result.ok and result.output is not None
    names = [e["name"] for e in result.output["entries"]]
    assert names == sorted(names) and ".git" not in names and "src" in names
    kinds = {e["name"]: e["kind"] for e in result.output["entries"]}
    assert kinds["src"] == "dir" and kinds["README.md"] == "file" and kinds["escape"] == "symlink"
    small = await call(workspace, "workspace.list_dir", ToolLimits(max_entries=2))
    assert (
        small.output is not None and len(small.output["entries"]) == 2 and small.output["truncated"]
    )
    assert code(await call(workspace, "workspace.list_dir", path="src/app.py")) == "not_a_directory"


async def test_read_file_returns_text_within_the_workspace(workspace: Path):
    result = await call(workspace, "workspace.read_file", path="README.md")
    assert result.ok and result.output == {
        "path": "README.md",
        "content": "# Demo\n",
        "size": 7,
        "truncated": False,
        "redactions": 0,
    }
    via_link = await call(workspace, "workspace.read_file", path="inside_link")
    assert via_link.ok and via_link.output is not None and via_link.output["path"] == "README.md"
    nested = await call(workspace, "workspace.read_file", path="./src/../src/app.py")
    assert (
        code(nested) == "path_outside_workspace"
    )  # ".." is refused even when it would stay inside


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("../outside.txt", "path_outside_workspace"),
        ("/etc/passwd", "path_outside_workspace"),
        ("~/.ssh/id_rsa", "path_outside_workspace"),
        ("escape", "path_outside_workspace"),
        ("escape_dir/outside.txt", "path_outside_workspace"),
        (".git/config", "path_denied"),
        (".env", "path_denied"),
        ("keys/server.pem", "path_denied"),
        ("bad\x00name", "path_denied"),
        ("bin.dat", "binary_file"),
        ("src", "not_a_file"),
        ("missing.txt", "path_not_found"),
    ],
)
async def test_read_file_refuses_escapes_secrets_binaries_and_misses(
    workspace: Path, path: str, expected: str
):
    assert code(await call(workspace, "workspace.read_file", path=path)) == expected


async def test_templates_are_readable_but_secret_values_are_redacted(workspace: Path):
    template = await call(workspace, "workspace.read_file", path=".env.example")
    assert template.ok
    config = await call(workspace, "workspace.read_file", path="src/config.py")
    assert config.ok and config.output is not None
    assert config.output["redactions"] == 1
    assert (
        'token = "[REDACTED]"' in config.output["content"]
        and 'name = "x"' in config.output["content"]
    )
    assert is_sensitive_name("id_ed25519.pub") and not is_sensitive_name(".env.sample")


async def test_read_file_truncates_to_the_platform_limit_never_above_it(workspace: Path):
    limited = await call(
        workspace, "workspace.read_file", ToolLimits(max_read_bytes=1024), path="big.txt"
    )
    assert limited.ok and limited.output is not None
    assert limited.output["truncated"] and len(limited.output["content"]) == 1024
    assert limited.output["size"] == 5000
    asked_more = await call(
        workspace,
        "workspace.read_file",
        ToolLimits(max_read_bytes=1024),
        path="big.txt",
        max_bytes=10_000,
    )
    assert asked_more.output is not None and len(asked_more.output["content"]) == 1024
    asked_less = await call(workspace, "workspace.read_file", path="big.txt", max_bytes=10)
    assert asked_less.output is not None and len(asked_less.output["content"]) == 10


async def test_find_matches_globs_inside_the_workspace_only(workspace: Path):
    result = await call(workspace, "workspace.find", pattern="**/*.py")
    assert result.ok and result.output is not None
    assert result.output["matches"] == ["src/app.py", "src/config.py"]
    everything = await call(workspace, "workspace.find", pattern="**/*")
    assert everything.output is not None
    assert not any(m.startswith(".git") for m in everything.output["matches"])
    assert "escape" not in everything.output["matches"]  # symlink out of the root is not reported
    assert code(await call(workspace, "workspace.find", pattern="../*")) == "path_outside_workspace"
    assert (
        code(await call(workspace, "workspace.find", pattern="*", path="missing"))
        == "path_not_found"
    )
    bounded = await call(workspace, "workspace.find", ToolLimits(max_entries=1), pattern="**/*.py")
    assert (
        bounded.output is not None
        and len(bounded.output["matches"]) == 1
        and bounded.output["truncated"]
    )


async def test_tools_need_an_assigned_workspace(workspace: Path):
    assert (
        code(await call(None, "workspace.read_file", path="README.md")) == "workspace_unavailable"
    )
    assert code(await call(None, "workspace.list_dir")) == "workspace_unavailable"

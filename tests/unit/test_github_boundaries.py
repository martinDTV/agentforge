"""Static guarantees of ADR 0010: GitHub credentials stay in the platform.

Only ``app/infrastructure/github`` (and the settings that feed it, the lifespan
wiring and the operator script) may touch the App id, the private key or an
installation token. Agents, tools, the sandbox contracts, the Git port and
the orchestration never import the GitHub package or PyJWT, and no tool
exposes GitHub to a model.
"""

import re
from pathlib import Path

from app.contracts.tools import RiskClass
from app.security.tools import DEFAULT_AGENT_GRANTS, ENABLED_RISK_CLASSES
from app.tools import ToolLimits, default_registry

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
SECRET_SETTINGS = re.compile(r"github_app_(id|private_key_file)|GITHUB_APP_PRIVATE_KEY")
ALLOWED_TO_READ_SECRETS = {
    "app/settings.py",
    "app/infrastructure/github/credentials.py",
    "app/main.py",
}
# OIDC verification and the local development issuer legitimately use PyJWT.
OIDC_MODULES = {"app/security/tokens.py", "app/security/dev_keys.py"}
NEVER_IMPORT_GITHUB = (
    "app/agents",
    "app/tools",
    "app/sandbox",
    "app/security",
    "app/git",
    "app/workspaces",
    "app/contracts",
    "app/projects",
    "app/api",
)


def _sources() -> dict[str, str]:
    return {
        str(path.relative_to(ROOT)): path.read_text(encoding="utf-8") for path in APP.rglob("*.py")
    }


def test_only_the_platform_reads_the_github_app_secrets():
    offenders = sorted(
        name
        for name, text in _sources().items()
        if SECRET_SETTINGS.search(text) and name not in ALLOWED_TO_READ_SECRETS
    )
    assert offenders == [], offenders


def test_agents_tools_sandbox_and_contracts_never_import_github_or_jwt():
    offenders = []
    for name, text in _sources().items():
        if not name.startswith(NEVER_IMPORT_GITHUB):
            continue
        if re.search(r"^\s*(from|import)\s+app\.infrastructure\.github\b", text, re.M):
            offenders.append((name, "app.infrastructure.github"))
        if re.search(r"^\s*(from|import)\s+jwt\b", text, re.M) and name not in OIDC_MODULES:
            offenders.append((name, "jwt"))
        if "InstallationToken" in text or "askpass_credential" in text:
            offenders.append((name, "token/askpass"))
    assert offenders == [], offenders


def test_the_orchestration_reaches_github_only_through_the_mirror_service():
    for path in (APP / "orchestration").rglob("*.py"):
        text = path.read_text()
        assert "app.infrastructure.github" not in text, path
        assert "InstallationToken" not in text and ".reveal(" not in text, path
    text = (APP / "orchestration" / "scoped_development.py").read_text()
    assert "MirrorService" in text  # the single platform-side entry point


def test_no_tool_exposes_github_mirrors_or_push_to_a_model():
    names = set(default_registry(ToolLimits(), None, None).names())
    assert not any(n.startswith(("github", "mirror", "remote")) for n in names), names
    assert "git.fetch" not in names and "git.pull" not in names
    assert ENABLED_RISK_CLASSES == {RiskClass.READ}
    for agent, grant in DEFAULT_AGENT_GRANTS.items():
        assert RiskClass.NETWORK not in grant.risks, agent
        assert RiskClass.WRITE not in grant.risks and RiskClass.EXEC not in grant.risks, agent


def test_the_askpass_helper_is_only_wired_by_the_mirror_fetch():
    local = (APP / "infrastructure" / "git" / "local.py").read_text()
    # The agents' adapter answers no credential prompt: GIT_ASKPASS is pinned to ``false``.
    assert re.search(r"GIT_ASKPASS\W{1,5}false", local), "GIT_ASKPASS=false missing"
    assert local.count("env_extra") >= 2  # the only override path is the explicit fetch
    assert "askpass" not in local.lower().replace("git_askpass", "")
    mirror = (APP / "infrastructure" / "git" / "mirror.py").read_text()
    assert "askpass_credential(" in mirror and "credential_env=credential.env" in mirror
    for name in ("worktrees", "tools", "agents"):
        for path in (APP / name).rglob("*.py"):
            assert "askpass" not in path.read_text().lower(), path

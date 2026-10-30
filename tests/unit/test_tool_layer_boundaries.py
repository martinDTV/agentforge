"""Dependency direction of the tool layer, checked statically."""

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"


def _imports(path: Path) -> set[str]:
    return set(re.findall(r"^\s*(?:from|import)\s+([\w.]+)", path.read_text(), re.M))


def test_tools_never_import_orchestration_infrastructure_or_fastapi():
    for module in (APP / "tools").glob("*.py"):
        imported = _imports(module)
        for forbidden in (
            "app.orchestration",
            "app.infrastructure",
            "app.api",
            "fastapi",
            "sqlalchemy",
        ):
            assert not any(i.startswith(forbidden) for i in imported), (module.name, forbidden)


def test_security_decides_about_tools_without_importing_them():
    imported = _imports(APP / "security" / "tools.py")
    assert not any(i.startswith(("app.tools", "fastapi")) for i in imported)
    assert "app.contracts.tools" in imported


def test_the_gateway_is_the_only_place_that_runs_a_tool():
    callers = [
        p
        for p in APP.rglob("*.py")
        if re.search(r"\.run\(context", p.read_text()) and p.name != "gateway.py"
    ]
    assert callers == [], callers


def test_domain_and_orchestration_do_not_depend_on_the_homelab():
    """Tailscale, Proxmox and Podman are deployment choices: only the adapter
    and the runner may name them."""
    words = re.compile(r"tailscale|tailnet|proxmox|podman", re.I)
    for layer in (
        "sandbox",
        "tools",
        "orchestration",
        "contracts",
        "security",
        "workspaces",
        "git",
        "api",
    ):
        for module in (APP / layer).rglob("*.py"):
            assert not words.search(module.read_text()), module


def test_the_goal_domain_imports_only_contracts_and_the_coordinator_never_approves():
    """ADR 0012: ``app/goals`` is pure; the coordinator reaches runs only through the
    launcher's create/decisions/cancel and never touches GitHub, Git or the sandbox."""
    allowed = ("app.contracts", "app.security")  # same rule as app/tools: pure packages only
    for module in (APP / "goals").glob("*.py"):
        for name in _imports(module):
            assert not name.startswith("app.") or name.startswith(allowed), (module.name, name)
    coordinator = APP / "orchestration" / "goals.py"
    imported = _imports(coordinator)
    for forbidden in (
        "app.infrastructure.github",
        "app.infrastructure.git",
        "app.infrastructure.sandbox",
        "app.tools",
        "app.sandbox",
        "app.workspaces",
        "fastapi",
        "sqlalchemy",
    ):
        assert not any(i.startswith(forbidden) for i in imported), forbidden
    source = coordinator.read_text()
    assert ".approve(" not in source and ".resume(" not in source
    assert "generate_structured" not in source  # no model in the supervision loop
    # Only the integration probe (platform code) talks to GitHub for goals.
    probe = APP / "infrastructure" / "github" / "integration.py"
    assert "PULLS_READ_PERMISSIONS" in probe.read_text()

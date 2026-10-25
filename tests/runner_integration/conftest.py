"""Real Podman rootless integration (Linux only). Run explicitly on the runner VM:

    RUNNER_TEST_IMAGE=localhost/agentforge/python-3.13@sha256:... \\
    uv run pytest tests/runner_integration -q

These tests FAIL (never skip) when Podman rootless, cgroups v2 or the pinned
image are not available: a green run here means the real boundary works.
They have NOT been executed as part of the development on macOS.
"""

import os
import platform
from pathlib import Path

import pytest
from agentforge_runner.podman import PodmanBackend


@pytest.fixture(scope="session")
def image() -> str:
    reference = os.environ.get("RUNNER_TEST_IMAGE")
    if not reference or "@sha256:" not in reference:
        pytest.fail(
            "Define RUNNER_TEST_IMAGE con una imagen fijada por digest que incluya ruff y pytest"
        )
    return reference


@pytest.fixture(scope="session")
def backend(image: str) -> PodmanBackend:
    if platform.system() != "Linux":
        pytest.fail("La integración del runner requiere Linux con Podman rootless")
    backend = PodmanBackend(os.environ.get("RUNNER_PODMAN", "podman"))
    report = backend.preflight({"test": image})
    if not report.ok:
        failed = ", ".join(f"{c.name}={c.detail}" for c in report.checks if not c.ok)
        pytest.fail(f"Preflight de aislamiento fallido: {failed}")
    return backend


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "ok.py").write_text("x = 1\n")
    (root / "src" / "bad.py").write_text("import os\nx=1\n")
    (root / "tests" / "test_demo.py").write_text(
        "import socket, time\n"
        "def test_ok():\n    assert True\n"
        "def test_no_network():\n"
        "    try:\n        socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
        "    except OSError:\n        return\n"
        "    raise AssertionError('la red debería estar deshabilitada')\n"
        "def test_readonly_root():\n"
        "    try:\n        open('/etc/agentforge-probe', 'w')\n"
        "    except OSError:\n        return\n"
        "    raise AssertionError('la raíz debería ser de solo lectura')\n"
    )
    return root

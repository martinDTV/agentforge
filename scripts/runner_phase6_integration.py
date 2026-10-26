"""Run the eight existing real Podman tests as UID 110 without changing services."""

import ast
import hashlib
import importlib.util
import inspect
import os
import tempfile
from pathlib import Path

from agentforge_runner.config import RunnerSettings
from agentforge_runner.podman import PodmanBackend


def main() -> None:
    if os.geteuid() != 110:
        raise RuntimeError("ejecutar como agentforge-runner UID 110")
    stage = Path(__file__).resolve().parent
    installed = Path("/home/agentforge-runner/app/runner/agentforge_runner")
    expected = {
        "podman.py": "2e9d64516ded57088403062cd34818eb61573c6a11132ac91be7de6771652356",
        "service.py": "3dd1c4e66237699251a7058f38adf98481c1fa7c8bae4da2c38a4d86ffa7f1ee",
    }
    assert all(
        hashlib.sha256((installed / n).read_bytes()).hexdigest() == h for n, h in expected.items()
    )
    print("deployed_cleanup_hashes_match=true", flush=True)
    settings = RunnerSettings(
        _env_file="/home/agentforge-runner/.config/agentforge-runner/runner.env"
    )
    image = settings.images["python-3.13"]
    backend = PodmanBackend(settings.podman_binary)
    report = backend.preflight({"python-3.13": image})
    assert report.ok and report.rootless and len(report.checks) == 16
    assert backend.list_containers() == []
    module_spec = importlib.util.spec_from_file_location(
        "real_podman_tests", stage / "test_podman_real.py"
    )
    assert module_spec and module_spec.loader
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    tree = ast.parse((stage / "runner_conftest.py").read_text())
    fixture = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "workspace")
    fixture.decorator_list = []
    namespace = {"Path": Path}
    exec(
        compile(ast.Module(body=[fixture], type_ignores=[]), "workspace_fixture", "exec"), namespace
    )
    tests = [(n, f) for n, f in vars(module).items() if n.startswith("test_") and callable(f)]
    assert len(tests) == 8
    for name, test in tests:
        with tempfile.TemporaryDirectory(prefix="agentforge-phase6-") as temporary:
            values = {
                "backend": backend,
                "image": image,
                "workspace": namespace["workspace"](Path(temporary)),
            }
            test(**{key: values[key] for key in inspect.signature(test).parameters})
        print(f"PASS {name}", flush=True)
    assert backend.list_containers() == []
    print("real_podman_tests_passed=8; residual_container_count=0", flush=True)


if __name__ == "__main__":
    main()

"""Operator-driven Developer + QA run on a disposable ``sandbox-*`` repository.

    uv run python -m scripts.development_mvp --seed-demo --yes
    uv run python -m scripts.development_mvp --repository sandbox-dev --project sandbox-dev --yes

First real multi-agent flow of AgentForge (Fase 6): the Developer agent edits a
bounded list of files in an isolated worktree, QA validates them in the remote
sandbox runner, and the loop repeats at most three times. Everything the agents
do goes through the audited Tool Gateway; nothing runs on this host.

Guarantees, each enforced in code (same posture as ``scripts/sandbox_smoke.py``):

- ``ENVIRONMENT=local`` only; a remote runner must be configured, verified and
  ready. There is no local execution of any program, ever.
- The repository must be registered under ``GIT_REPOSITORIES_ROOT`` inside a
  ``sandbox-*`` project. ``--seed-demo`` creates the throwaway ``sandbox-dev``
  project there and registers it. Personal repositories are out of reach.
- Authority is an ephemeral ``DevelopmentScope``: this run id, this project,
  this worktree, an explicit list of writable files and an expiry. Developer
  may read, write those files and run ruff; QA may read and run ruff/pytest.
  Global policies, grants and the risk gate are not touched.
- The model is Claude via Vertex unless ``--fake-model`` is passed, in which
  case canned changes exercise the plumbing and the report says ``model=fake``.
  If Settings say ``USE_FAKE_MODEL=true`` the run is refused as not real.
- Runs, events, tool invocations and LangGraph checkpoints land in PostgreSQL
  through the same ``RunService`` the API uses (lease, heartbeat, projection).
- The operator confirms before anything is sent (``--yes`` for unattended use).
- The worktree is kept for inspection unless ``--remove-worktree`` is given;
  the patch is part of the report and optionally written with ``--patch-out``.
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.agents.development import DeveloperChanges, FileEdit, QAReview
from app.contracts.execution import RunError
from app.contracts.lifecycle import TERMINAL_STATUSES, RunLifecycleStatus, WorkflowKind
from app.contracts.tools import InvocationStatus
from app.git import GitRepositoryPort, RepositoryConfig, RepositoryRegistry
from app.infrastructure.checkpoints import PostgresCheckpointer
from app.infrastructure.db import (
    Database,
    SqlAlchemyRunRepository,
    SqlAlchemyToolInvocationRepository,
    verify_schema,
)
from app.infrastructure.git import LocalGitAdapter
from app.infrastructure.llm.budget import BudgetedLLM
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.infrastructure.llm.port import LLMPort
from app.infrastructure.llm.vertex_claude import VertexClaudeAdapter
from app.infrastructure.sandbox import RemoteSandboxAdapter
from app.orchestration.development import DevelopmentDeps, build_development_graph
from app.orchestration.policy import RecoveryPolicy
from app.orchestration.ports import RunProjection, RunRepository
from app.orchestration.runs import RunService
from app.sandbox import SandboxPort
from app.security import AuthorizationPolicy, Principal
from app.security.development import DevelopmentScope, DevelopmentToolPolicy
from app.settings import get_environment, get_settings
from app.tools import (
    ExecRunTool,
    ExecutionPolicy,
    GitTools,
    ToolGateway,
    ToolLimits,
    default_registry,
)
from app.tools.ports import ToolInvocationRepository
from app.tools.write_file import WriteFileTool
from app.workspaces import WorktreeManager, WorktreePolicy

PROJECT_PREFIX = "sandbox-"
DEMO_REPOSITORY = "sandbox-dev"
CONFIRM_WORD = "ejecutar"
OPERATOR_SUBJECT = "operator:development-mvp"
OPERATOR_ISSUER = "agentforge:operator"
DEFAULT_TASK = (
    "Implementa validate_email(address: str) -> bool en src/mailcheck/validator.py siguiendo "
    "exactamente las reglas de README.md y añade pruebas unitarias propias en "
    "tests/test_validator.py. Las pruebas de aceptación de tests/test_acceptance.py deben pasar."
)
DEFAULT_WRITABLE = ("src/mailcheck/validator.py", "tests/test_validator.py")
DEFAULT_READ = (
    "README.md",
    "pyproject.toml",
    "src/mailcheck/__init__.py",
    "src/mailcheck/validator.py",
    "tests/test_acceptance.py",
)
# Reference prices used only to *estimate* the real spend in the report
# (Claude Sonnet 4.6: USD 3 / M input, USD 15 / M output).
REFERENCE_USD_PER_M_INPUT = 3.0
REFERENCE_USD_PER_M_OUTPUT = 15.0


class DevelopmentRefused(Exception):
    """The run will not start; ``code`` says why."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# -- guards ------------------------------------------------------------------------------------


def require_local_environment(environment: str) -> None:
    if environment != "local":
        raise DevelopmentRefused(
            "environment", f"solo en ENVIRONMENT=local (actual: {environment})"
        )


def require_sandbox_project(project_id: str) -> None:
    if not project_id.startswith(PROJECT_PREFIX):
        raise DevelopmentRefused(
            "project", f"el proyecto debe empezar por '{PROJECT_PREFIX}' (actual: {project_id})"
        )


def require_verified_runner(sandbox: SandboxPort) -> str:
    health = sandbox.health()
    if not health.verified:
        raise DevelopmentRefused("runner_unverified", "el runner no acreditó identidad y backend")
    if not health.ready:
        raise DevelopmentRefused(
            "runner_not_ready", "el runner no supera su preflight de aislamiento"
        )
    return health.runner_id


def operator_principal(project_id: str) -> Principal:
    """The human operator of this script: may create and audit runs in the sandbox project."""
    return Principal(
        subject=OPERATOR_SUBJECT,
        kind="user",
        issuer=OPERATOR_ISSUER,
        roles=frozenset({"operator"}),
        projects=frozenset({project_id}),
    )


# -- demo repository ---------------------------------------------------------------------------

DEMO_FILES: dict[str, str] = {
    "README.md": """# sandbox-dev (mailcheck)

Repositorio desechable para la primera ejecución multiagente de AgentForge.

## Tarea

Implementar `validate_email(address: str) -> bool` en `src/mailcheck/validator.py`
y añadir pruebas unitarias propias en `tests/test_validator.py`.
Las pruebas de aceptación de `tests/test_acceptance.py` definen el comportamiento
exigido y no se modifican.

## Reglas

1. Exactamente un `@`; parte local y dominio no vacíos.
2. Longitud total máxima 254 caracteres; parte local máxima 64.
3. Parte local: letras ASCII, dígitos y los símbolos `. _ % + -`; sin punto
   inicial ni final; sin dos puntos consecutivos.
4. Dominio: al menos dos etiquetas separadas por `.`; cada etiqueta de 1 a 63
   caracteres entre letras ASCII, dígitos y guion, sin guion inicial ni final.
5. La última etiqueta (TLD) contiene solo letras y al menos dos.
6. Sin espacios en blanco en ninguna posición. Mayúsculas y minúsculas se admiten.
7. Si `address` no es `str`, lanzar `TypeError`.

Sin dependencias externas. Debe pasar `ruff check`, `ruff format --check` y `pytest`
con la configuración de `pyproject.toml`.
""",
    "pyproject.toml": """[project]
name = "mailcheck"
version = "0.1.0"
description = "Proyecto desechable para la primera ejecución Developer + QA de AgentForge"
requires-python = ">=3.13"

[tool.ruff]
line-length = 100
target-version = "py313"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
""",
    "src/mailcheck/__init__.py": (
        '"""mailcheck: validación de correos electrónicos (proyecto de prueba de AgentForge)."""\n'
        "\n"
        "from .validator import validate_email\n"
        "\n"
        '__all__ = ["validate_email"]\n'
    ),
    "src/mailcheck/validator.py": (
        '"""Validación de direcciones de correo electrónico. Pendiente de implementar."""\n'
        "\n"
        "\n"
        "def validate_email(address: str) -> bool:\n"
        '    """Devuelve True si ``address`` cumple las reglas descritas en README.md."""\n'
        '    raise NotImplementedError("pendiente: implementar según README.md")\n'
    ),
    "tests/test_acceptance.py": '''\
"""Pruebas de aceptación: definen el comportamiento exigido. No se modifican."""

import pytest

from mailcheck import validate_email

VALID = [
    "user@example.com",
    "First.Last+tag@Sub.Example.ORG",
    "a_b%c-d@ex-ample.co",
    "x@y.io",
]

INVALID = [
    "",
    "plainaddress",
    "@example.com",
    "user@",
    "user@@example.com",
    "us er@example.com",
    "user@example.com ",
    ".user@example.com",
    "user.@example.com",
    "us..er@example.com",
    "user@example",
    "user@-example.com",
    "user@example-.com",
    "user@example.c0m",
    "user@example.c",
    "a" * 65 + "@example.com",
    "u@" + "a" * 64 + ".com",
    "a" * 60 + "@" + ("b" * 63 + ".") * 3 + "com",
]


@pytest.mark.parametrize("address", VALID)
def test_accepts_valid_addresses(address: str) -> None:
    assert validate_email(address) is True


@pytest.mark.parametrize("address", INVALID)
def test_rejects_invalid_addresses(address: str) -> None:
    assert validate_email(address) is False


@pytest.mark.parametrize("value", [None, 42, b"user@example.com"])
def test_non_string_input_raises_type_error(value: object) -> None:
    with pytest.raises(TypeError):
        validate_email(value)  # type: ignore[arg-type]
''',
}

LINT_REPOSITORY = "sandbox-lint"
LINT_TASK = (
    "Añade slugify_many(values: Iterable[str], *, separator: str = '-') -> list[str] en "
    "src/textkit/slug.py: aplica slugify a cada elemento conservando el orden y descarta los "
    "resultados vacíos. Añade pruebas unitarias propias en tests/test_slug_many.py. Las pruebas "
    "de aceptación de tests/ deben pasar y ruff check, ruff format --check y pytest deben "
    "terminar en cero."
)
LINT_WRITABLE = ("src/textkit/slug.py", "tests/test_slug_many.py")
LINT_READ = (
    "README.md",
    "pyproject.toml",
    "src/textkit/__init__.py",
    "src/textkit/slug.py",
    "tests/test_slug.py",
    "tests/test_slug_many_acceptance.py",
)
# ``slug.py`` ships one deliberate ``I001`` (members of the ``collections.abc``
# import out of order and two blank lines before a comment banner): the kind of
# finding ``ruff check --output-format concise`` does not explain and that
# ``ruff_check_diff`` makes obvious.
LINT_SLUG_SEED = '''\
"""Generación de slugs a partir de texto libre."""

import re
import unicodedata
from collections.abc import Iterable, Callable


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------

_NON_WORD = re.compile(r"[^a-z0-9]+")


def slugify(value: str, *, separator: str = "-") -> str:
    """Convierte ``value`` en un slug ASCII en minúsculas separado por ``separator``."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return _NON_WORD.sub(separator, normalized.lower()).strip(separator)


def apply_all(values: Iterable[str], transform: Callable[[str], str]) -> list[str]:
    """Aplica ``transform`` a cada elemento conservando el orden."""
    return [transform(value) for value in values]
'''
LINT_DEMO_FILES: dict[str, str] = {
    "README.md": """# sandbox-lint (textkit)

Repositorio desechable para la segunda ejecución multiagente de AgentForge.

## Tarea

Añadir `slugify_many(values: Iterable[str], *, separator: str = "-") -> list[str]`
en `src/textkit/slug.py` y pruebas unitarias propias en `tests/test_slug_many.py`.
Las pruebas de aceptación de `tests/test_slug.py` y
`tests/test_slug_many_acceptance.py` definen el comportamiento y no se modifican.

## Reglas

1. `slugify_many` aplica `slugify` a cada elemento conservando el orden.
2. Descarta los resultados vacíos.
3. Acepta cualquier iterable y propaga `separator` a `slugify`.
4. Sin dependencias externas. Debe pasar `ruff check`, `ruff format --check` y
   `pytest` con la configuración de `pyproject.toml`.
""",
    "pyproject.toml": """[project]
name = "textkit"
version = "0.1.0"
description = "Proyecto desechable para la segunda ejecución Developer + QA de AgentForge"
requires-python = ">=3.13"

[tool.ruff]
line-length = 100
target-version = "py313"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
""",
    "src/textkit/__init__.py": (
        '"""textkit: utilidades de texto (proyecto de prueba de AgentForge)."""\n'
        "\n"
        "from .slug import slugify\n"
        "\n"
        '__all__ = ["slugify"]\n'
    ),
    "src/textkit/slug.py": LINT_SLUG_SEED,
    "tests/test_slug.py": '''\
"""Pruebas de aceptación de slugify. No se modifican."""

import pytest

from textkit import slugify


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Hello World", "hello-world"),
        ("  Múltiples   espacios  ", "multiples-espacios"),
        ("Año-2026_ok", "ano-2026-ok"),
        ("¡Hola!", "hola"),
        ("", ""),
    ],
)
def test_slugify(value: str, expected: str) -> None:
    assert slugify(value) == expected


def test_custom_separator() -> None:
    assert slugify("Hello World", separator="_") == "hello_world"
''',
    "tests/test_slug_many_acceptance.py": '''\
"""Pruebas de aceptación de slugify_many. No se modifican."""

from textkit.slug import slugify_many


def test_preserves_order_and_drops_empty_results() -> None:
    assert slugify_many(["Hello World", "¡!", "Año Nuevo"]) == ["hello-world", "ano-nuevo"]


def test_accepts_any_iterable_and_custom_separator() -> None:
    assert slugify_many(iter(["A B", "C"]), separator="_") == ["a_b", "c"]


def test_empty_input() -> None:
    assert slugify_many([]) == []
''',
}

# Canned Developer answers for the lint profile: revision 0 adds the function
# but keeps the import block as found (the trap survives, QA's ruff fails and
# the diff preview runs); revision 1 applies the fix. Not an agent result.
_LINT_FUNCTION = '''

def slugify_many(values: Iterable[str], *, separator: str = "-") -> list[str]:
    """Aplica ``slugify`` a cada elemento, conserva el orden y descarta los vacíos."""
    return [slug for slug in apply_all(values, lambda v: slugify(v, separator=separator)) if slug]
'''
FAKE_LINT_SLUG_TRAP = LINT_SLUG_SEED + _LINT_FUNCTION
FAKE_LINT_SLUG_FIXED = (
    LINT_SLUG_SEED.replace(
        "from collections.abc import Iterable, Callable\n\n\n"
        "# ---------------------------------------------------------------------------\n"
        "# Normalización\n"
        "# ---------------------------------------------------------------------------\n\n",
        "from collections.abc import Callable, Iterable\n\n",
    )
    + _LINT_FUNCTION
)
FAKE_LINT_TESTS = '''\
"""Pruebas unitarias de ``slugify_many`` (complementan las de aceptación)."""

from textkit.slug import slugify_many


def test_separator_is_propagated_and_empty_items_dropped() -> None:
    assert slugify_many(["Uno Dos", "", "¡¡"], separator="_") == ["uno_dos"]


def test_generator_input_is_consumed_once() -> None:
    assert slugify_many(x for x in ["A", "B"]) == ["a", "b"]
'''


@dataclass(frozen=True)
class DemoProfile:
    repository: str
    task: str
    writable: tuple[str, ...]
    read: tuple[str, ...]
    files: dict[str, str]

    @property
    def project(self) -> str:
        return self.repository


PROFILES: dict[str, DemoProfile] = {
    "mailcheck": DemoProfile(
        DEMO_REPOSITORY, DEFAULT_TASK, DEFAULT_WRITABLE, DEFAULT_READ, DEMO_FILES
    ),
    "lint": DemoProfile(LINT_REPOSITORY, LINT_TASK, LINT_WRITABLE, LINT_READ, LINT_DEMO_FILES),
}

# Canned answers for ``--fake-model``: a correct implementation that lets the
# whole pipeline (write_file, ruff and pytest in the real runner, audit,
# checkpoints) be exercised without a model. Clearly not an agent result.
FAKE_VALIDATOR = '''\
"""Validación de direcciones de correo electrónico según las reglas de README.md."""

import re

MAX_ADDRESS_LENGTH = 254
MAX_LOCAL_LENGTH = 64
_LOCAL_RE = re.compile(r"^[A-Za-z0-9._%+-]+$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9-]{1,63}$")
_TLD_RE = re.compile(r"^[A-Za-z]{2,63}$")


def validate_email(address: str) -> bool:
    """Devuelve True si ``address`` cumple las reglas descritas en README.md."""
    if not isinstance(address, str):
        raise TypeError("address debe ser str")
    if len(address) > MAX_ADDRESS_LENGTH or address.count("@") != 1:
        return False
    if any(character.isspace() for character in address):
        return False
    local, domain = address.split("@")
    if not local or not domain or len(local) > MAX_LOCAL_LENGTH:
        return False
    if not _LOCAL_RE.match(local) or local.startswith(".") or local.endswith("."):
        return False
    if ".." in local:
        return False
    labels = domain.split(".")
    if len(labels) < 2:
        return False
    for label in labels:
        if not _LABEL_RE.match(label) or label.startswith("-") or label.endswith("-"):
            return False
    return _TLD_RE.match(labels[-1]) is not None
'''

FAKE_TESTS = '''"""Pruebas unitarias de ``validate_email`` (complementan las de aceptación)."""

import pytest

from mailcheck.validator import MAX_ADDRESS_LENGTH, MAX_LOCAL_LENGTH, validate_email


@pytest.mark.parametrize(
    "address",
    ["simple@example.com", "UPPER@EXAMPLE.COM", "with+plus@example.com", "a@b.cd"],
)
def test_valid_addresses(address: str) -> None:
    assert validate_email(address) is True


@pytest.mark.parametrize(
    "address",
    ["no-at-sign", "two@@example.com", "tab\\t@example.com", "user@example.1a", "user@.com"],
)
def test_invalid_addresses(address: str) -> None:
    assert validate_email(address) is False


def test_length_limits_are_inclusive() -> None:
    local = "a" * MAX_LOCAL_LENGTH
    assert validate_email(f"{local}@example.com") is True
    assert validate_email(f"{local}a@example.com") is False
    domain = ("b" * 63 + ".") * 2 + "c" * 57 + ".com"
    address = "a" * (MAX_ADDRESS_LENGTH - len(domain) - 1) + "@" + domain
    assert len(address) == MAX_ADDRESS_LENGTH and len(address.split("@")[0]) == MAX_LOCAL_LENGTH
    assert validate_email(address) is True
    assert validate_email("a" + address) is False


def test_non_string_raises_type_error() -> None:
    with pytest.raises(TypeError):
        validate_email(123)  # type: ignore[arg-type]
'''

_SEED_ENV_KEYS = ("PATH",)


def write_demo_tree(work: Path, files: dict[str, str] | None = None) -> None:
    for relative, content in (files or DEMO_FILES).items():
        target = work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def seed_demo_repository(
    root: Path, name: str = DEMO_REPOSITORY, files: dict[str, str] | None = None
) -> Path:
    """A bare ``<name>.git`` under the managed root with a demo fixture. Idempotent.

    Uses ``git`` with a scratch HOME so no personal configuration or credential
    helper is involved; no remote is ever configured.
    """
    target = root / f"{name}.git"
    if target.exists():
        return target
    root.mkdir(parents=True, exist_ok=True)
    scratch = root / f".{name}-seed"
    work = scratch / "work"
    work.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k in _SEED_ENV_KEYS}
    env.update(
        HOME=str(scratch),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="AgentForge",
        GIT_AUTHOR_EMAIL="agentforge@noreply.agentforge.local",
        GIT_COMMITTER_NAME="AgentForge",
        GIT_COMMITTER_EMAIL="agentforge@noreply.agentforge.local",
    )

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), env=env, check=True, capture_output=True)

    try:
        git("init", "-q", "-b", "main", str(work), cwd=scratch)
        write_demo_tree(work, files)
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", f"seed: {name} fixture for AgentForge Fase 6", cwd=work)
        git("clone", "-q", "--bare", str(work), str(target), cwd=root)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return target


def register_in_file(file: Path, config: RepositoryConfig) -> None:
    """Append ``config`` to the operator's ``GIT_REPOSITORIES_FILE`` (atomic rewrite)."""
    data: dict[str, Any] = {"repositories": []}
    if file.exists():
        loaded = json.loads(file.read_text(encoding="utf-8"))
        data = loaded if isinstance(loaded, dict) else {"repositories": loaded}
    entries = [e for e in data.get("repositories", []) if e.get("id") != config.id]
    entries.append(
        {
            "id": config.id,
            "project_id": config.project_id,
            "path": str(config.path),
            "authorized_test_paths": sorted(config.authorized_test_paths),
        }
    )
    data["repositories"] = entries
    temporary = file.with_suffix(file.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, file)


def fake_model(profile: str = "mailcheck") -> FakeLLMAdapter:
    adapter = FakeLLMAdapter()
    if profile == "lint":
        adapter.register_sequence(
            DeveloperChanges,
            [
                DeveloperChanges(
                    summary="(modelo simulado) añade slugify_many; conserva los imports",
                    edits=[
                        FileEdit(path="src/textkit/slug.py", content=FAKE_LINT_SLUG_TRAP),
                        FileEdit(path="tests/test_slug_many.py", content=FAKE_LINT_TESTS),
                    ],
                ),
                DeveloperChanges(
                    summary="(modelo simulado) aplica el diff de ruff al bloque de imports",
                    edits=[FileEdit(path="src/textkit/slug.py", content=FAKE_LINT_SLUG_FIXED)],
                ),
            ],
        )
    else:
        adapter.register(
            DeveloperChanges,
            DeveloperChanges(
                summary="(modelo simulado) implementación de referencia y pruebas unitarias",
                edits=[
                    FileEdit(path="src/mailcheck/validator.py", content=FAKE_VALIDATOR),
                    FileEdit(path="tests/test_validator.py", content=FAKE_TESTS),
                ],
            ),
        )
    adapter.register(
        QAReview,
        QAReview(approved=True, summary="(modelo simulado) sin observaciones", issues=[]),
    )
    return adapter


# -- the run ------------------------------------------------------------------------------------


@dataclass
class DevelopmentBudget:
    max_corrections: int = 3
    wall_seconds: float = 1500.0
    model_timeout_seconds: float = 300.0
    max_usd: float = 8.0  # ceiling of the *conservative reservation*, not the expected spend
    scope_ttl_seconds: float = 3600.0
    ruff_diff: bool = False  # requires a runner that knows ``ruff_check_diff``

    @property
    def max_calls(self) -> int:
        return 2 + 2 * self.max_corrections

    @property
    def max_reserved_tokens(self) -> int:
        return int(self.max_usd * 1_000_000 / 20.0)


@dataclass
class DevelopmentDemoDeps:
    sandbox: SandboxPort
    git: GitRepositoryPort
    registry: RepositoryRegistry
    workspaces: WorktreeManager
    audit: ToolInvocationRepository
    runs: RunRepository
    llm: LLMPort
    model_name: str
    limits: ToolLimits
    execution_policy: ExecutionPolicy
    confirm: Callable[[str], bool]
    checkpointer: BaseCheckpointSaver[Any] | None = None
    recovery: RecoveryPolicy = field(default_factory=RecoveryPolicy)
    out: Callable[[str], None] = print


@dataclass
class DevelopmentReport:
    run_id: str
    repository_id: str
    project_id: str
    runner_id: str
    model: str
    worktree: str | None
    branch: str
    status: str
    error_code: str | None
    error_message: str | None
    revision: int
    llm_calls: int
    usage: dict[str, Any]
    estimated_usd: float
    qa_approved: bool
    qa_summary: str
    qa_issues: list[dict[str, Any]]
    validation: list[dict[str, Any]]
    changed_files: list[str]
    patch: str
    patch_truncated: bool
    events: list[dict[str, Any]]
    tool_invocations: list[dict[str, Any]]
    tool_invocations_by_status: dict[str, int]
    uncertain_invocations: int
    worktree_removed: bool


async def run_development(
    deps: DevelopmentDemoDeps,
    *,
    repository_id: str,
    project_id: str,
    task: str = DEFAULT_TASK,
    writable_paths: Sequence[str] = DEFAULT_WRITABLE,
    read_paths: Sequence[str] = DEFAULT_READ,
    lint_paths: Sequence[str] = ("src", "tests"),
    test_paths: Sequence[str] = ("tests",),
    budget: DevelopmentBudget | None = None,
    remove_worktree: bool = False,
) -> DevelopmentReport:
    budget = budget or DevelopmentBudget()
    require_sandbox_project(project_id)
    repository = deps.registry.require(repository_id, project_id)
    runner_id = require_verified_runner(deps.sandbox)
    run_id = uuid.uuid4()
    principal = operator_principal(project_id)
    record = deps.workspaces.ensure(run_id, project_id, repository_id)
    scope = DevelopmentScope(
        run_id=run_id,
        project_id=project_id,
        repository_id=repository.id,
        workspace_root=record.path,
        expires_at=datetime.now(UTC) + timedelta(seconds=budget.scope_ttl_seconds),
        writable_paths=frozenset(writable_paths),
    )
    registry = default_registry(
        deps.limits,
        GitTools(deps.git, deps.workspaces, deps.limits),
        ExecRunTool(deps.sandbox, deps.workspaces, git=deps.git, policy=deps.execution_policy),
    )
    registry.register(WriteFileTool(deps.workspaces, scope))
    gateway = ToolGateway(registry, DevelopmentToolPolicy(scope), deps.audit, limits=deps.limits)
    bounded = BudgetedLLM(
        deps.llm,
        max_calls=budget.max_calls,
        max_reserved_tokens=budget.max_reserved_tokens,
        max_usd=budget.max_usd,
    )
    graph_deps = DevelopmentDeps(
        scope=scope,
        gateway=gateway,
        llm=bounded,
        read_paths=tuple(read_paths),
        lint_paths=tuple(lint_paths),
        test_paths=tuple(test_paths),
        max_corrections=budget.max_corrections,
        wall_seconds=budget.wall_seconds,
        model_timeout_seconds=budget.model_timeout_seconds,
        ruff_diff=budget.ruff_diff,
    )
    graph = build_development_graph(graph_deps, checkpointer=deps.checkpointer)
    owner = f"development-mvp:{uuid.uuid4().hex[:8]}"
    service = RunService(
        graph,
        deps.runs,
        deps.recovery,
        owner=owner,
        authorization=AuthorizationPolicy(separation_of_duties=False),
        workflow_kind=WorkflowKind.DEVELOPMENT,
    )
    summary = json.dumps(
        {
            "run_id": str(run_id),
            "runner_id": runner_id,
            "repository": repository.id,
            "project": project_id,
            "branch": record.branch,
            "worktree": str(record.path),
            "model": deps.model_name,
            "task": task,
            "writable_paths": sorted(writable_paths),
            "read_paths": list(read_paths),
            "max_corrections": budget.max_corrections,
            "max_model_calls": budget.max_calls,
            "reservation_ceiling_usd": budget.max_usd,
            "wall_seconds": budget.wall_seconds,
            "sandbox_limits": deps.execution_policy.limits.model_dump(),
            "developer": "read + write_file(listed) + exec.run(ruff)",
            "qa": "read + exec.run(ruff, pytest)"
            + (", ruff_check_diff" if budget.ruff_diff else ""),
        },
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    removed = False
    try:
        if not deps.confirm(summary):
            raise DevelopmentRefused("not_confirmed", "el operador no confirmó la ejecución")
        deps.out(f"run {run_id}: iniciando Developer → QA sobre {repository.id}")
        try:
            await service.create(task, principal, project_id=project_id, run_id=str(run_id))
        except (asyncio.CancelledError, KeyboardInterrupt):
            await _fail_cancelled(deps.runs, run_id, owner)
            raise
        finally:
            # The run is over, one way or another: nothing may use this authority again.
            scope.revoke()
            await service.shutdown(5.0)
        row = await deps.runs.get_run(run_id)
        assert row is not None
        snapshot = await graph.aget_state({"configurable": {"thread_id": str(run_id)}})
        values: dict[str, Any] = dict(snapshot.values) if snapshot.values else {}
        patch, truncated = _current_patch(deps.git, record.path, values)
        events = await deps.runs.list_events(run_id, after_id=None, limit=500)
        invocations = await deps.audit.list_for_run(run_id, limit=500)
        by_status: dict[str, int] = {}
        for row_i in invocations:
            by_status[row_i.status.value] = by_status.get(row_i.status.value, 0) + 1
        usage = dict(values.get("usage") or {})
        review = values.get("qa_review") or {}
        if row.status in TERMINAL_STATUSES:
            deps.workspaces.release(run_id, project_id, status=row.status)
        if remove_worktree:
            removed = deps.workspaces.remove(run_id, project_id)
        return DevelopmentReport(
            run_id=str(run_id),
            repository_id=repository.id,
            project_id=project_id,
            runner_id=runner_id,
            model=deps.model_name,
            worktree=None if removed else str(record.path),
            branch=record.branch,
            status=row.status.value,
            error_code=row.error.code if row.error else None,
            error_message=row.error.message if row.error else None,
            revision=row.revision,
            llm_calls=row.llm_calls,
            usage=usage,
            estimated_usd=round(
                float(usage.get("input_tokens", 0)) * REFERENCE_USD_PER_M_INPUT / 1e6
                + float(usage.get("output_tokens", 0)) * REFERENCE_USD_PER_M_OUTPUT / 1e6,
                4,
            ),
            qa_approved=bool(review.get("approved")),
            qa_summary=str(review.get("summary", "")),
            qa_issues=list(review.get("issues") or []),
            validation=[
                {k: v for k, v in r.items() if k != "stdout" and k != "stderr"}
                | {"stdout_tail": str(r.get("stdout", ""))[-1200:]}
                for r in values.get("validation") or []
            ],
            changed_files=list(values.get("changed_files") or []),
            patch=patch,
            patch_truncated=truncated,
            events=[
                {"kind": e.kind.value, "ordinal": e.ordinal, "payload": e.payload} for e in events
            ],
            tool_invocations=[
                {
                    "step": i.step,
                    "tool": i.tool_name,
                    "agent": i.agent.value,
                    "risk": i.risk.value if i.risk else None,
                    "status": i.status.value,
                    "error_code": i.error_code,
                    "duration_ms": i.duration_ms,
                }
                for i in invocations
            ],
            tool_invocations_by_status=by_status,
            uncertain_invocations=sum(
                1 for i in invocations if i.status is InvocationStatus.UNCERTAIN
            ),
            worktree_removed=removed,
        )
    except DevelopmentRefused:
        scope.revoke()
        deps.workspaces.remove(run_id, project_id)
        raise


def _current_patch(
    git: GitRepositoryPort, worktree: Path, values: dict[str, Any]
) -> tuple[str, bool]:
    """The patch as the worktree stands now (trusted operator read), falling back to QA's."""
    try:
        result = git.diff(worktree, max_bytes=200_000)
        return result.patch, result.truncated
    except Exception:  # the fallback is the diff QA saw, kept in the checkpoint
        final = values.get("final_diff") or {}
        return str(final.get("patch", "")), bool(final.get("truncated"))


async def _fail_cancelled(runs: RunRepository, run_id: uuid.UUID, owner: str) -> None:
    """Close the row when the operator interrupts; only with the lease this process holds."""
    row = await runs.get_run(run_id)
    if row is None or row.lease is None or row.lease_owner != owner:
        return
    await runs.project(
        run_id,
        RunProjection(
            status=RunLifecycleStatus.FAILED,
            error=RunError(code="operator_cancelled", message="interrumpida por el operador"),
        ),
        lease=row.lease,
        release=True,
    )


def succeeded(report: DevelopmentReport) -> bool:
    return (
        report.status == RunLifecycleStatus.COMPLETED.value
        and report.qa_approved
        and bool(report.validation)
        and all(r.get("exit_code") == 0 for r in report.validation)
        and report.uncertain_invocations == 0
        and bool(report.changed_files)
    )


# -- entry point -----------------------------------------------------------------------------


def _confirm(summary: str) -> bool:
    print("Se ejecutará la siguiente tarea de desarrollo:\n" + summary, file=sys.stderr)
    answer = input(f"Escribe '{CONFIRM_WORD}' para continuar: ")
    return answer.strip() == CONFIRM_WORD


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--profile", choices=sorted(PROFILES), default="mailcheck")
    parser.add_argument("--repository", default=None, help="por defecto el del perfil")
    parser.add_argument("--project", default=None, help="por defecto el del perfil")
    parser.add_argument("--task", default=None, help="por defecto la del perfil")
    parser.add_argument("--writable", nargs="*", default=None)
    parser.add_argument("--read", nargs="*", default=None)
    parser.add_argument("--max-corrections", type=int, default=3)
    parser.add_argument("--wall-seconds", type=float, default=1500.0)
    parser.add_argument("--max-usd", type=float, default=8.0)
    parser.add_argument(
        "--ruff-diff",
        action="store_true",
        help="pedir a QA ruff check --diff cuando ruff falle (runner con ruff_check_diff)",
    )
    parser.add_argument(
        "--model-timeout-seconds",
        type=float,
        default=None,
        help="por defecto: LLM_TIMEOUT_SECONDS x (1 + reintentos) x (1 + reparaciones) + 30",
    )
    parser.add_argument("--seed-demo", action="store_true", help="crea y registra sandbox-dev")
    parser.add_argument("--fake-model", action="store_true", help="respuestas simuladas")
    parser.add_argument("--yes", action="store_true", help="no pedir confirmación interactiva")
    parser.add_argument("--remove-worktree", action="store_true")
    parser.add_argument("--patch-out", type=Path, default=None)
    args = parser.parse_args(argv)
    profile = PROFILES[args.profile]
    repository_id = args.repository or profile.repository
    project_id = args.project or profile.project
    task = args.task or profile.task
    writable = tuple(args.writable) if args.writable else profile.writable
    read = tuple(args.read) if args.read else profile.read
    try:
        require_local_environment(get_environment().environment)
        require_sandbox_project(project_id)
        settings = get_settings()
        if not (
            settings.sandbox_runner_url
            and settings.sandbox_runner_token
            and settings.sandbox_runner_id
        ):
            raise DevelopmentRefused(
                "runner_unconfigured",
                "configura SANDBOX_RUNNER_URL, SANDBOX_RUNNER_TOKEN y SANDBOX_RUNNER_ID; "
                "no hay ejecución local",
            )
        if settings.git_repositories_root is None or settings.worktrees_root is None:
            raise DevelopmentRefused("storage", "configura GIT_REPOSITORIES_ROOT y WORKTREES_ROOT")
        if settings.use_fake_model and not args.fake_model:
            raise DevelopmentRefused(
                "fake_model_configured",
                "USE_FAKE_MODEL=true: no es una ejecución real; usa --fake-model explícitamente",
            )
        adapter = LocalGitAdapter(
            binary=settings.git_binary, timeout_seconds=settings.git_timeout_seconds
        )
        repositories_file = settings.git_repositories_file
        registry = (
            RepositoryRegistry.from_file(settings.git_repositories_root, repositories_file)
            if repositories_file is not None and repositories_file.exists()
            else RepositoryRegistry(settings.git_repositories_root)
        )
        if args.seed_demo and registry.get(profile.repository) is None:
            path = seed_demo_repository(
                settings.git_repositories_root, profile.repository, profile.files
            )
            config = RepositoryConfig(id=profile.repository, project_id=profile.project, path=path)
            registry.register(config)
            if repositories_file is not None:
                register_in_file(repositories_file, config)
        manager = WorktreeManager(
            settings.worktrees_root,
            adapter,
            registry,
            policy=WorktreePolicy.from_settings(settings),
        )
        sandbox = RemoteSandboxAdapter.from_settings(settings)
        database = Database.from_settings(settings)
        checkpoints = PostgresCheckpointer.from_settings(settings)
        llm: LLMPort
        if args.fake_model:
            llm, model_name = fake_model(args.profile), "fake"
        else:
            llm, model_name = VertexClaudeAdapter(settings), settings.claude_model
        # Never shorter than the adapter's own worst case (SDK retries plus
        # repair turns): an abandoned model thread would still be billed.
        worst_case = (
            settings.llm_timeout_seconds
            * (1 + settings.llm_max_retries)
            * (1 + settings.llm_max_repair_attempts)
            + 30.0
        )
        budget = DevelopmentBudget(
            max_corrections=args.max_corrections,
            wall_seconds=args.wall_seconds,
            model_timeout_seconds=args.model_timeout_seconds or worst_case,
            max_usd=args.max_usd,
            ruff_diff=args.ruff_diff,
        )

        async def go() -> DevelopmentReport:
            try:
                await verify_schema(database.engine)
                saver = await checkpoints.open()
                await checkpoints.verify_schema()
                deps = DevelopmentDemoDeps(
                    sandbox=sandbox,
                    git=adapter,
                    registry=registry,
                    workspaces=manager,
                    audit=SqlAlchemyToolInvocationRepository(database.session_factory),
                    runs=SqlAlchemyRunRepository(database.session_factory),
                    llm=llm,
                    model_name=model_name,
                    limits=ToolLimits.from_settings(settings),
                    execution_policy=ExecutionPolicy.from_settings(settings),
                    confirm=(lambda _s: True) if args.yes else _confirm,
                    checkpointer=saver,
                    recovery=RecoveryPolicy.from_settings(settings),
                    out=lambda line: print(line, file=sys.stderr),
                )
                return await run_development(
                    deps,
                    repository_id=repository_id,
                    project_id=project_id,
                    task=task,
                    writable_paths=writable,
                    read_paths=read,
                    budget=budget,
                    remove_worktree=args.remove_worktree,
                )
            finally:
                await checkpoints.close()
                await database.dispose()
                sandbox.close()

        report = asyncio.run(go())
    except DevelopmentRefused as exc:
        print(json.dumps({"refused": exc.code, "detail": exc.detail}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"interrupted": True}), file=sys.stderr)
        return 130
    if args.patch_out is not None:
        args.patch_out.write_text(report.patch, encoding="utf-8")
    print(json.dumps(asdict(report), indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if succeeded(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())

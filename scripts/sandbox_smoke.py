"""Operator smoke test: the first end-to-end ``exec.run`` (``ruff_check``) against a
real, verified AgentForge Runner. Never part of the application or of any agent.

    uv run python -m scripts.sandbox_smoke --seed-demo
    uv run python -m scripts.sandbox_smoke --repository sandbox-demo --project sandbox-demo

Guarantees, each enforced in code:

- ``ENVIRONMENT=local`` only; anything else is refused before reading settings.
- A remote runner must be configured (``SANDBOX_RUNNER_URL``, ``_TOKEN``, ``_ID``)
  and must verify its identity, backend and readiness; otherwise nothing runs.
  There is no local execution of any kind.
- The repository must be explicitly registered under ``GIT_REPOSITORIES_ROOT``
  and belong to an isolated ``sandbox-*`` project; ``--seed-demo`` creates a
  throwaway demo repository there. Personal repositories are out of reach.
- The principal is the trusted automation identity for one test agent; the
  worktree is managed and removed at the end.
- A temporary policy, local to this process, grants exactly ``exec.run`` to that
  agent with the ``read`` and ``exec`` risk classes. ``write`` and ``network``
  stay disabled; the application's default policies are not touched; the only
  program ever sent is ``ruff_check``.
- The operator confirms interactively before the execution is submitted.
- The audit lands in PostgreSQL (``runs`` + ``tool_invocations``) and the run
  row is closed as ``failed`` with code ``sandbox_smoke`` so no sweeper picks it up.
- A second identical call must be refused as ``duplicate_invocation``.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from app.contracts.execution import RunError
from app.contracts.lifecycle import RunLifecycleStatus
from app.contracts.tools import AgentKind, RiskClass, ToolCall, ToolOutcome
from app.git import GitRepositoryPort, RepositoryConfig, RepositoryRegistry
from app.infrastructure.db import (
    Database,
    SqlAlchemyRunRepository,
    SqlAlchemyToolInvocationRepository,
    verify_schema,
)
from app.infrastructure.git import LocalGitAdapter
from app.infrastructure.sandbox import RemoteSandboxAdapter
from app.orchestration.ports import RunProjection, RunRepository
from app.sandbox import SandboxPort
from app.security import (
    AgentGrant,
    AuthorizationPolicy,
    AutomationIdentity,
    ToolAuthorizationPolicy,
)
from app.settings import get_environment, get_settings
from app.tools import (
    ExecRunTool,
    ExecutionPolicy,
    ToolContext,
    ToolGateway,
    ToolLimits,
    ToolRegistry,
)
from app.tools.ports import ToolInvocationRepository
from app.workspaces import WorktreeManager, WorktreePolicy

SMOKE_AGENT = AgentKind.DEVELOPER
SMOKE_PROGRAM = "ruff_check"
SMOKE_STEP = "smoke:ruff_check"
PROJECT_PREFIX = "sandbox-"
DEMO_REPOSITORY = "sandbox-demo"
CONFIRM_WORD = "ejecutar"


class SmokeRefused(Exception):
    """The smoke test will not run; ``code`` says why."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# -- guards ------------------------------------------------------------------------------------


def require_local_environment(environment: str) -> None:
    if environment != "local":
        raise SmokeRefused("environment", f"solo en ENVIRONMENT=local (actual: {environment})")


def require_sandbox_project(project_id: str) -> None:
    if not project_id.startswith(PROJECT_PREFIX):
        raise SmokeRefused(
            "project", f"el proyecto debe empezar por '{PROJECT_PREFIX}' (actual: {project_id})"
        )


def smoke_policy() -> ToolAuthorizationPolicy:
    """Exactly exec.run, for exactly one test agent, in read+exec. Nothing else."""
    grants = {
        SMOKE_AGENT: AgentGrant(
            tools=frozenset({"exec.run"}), risks=frozenset({RiskClass.READ, RiskClass.EXEC})
        )
    }
    return ToolAuthorizationPolicy(
        AuthorizationPolicy(), grants, enabled_risks=frozenset({RiskClass.READ, RiskClass.EXEC})
    )


def require_verified_runner(sandbox: SandboxPort) -> str:
    health = sandbox.health()
    if not health.verified:
        raise SmokeRefused("runner_unverified", "el runner no acreditó identidad y backend")
    if not health.ready:
        raise SmokeRefused("runner_not_ready", "el runner no supera su preflight de aislamiento")
    return health.runner_id


# -- demo repository ---------------------------------------------------------------------------

_SEED_ENV_KEYS = ("PATH",)


def seed_demo_repository(root: Path, name: str = DEMO_REPOSITORY) -> Path:
    """A bare repository with one ruff finding, under the managed root. Idempotent.

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

    git("init", "-q", "-b", "main", str(work), cwd=scratch)
    (work / "src").mkdir()
    (work / "tests").mkdir()
    (work / "src" / "app.py").write_text("import os\n\nVALUE = 1\n")  # F401: the expected finding
    (work / "tests" / "test_demo.py").write_text("def test_demo() -> None:\n    assert True\n")
    (work / "README.md").write_text(
        "# sandbox-demo\n\nRepositorio desechable para la prueba de humo.\n"
    )
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "seed", cwd=work)
    git("clone", "-q", "--bare", str(work), str(target), cwd=root)
    import shutil

    shutil.rmtree(scratch)
    return target


# -- the smoke test ---------------------------------------------------------------------------


@dataclass
class SmokeDeps:
    sandbox: SandboxPort
    git: GitRepositoryPort
    registry: RepositoryRegistry
    workspaces: WorktreeManager
    audit: ToolInvocationRepository
    runs: RunRepository
    limits: ToolLimits
    execution_policy: ExecutionPolicy
    confirm: Callable[[str], bool]
    out: Callable[[str], None] = print


@dataclass
class SmokeReport:
    run_id: str
    repository_id: str
    project_id: str
    runner_id: str
    invocation_id: str
    step: str
    outcome: str
    status: str | None
    exit_code: int | None
    duration_ms: int
    stdout_excerpt: str
    error_code: str | None
    duplicate_outcome: str
    duplicate_code: str | None
    audit_rows: int
    worktree_removed: bool


async def run_smoke(
    deps: SmokeDeps, *, repository_id: str, project_id: str, paths: Sequence[str] = ("src",)
) -> SmokeReport:
    require_sandbox_project(project_id)
    repository = deps.registry.require(repository_id, project_id)
    runner_id = require_verified_runner(deps.sandbox)
    run_id = uuid.uuid4()
    principal = AutomationIdentity().principal_for(SMOKE_AGENT, project_id)
    record = deps.workspaces.ensure(run_id, project_id, repository_id)
    removed = False
    try:
        created = await deps.runs.create_run(
            run_id,
            f"sandbox smoke: {SMOKE_PROGRAM} on {repository_id}",
            owner="sandbox-smoke",
            ttl_seconds=600,
            owner_subject="operator:sandbox-smoke",
            project_id=project_id,
        )
        exec_tool = ExecRunTool(
            deps.sandbox, deps.workspaces, git=deps.git, policy=deps.execution_policy
        )
        gateway = ToolGateway(
            ToolRegistry([exec_tool]), smoke_policy(), deps.audit, limits=deps.limits
        )
        context = ToolContext(
            run_id=run_id,
            attempt=0,
            project_id=project_id,
            agent=SMOKE_AGENT,
            principal=principal,
            workspace_root=record.path,
        )
        call = ToolCall(name="exec.run", arguments={"program": SMOKE_PROGRAM, "paths": list(paths)})
        summary = json.dumps(
            {
                "runner_id": runner_id,
                "repository": repository.id,
                "project": project_id,
                "branch": record.branch,
                "program": SMOKE_PROGRAM,
                "paths": list(paths),
                "limits": deps.execution_policy.limits.model_dump(),
                "agent": SMOKE_AGENT.value,
                "principal": principal.subject,
            },
            indent=2,
            sort_keys=True,
        )
        try:
            if not deps.confirm(summary):
                raise SmokeRefused("not_confirmed", "el operador no confirmó la ejecución")
            first = await gateway.invoke(context, call, step=SMOKE_STEP)
            second = await gateway.invoke(context, call, step=SMOKE_STEP)
        finally:
            await deps.runs.project(
                run_id,
                RunProjection(
                    status=RunLifecycleStatus.FAILED,
                    error=RunError(
                        code="sandbox_smoke",
                        message="prueba de humo del operador; no es un run real",
                    ),
                ),
                lease=created.lease,
                release=True,
            )
        rows = await deps.audit.list_for_run(run_id)
        output = first.output or {}
        return SmokeReport(
            run_id=str(run_id),
            repository_id=repository.id,
            project_id=project_id,
            runner_id=runner_id,
            invocation_id=str(first.invocation_id),
            step=first.step,
            outcome=first.outcome.value,
            status=str(output.get("status")) if output else None,
            exit_code=output.get("exit_code") if output else None,
            duration_ms=first.duration_ms,
            stdout_excerpt=str(output.get("stdout", ""))[:500],
            error_code=first.error.code if first.error else None,
            duplicate_outcome=second.outcome.value,
            duplicate_code=second.error.code if second.error else None,
            audit_rows=len(rows),
            worktree_removed=False,
        )
    finally:
        removed = deps.workspaces.remove(run_id, project_id)
        deps.out(f"worktree eliminado: {removed}")


def succeeded(report: SmokeReport) -> bool:
    return (
        report.outcome == ToolOutcome.OK.value
        and report.status == "completed"
        and report.duplicate_outcome == ToolOutcome.DENIED.value
        and report.duplicate_code == "duplicate_invocation"
    )


# -- entry point -----------------------------------------------------------------------------


def _confirm(summary: str) -> bool:
    print("Se enviará al runner la siguiente ejecución:\n" + summary, file=sys.stderr)
    answer = input(f"Escribe '{CONFIRM_WORD}' para continuar: ")
    return answer.strip() == CONFIRM_WORD


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repository", default=DEMO_REPOSITORY)
    parser.add_argument("--project", default=DEMO_REPOSITORY)
    parser.add_argument("--paths", nargs="*", default=["src"])
    parser.add_argument(
        "--seed-demo",
        action="store_true",
        help="crea <GIT_REPOSITORIES_ROOT>/sandbox-demo.git si falta",
    )
    args = parser.parse_args(argv)
    try:
        require_local_environment(get_environment().environment)
        require_sandbox_project(args.project)
        settings = get_settings()
        if not (
            settings.sandbox_runner_url
            and settings.sandbox_runner_token
            and settings.sandbox_runner_id
        ):
            raise SmokeRefused(
                "runner_unconfigured",
                "configura SANDBOX_RUNNER_URL, SANDBOX_RUNNER_TOKEN y SANDBOX_RUNNER_ID; "
                "no hay ejecución local",
            )
        if settings.git_repositories_root is None or settings.worktrees_root is None:
            raise SmokeRefused("storage", "configura GIT_REPOSITORIES_ROOT y WORKTREES_ROOT")
        adapter = LocalGitAdapter(
            binary=settings.git_binary, timeout_seconds=settings.git_timeout_seconds
        )
        registry = (
            RepositoryRegistry.from_file(
                settings.git_repositories_root, settings.git_repositories_file
            )
            if settings.git_repositories_file is not None
            and settings.git_repositories_file.exists()
            else RepositoryRegistry(settings.git_repositories_root)
        )
        if args.seed_demo and registry.get(DEMO_REPOSITORY) is None:
            path = seed_demo_repository(settings.git_repositories_root)
            registry.register(
                RepositoryConfig(id=DEMO_REPOSITORY, project_id=DEMO_REPOSITORY, path=path)
            )
        manager = WorktreeManager(
            settings.worktrees_root,
            adapter,
            registry,
            policy=WorktreePolicy.from_settings(settings),
        )
        sandbox = RemoteSandboxAdapter.from_settings(settings)
        database = Database.from_settings(settings)

        async def go() -> SmokeReport:
            try:
                await verify_schema(database.engine)
                deps = SmokeDeps(
                    sandbox=sandbox,
                    git=adapter,
                    registry=registry,
                    workspaces=manager,
                    audit=SqlAlchemyToolInvocationRepository(database.session_factory),
                    runs=SqlAlchemyRunRepository(database.session_factory),
                    limits=ToolLimits.from_settings(settings),
                    execution_policy=ExecutionPolicy.from_settings(settings),
                    confirm=_confirm,
                    out=lambda line: print(line, file=sys.stderr),
                )
                return await run_smoke(
                    deps, repository_id=args.repository, project_id=args.project, paths=args.paths
                )
            finally:
                await database.dispose()
                sandbox.close()

        report = asyncio.run(go())
    except SmokeRefused as exc:
        print(json.dumps({"refused": exc.code, "detail": exc.detail}), file=sys.stderr)
        return 2
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0 if succeeded(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())

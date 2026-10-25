import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import api_router, health
from app.api.errors import register_exception_handlers
from app.api.headers import SecurityHeadersMiddleware
from app.git import RepositoryRegistry
from app.infrastructure.checkpoints import PostgresCheckpointer
from app.infrastructure.db import (
    Database,
    DatabaseUnavailableError,
    InMemoryRunRepository,
    InMemoryToolInvocationRepository,
    SqlAlchemyRunRepository,
    SqlAlchemyToolInvocationRepository,
    verify_schema,
)
from app.infrastructure.git import LocalGitAdapter
from app.infrastructure.llm import build_llm
from app.infrastructure.llm.port import LLMPort
from app.infrastructure.sandbox import RemoteSandboxAdapter, UnconfiguredSandbox
from app.orchestration.graph import build_graph
from app.orchestration.limits import RunLimits
from app.orchestration.policy import RecoveryPolicy
from app.orchestration.ports import RunRepository
from app.orchestration.recovery import RecoveryService
from app.orchestration.runs import RunService
from app.sandbox import SandboxPort
from app.security import AuthorizationPolicy, JwtVerifier, TokenVerifier, ToolAuthorizationPolicy
from app.security.tokens import DisabledAuthentication
from app.settings import get_environment, get_settings
from app.tools import (
    ExecRunTool,
    ExecutionPolicy,
    GitTools,
    ToolGateway,
    ToolInvocationRepository,
    ToolLimits,
    ToolRegistry,
    default_registry,
)
from app.workspaces import WorktreeManager, WorktreePolicy

logger = logging.getLogger(__name__)

DEFAULT_SHUTDOWN_GRACE_SECONDS = 30.0


async def _check_database_on_startup(app: FastAPI, database: Database) -> None:
    """Connectivity is soft, schema is strict.

    A database that is merely down must not crash-loop the app: readiness
    reports 503 until it is back. A database that answers with the wrong
    Alembic revision is a deployment error and aborts startup.
    """
    try:
        await database.ping()
    except DatabaseUnavailableError as exc:
        logger.warning(
            "PostgreSQL no alcanzable al arrancar (%s); /health/ready responderá 503: %s",
            database.display_url,
            exc.message,
        )
        return
    await verify_schema(database.engine)
    app.state.schema_verified = True


async def _check_checkpoints_on_startup(app: FastAPI, checkpoints: PostgresCheckpointer) -> None:
    """Same policy for the LangGraph tables: unreachable = degraded, missing = abort."""
    try:
        await checkpoints.ping()
    except DatabaseUnavailableError as exc:
        logger.warning(
            "Pool de checkpoints sin conexión al arrancar; /health/ready responderá 503: %s",
            exc.message,
        )
        return
    await checkpoints.verify_schema()
    app.state.checkpoints_verified = True


def create_app(
    llm: LLMPort | None = None,
    limits: RunLimits | None = None,
    database: Database | None = None,
    checkpoints: PostgresCheckpointer | None = None,
    repository: RunRepository | None = None,
    policy: RecoveryPolicy | None = None,
    token_verifier: TokenVerifier | None = None,
    authorization: AuthorizationPolicy | None = None,
    *,
    tool_registry: ToolRegistry | None = None,
    tool_audit: ToolInvocationRepository | None = None,
    tool_limits: ToolLimits | None = None,
    worktrees: WorktreeManager | None = None,
    sandbox: SandboxPort | None = None,
    docs_enabled: bool | None = None,
    shutdown_grace_seconds: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
    startup_sweep: bool | None = None,
    recovery_sweep_seconds: float | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    Wiring happens in ``lifespan`` (not at import time) and lands on
    ``app.state``. With no ``llm`` everything comes from ``Settings``
    (production). Tests pass a ``FakeLLMAdapter``, explicit ``RunLimits`` and,
    when they need PostgreSQL, a ``Database`` and an unopened
    ``PostgresCheckpointer``; the app opens and closes both and persists runs
    through ``SqlAlchemyRunRepository``. Unit tests pass neither: the graph
    then checkpoints in memory, runs live in ``InMemoryRunRepository`` and
    Settings are never read. Recovery sweeps are opt-in outside production.

    Authentication: production builds the verifier from Settings (OIDC, or
    ``AUTH_MODE=disabled`` where Settings allow it). Tests inject a
    ``TokenVerifier``; when they inject none, authentication is disabled
    for that app, which is only possible because it never runs in production.

    Tools (Fase 5): the gateway is built here with the read-only registry and
    the invocation audit (PostgreSQL or in memory); no agent calls it yet.
    Git tools appear only when managed repositories are configured
    (``GIT_REPOSITORIES_FILE``) or a ``WorktreeManager`` is injected; so does
    ``exec.run``, which talks to the remote sandbox runner when one is
    configured (``SANDBOX_RUNNER_URL``) and refuses otherwise.
    """
    production = llm is None
    production_like = production and get_environment().is_production_like
    if docs_enabled is None:
        # Only the environment is read here; full Settings are validated in lifespan.
        docs_enabled = not production_like
    run_startup_sweep = production if startup_sweep is None else startup_sweep

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        port, run_limits, db, cp, run_policy = llm, limits, database, checkpoints, policy
        verifier: TokenVerifier | None = token_verifier
        authz: AuthorizationPolicy | None = authorization
        grace, sweep_every = shutdown_grace_seconds, recovery_sweep_seconds
        limits_for_tools = tool_limits
        manager = worktrees
        sandbox_port = sandbox
        execution_policy = ExecutionPolicy()
        if port is None:
            settings = get_settings()
            port = build_llm(settings)
            limits_for_tools = limits_for_tools or ToolLimits.from_settings(settings)
            execution_policy = ExecutionPolicy.from_settings(settings)
            if sandbox_port is None and settings.sandbox_runner_url is not None:
                sandbox_port = RemoteSandboxAdapter.from_settings(settings)
            if manager is None and settings.git_repositories_file is not None:
                assert settings.git_repositories_root is not None  # validated by Settings
                assert settings.worktrees_root is not None
                manager = WorktreeManager(
                    settings.worktrees_root,
                    LocalGitAdapter(
                        binary=settings.git_binary, timeout_seconds=settings.git_timeout_seconds
                    ),
                    RepositoryRegistry.from_file(
                        settings.git_repositories_root, settings.git_repositories_file
                    ),
                    policy=WorktreePolicy.from_settings(settings),
                )
            if verifier is None:
                verifier = (
                    JwtVerifier.from_settings(settings)
                    if settings.auth_mode == "oidc"
                    else DisabledAuthentication()
                )
            if authz is None:
                authz = AuthorizationPolicy(separation_of_duties=settings.separation_of_duties)
            run_limits = run_limits or RunLimits(
                max_revisions=settings.max_plan_revisions,
                max_llm_calls=settings.max_llm_calls_per_run,
            )
            db = db or Database.from_settings(settings)
            cp = cp or PostgresCheckpointer.from_settings(settings)
            run_policy = run_policy or RecoveryPolicy.from_settings(settings)
            grace = settings.shutdown_grace_seconds
            if sweep_every is None:
                sweep_every = settings.recovery_sweep_seconds

        saver = await cp.open() if cp is not None else None
        repo: RunRepository = repository or (
            SqlAlchemyRunRepository(db.session_factory)
            if db is not None
            else InMemoryRunRepository()
        )
        service = RunService(
            build_graph(port, run_limits or RunLimits(), checkpointer=saver),
            repo,
            run_policy or RecoveryPolicy(),
            # Outside production (tests) the local default allows self-approval,
            # matching a single developer; production derives it from Settings.
            authorization=authz or AuthorizationPolicy(separation_of_duties=False),
        )
        recovery = RecoveryService(service)
        limits_for_tools = limits_for_tools or ToolLimits()
        sandbox_port = sandbox_port or UnconfiguredSandbox()
        git_tools = (
            GitTools(manager.git, manager, limits_for_tools) if manager is not None else None
        )
        exec_tool = (
            ExecRunTool(sandbox_port, manager, git=manager.git, policy=execution_policy)
            if manager is not None
            else None
        )
        tool_gateway = ToolGateway(
            tool_registry or default_registry(limits_for_tools, git_tools, exec_tool),
            ToolAuthorizationPolicy(service.authorization),
            tool_audit
            or (
                SqlAlchemyToolInvocationRepository(db.session_factory)
                if db is not None
                else InMemoryToolInvocationRepository()
            ),
            limits=limits_for_tools,
        )
        app.state.run_service = service
        app.state.tool_gateway = tool_gateway
        app.state.worktrees = manager
        app.state.sandbox = sandbox_port
        app.state.repository = repo
        app.state.recovery = recovery
        app.state.token_verifier = verifier or DisabledAuthentication()
        app.state.authorization = service.authorization
        app.state.database = db
        app.state.checkpoints = cp
        app.state.schema_verified = False
        app.state.checkpoints_verified = False
        sweeper: asyncio.Task[None] | None = None
        try:
            if db is not None:
                await _check_database_on_startup(app, db)
            if cp is not None:
                await _check_checkpoints_on_startup(app, cp)
            if run_startup_sweep and app.state.schema_verified:
                # Off the startup path: a sweep may re-drive runs (LLM calls).
                asyncio.create_task(recovery.sweep(), name="recovery-startup-sweep")
            if sweep_every and sweep_every > 0:
                sweeper = asyncio.create_task(
                    recovery.run_periodically(sweep_every), name="recovery-sweeper"
                )
            yield
        finally:
            # Ordered shutdown: stop sweeping, stop accepting, drain within the
            # grace period, cancel the rest, release only our leases, then close.
            if sweeper is not None:
                sweeper.cancel()
                await asyncio.gather(sweeper, return_exceptions=True)
            report = await service.shutdown(grace)
            if not report.drained:
                logger.warning(
                    "Apagado tras %.0fs con ejecuciones canceladas %s; leases liberados: %d",
                    grace,
                    report.cancelled,
                    report.leases_released,
                )
            if cp is not None:
                await cp.close()
            if db is not None:
                await db.dispose()

    app = FastAPI(
        title="AgentForge",
        version="0.4.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    register_exception_handlers(app)
    app.include_router(api_router)
    app.include_router(health.router)
    # Outermost: applies to every response, including Problem Details and docs.
    app.add_middleware(SecurityHeadersMiddleware, hsts=production_like)
    return app


app = create_app()

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import api_router, health
from app.api.errors import register_exception_handlers
from app.infrastructure.checkpoints import PostgresCheckpointer
from app.infrastructure.db import (
    Database,
    DatabaseUnavailableError,
    InMemoryRunRepository,
    SqlAlchemyRunRepository,
    verify_schema,
)
from app.infrastructure.llm import build_llm
from app.infrastructure.llm.port import LLMPort
from app.orchestration.graph import build_graph
from app.orchestration.limits import RunLimits
from app.orchestration.policy import RecoveryPolicy
from app.orchestration.ports import RunRepository
from app.orchestration.recovery import RecoveryService
from app.orchestration.runs import RunService
from app.settings import get_environment, get_settings

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
    *,
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
    """
    production = llm is None
    if docs_enabled is None:
        # Only the environment is read here; full Settings are validated in lifespan.
        docs_enabled = not production or not get_environment().is_production_like
    run_startup_sweep = production if startup_sweep is None else startup_sweep

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        port, run_limits, db, cp, run_policy = llm, limits, database, checkpoints, policy
        grace, sweep_every = shutdown_grace_seconds, recovery_sweep_seconds
        if port is None:
            settings = get_settings()
            port = build_llm(settings)
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
        )
        recovery = RecoveryService(service)
        app.state.run_service = service
        app.state.repository = repo
        app.state.recovery = recovery
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
        version="0.3.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    register_exception_handlers(app)
    app.include_router(api_router)
    app.include_router(health.router)
    return app


app = create_app()

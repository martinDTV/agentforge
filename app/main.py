import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import api_router, health
from app.api.errors import register_exception_handlers
from app.api.headers import SecurityHeadersMiddleware
from app.contracts.lifecycle import WorkflowKind
from app.git import RepositoryRegistry
from app.goals import GoalRepository, IntegrationProbe
from app.infrastructure.checkpoints import PostgresCheckpointer
from app.infrastructure.db import (
    Database,
    DatabaseUnavailableError,
    InMemoryGoalRepository,
    InMemoryMirrorSyncRepository,
    InMemoryPublicationRepository,
    InMemoryRouteDecisionRepository,
    InMemoryRunRepository,
    InMemoryToolInvocationRepository,
    SqlAlchemyGoalRepository,
    SqlAlchemyMirrorSyncRepository,
    SqlAlchemyNotificationOutbox,
    SqlAlchemyPublicationRepository,
    SqlAlchemyRouteDecisionRepository,
    SqlAlchemyRunRepository,
    SqlAlchemyToolInvocationRepository,
    verify_schema,
)
from app.infrastructure.git import LocalGitAdapter
from app.infrastructure.git.mirror import MirrorService
from app.infrastructure.git.publish import GitPublisher
from app.infrastructure.github import (
    GitHubAppCredentials,
    GitHubClient,
    GitHubPublisher,
    InstallationTokenProvider,
)
from app.infrastructure.github.integration import GitHubIntegrationProbe
from app.infrastructure.llm import build_llm
from app.infrastructure.llm.port import LLMPort
from app.infrastructure.notifications import SmtpEmailSender
from app.infrastructure.sandbox import RemoteSandboxAdapter, UnconfiguredSandbox
from app.notifications import (
    EmailSender,
    NotificationEnqueuer,
    NotificationOutbox,
    NotificationPolicy,
    NotificationWorker,
    PublicLinks,
)
from app.orchestration.goals import GoalCoordinator, GoalService, SupervisorLimits
from app.orchestration.graph import RunGraph, build_graph
from app.orchestration.limits import RunLimits
from app.orchestration.policy import RecoveryPolicy
from app.orchestration.ports import PersistedRun, RunRepository
from app.orchestration.publication import PublicationRuntime
from app.orchestration.recovery import RecoveryService
from app.orchestration.runs import RunService, TerminalHook
from app.orchestration.scoped_development import (
    DevelopmentRuntime,
    build_scoped_development_graph,
)
from app.projects import ProjectPolicyRegistry
from app.routing import SmartRouter
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
    projects: ProjectPolicyRegistry | None = None,
    mirrors: MirrorService | None = None,
    publisher: PublicationRuntime | None = None,
    router: SmartRouter | None = None,
    notifications: NotificationEnqueuer | None = None,
    outbox: NotificationOutbox | None = None,
    email_sender: EmailSender | None = None,
    docs_enabled: bool | None = None,
    shutdown_grace_seconds: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
    startup_sweep: bool | None = None,
    recovery_sweep_seconds: float | None = None,
    goal_repository: GoalRepository | None = None,
    integration_probe: IntegrationProbe | None = None,
    supervisor_limits: SupervisorLimits | None = None,
    supervisor_tick_seconds: float | None = None,
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
        policies = projects
        mirror_service = mirrors
        publication_runtime = publisher
        smart_router = router
        enqueuer = notifications
        notification_outbox = outbox
        sender = email_sender
        worker_interval = 15.0
        worker_max_attempts = 6
        publication_parts: tuple[LocalGitAdapter, GitHubClient, InstallationTokenProvider] | None
        publication_parts = None
        github_parts: tuple[LocalGitAdapter, GitHubClient, InstallationTokenProvider] | None
        github_parts = None
        goals_repo = goal_repository
        probe = integration_probe
        supervisor_on = True
        supervisor_tick = supervisor_tick_seconds
        supervisor_policy = supervisor_limits
        publish_allowed: frozenset[str] = frozenset()
        publish_distinct = False
        execution_policy = ExecutionPolicy()
        if port is None:
            settings = get_settings()
            port = build_llm(settings)
            limits_for_tools = limits_for_tools or ToolLimits.from_settings(settings)
            execution_policy = ExecutionPolicy.from_settings(settings)
            if sandbox_port is None and settings.sandbox_runner_url is not None:
                sandbox_port = RemoteSandboxAdapter.from_settings(settings)
            if policies is None and settings.projects_file is not None:
                policies = ProjectPolicyRegistry.from_file(settings.projects_file)
            if manager is None and settings.git_repositories_file is not None:
                assert settings.git_repositories_root is not None  # validated by Settings
                assert settings.worktrees_root is not None
                git_adapter = LocalGitAdapter(
                    binary=settings.git_binary, timeout_seconds=settings.git_timeout_seconds
                )
                registry = RepositoryRegistry.from_file(
                    settings.git_repositories_root, settings.git_repositories_file
                )
                manager = WorktreeManager(
                    settings.worktrees_root,
                    git_adapter,
                    registry,
                    policy=WorktreePolicy.from_settings(settings),
                )
                credentials = GitHubAppCredentials.from_settings(settings)
                if mirror_service is None and credentials is not None and policies is not None:
                    # GitHub App (ADR 0010): credentials and installation tokens stay
                    # inside this service; agents get neither the client nor a token.
                    github = GitHubClient(
                        settings.github_api_url, timeout_seconds=settings.github_timeout_seconds
                    )
                    # Write scopes exist only behind GITHUB_WRITE_ENABLED (ADR 0011):
                    # the same provider serves read tokens for mirrors and, when the
                    # flag is on, the Publisher's narrowed write tokens.
                    tokens = InstallationTokenProvider(
                        github, credentials, write_enabled=settings.github_write_enabled
                    )
                    mirror_service = MirrorService(
                        git=git_adapter,
                        repositories_root=settings.git_repositories_root,
                        policies=policies,
                        github=github,
                        tokens=tokens,
                        registry=registry,
                        fetch_timeout_seconds=settings.mirror_fetch_timeout_seconds,
                    )
                    github_parts = (git_adapter, github, tokens)
                    if settings.github_write_enabled:
                        publication_parts = (git_adapter, github, tokens)
                        publish_allowed = frozenset(settings.publish_allowed_repositories)
                publish_distinct = settings.publish_distinct_approver
            if verifier is None:
                verifier = (
                    JwtVerifier.from_settings(settings)
                    if settings.auth_mode == "oidc"
                    else DisabledAuthentication()
                )
            if authz is None:
                authz = AuthorizationPolicy(
                    separation_of_duties=settings.separation_of_duties,
                    publish_distinct_approver=settings.publish_distinct_approver,
                )
            run_limits = run_limits or RunLimits(
                max_revisions=settings.max_plan_revisions,
                max_llm_calls=settings.max_llm_calls_per_run,
            )
            if smart_router is None:
                smart_router = SmartRouter(
                    port, confidence_threshold=settings.router_confidence_threshold
                )
            if settings.agentforge_email_enabled:
                # Notifications: rendered drafts land in the outbox inside the event
                # transaction; the worker below delivers them with SMTP over STARTTLS.
                assert settings.agentforge_smtp_username is not None
                assert settings.agentforge_smtp_app_password is not None
                assert settings.agentforge_email_from is not None
                enqueuer = enqueuer or NotificationEnqueuer(
                    NotificationPolicy(
                        links=PublicLinks.resolve(
                            settings.public_app_url, legacy=settings.agentforge_public_url
                        )
                    ),
                    recipients=settings.agentforge_email_to,
                    domain=settings.agentforge_email_from.split("@", 1)[-1],
                    level=settings.notifications_level,
                )
                sender = sender or SmtpEmailSender(
                    host=settings.agentforge_smtp_host,
                    port=settings.agentforge_smtp_port,
                    username=settings.agentforge_smtp_username,
                    password=settings.agentforge_smtp_app_password,
                    sender=settings.agentforge_email_from,
                    starttls=settings.agentforge_smtp_starttls,
                )
                worker_interval = settings.notifications_worker_interval_seconds
                worker_max_attempts = settings.notifications_max_attempts
            supervisor_on = settings.supervisor_enabled
            if supervisor_tick is None:
                supervisor_tick = settings.supervisor_tick_seconds
            supervisor_policy = supervisor_policy or SupervisorLimits.from_settings(settings)
            db = db or Database.from_settings(settings)
            cp = cp or PostgresCheckpointer.from_settings(settings)
            run_policy = run_policy or RecoveryPolicy.from_settings(settings)
            grace = settings.shutdown_grace_seconds
            if sweep_every is None:
                sweep_every = settings.recovery_sweep_seconds

        saver = await cp.open() if cp is not None else None
        repo: RunRepository = repository or (
            SqlAlchemyRunRepository(db.session_factory, notifications=enqueuer)
            if db is not None
            else InMemoryRunRepository(notifications=enqueuer)
        )
        if notification_outbox is None:
            if db is not None:
                notification_outbox = SqlAlchemyNotificationOutbox(db.session_factory)
            elif isinstance(repo, InMemoryRunRepository):
                notification_outbox = repo.outbox
        limits_for_tools = limits_for_tools or ToolLimits()
        sandbox_port = sandbox_port or UnconfiguredSandbox()
        planning_graph = build_graph(port, run_limits or RunLimits(), checkpointer=saver)
        graphs: dict[WorkflowKind, RunGraph] = {WorkflowKind.PLANNING: planning_graph}
        runtime: DevelopmentRuntime | None = None
        if publication_runtime is None and publication_parts is not None:
            git_for_publish, github_for_publish, tokens_for_publish = publication_parts
            publication_runtime = PublicationRuntime(
                git=GitPublisher(
                    git_for_publish,
                    push_timeout_seconds=get_settings().publish_push_timeout_seconds,
                ),
                github=GitHubPublisher(github_for_publish, tokens_for_publish),
                tokens=tokens_for_publish,
                audit=(
                    SqlAlchemyPublicationRepository(db.session_factory)
                    if db is not None
                    else InMemoryPublicationRepository()
                ),
                runs=repo,
                allowed_repositories=publish_allowed,
            )
        if policies is not None and manager is not None:
            # The governed development workflow (ADR 0009) exists only when the
            # operator configured both repositories and their policies. Agent
            # authority is derived per run from the approved scope; the global
            # gateway below stays read-only.
            runtime = DevelopmentRuntime(
                policies=policies,
                workspaces=manager,
                git=manager.git,
                sandbox=sandbox_port,
                audit=tool_audit
                or (
                    SqlAlchemyToolInvocationRepository(db.session_factory)
                    if db is not None
                    else InMemoryToolInvocationRepository()
                ),
                llm=port,
                limits=limits_for_tools,
                execution_policy=execution_policy,
                mirrors=mirror_service,
                mirror_syncs=(
                    SqlAlchemyMirrorSyncRepository(db.session_factory)
                    if db is not None
                    else InMemoryMirrorSyncRepository()
                ),
                publisher=publication_runtime,
                router=smart_router,
                route_decisions=(
                    SqlAlchemyRouteDecisionRepository(db.session_factory)
                    if db is not None
                    else InMemoryRouteDecisionRepository()
                ),
            )
            graphs[WorkflowKind.DEVELOPMENT] = build_scoped_development_graph(
                runtime, checkpointer=saver
            )

        async def release_worktree(row: PersistedRun) -> None:
            if runtime is not None and row.workflow_kind is WorkflowKind.DEVELOPMENT:
                await runtime.release(row.id, row.project_id, row.status)

        # Terminal hooks: worktree release first, then the goal Supervisor (ADR 0012).
        terminal_hooks: list[TerminalHook] = [release_worktree]

        async def on_terminal(row: PersistedRun) -> None:
            for hook in terminal_hooks:
                await hook(row)

        service = RunService(
            planning_graph,
            repo,
            run_policy or RecoveryPolicy(),
            # Outside production (tests) the local default allows self-approval,
            # matching a single developer; production derives it from Settings.
            authorization=authz or AuthorizationPolicy(separation_of_duties=False),
            graphs=graphs,
            policies=policies,
            on_terminal=on_terminal,
        )
        recovery = RecoveryService(service)
        goal_service: GoalService | None = None
        coordinator: GoalCoordinator | None = None
        if runtime is not None and policies is not None and supervisor_on:
            # Goal Supervisor (Fase 8, ADR 0012): deterministic coordination of goals over
            # ordinary runs. Exists only where the development workflow exists.
            if goals_repo is None:
                goals_repo = (
                    SqlAlchemyGoalRepository(db.session_factory, notifications=enqueuer)
                    if db is not None
                    else InMemoryGoalRepository(
                        notifications=enqueuer,
                        outbox=repo.outbox if isinstance(repo, InMemoryRunRepository) else None,
                    )
                )
            if probe is None and mirror_service is not None and github_parts is not None:
                git_for_probe, github_for_probe, tokens_for_probe = github_parts
                probe = GitHubIntegrationProbe(
                    github=github_for_probe,
                    tokens=tokens_for_probe,
                    mirrors=mirror_service,
                    git=git_for_probe,
                    policies=policies,
                    mirror_syncs=runtime.mirror_syncs,
                )
            coordinator = GoalCoordinator(
                goals=goals_repo,
                runs=repo,
                launcher=service,
                policies=policies,
                planning_limits=run_limits or RunLimits(),
                probe=probe,
                limits=supervisor_policy or SupervisorLimits(),
                owner=service.owner,
            )
            goal_service = GoalService(
                coordinator,
                authorization=service.authorization,
                policies=policies,
                runs=repo,
                router=smart_router,
                planning_limits=run_limits or RunLimits(),
            )
            terminal_hooks.append(coordinator.on_run_terminal)
        app.state.goal_service = goal_service
        app.state.goal_coordinator = coordinator
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
        app.state.projects = policies
        app.state.mirrors = mirror_service
        app.state.publisher = publication_runtime
        app.state.notification_outbox = notification_outbox
        notification_worker = (
            NotificationWorker(notification_outbox, sender, max_attempts=worker_max_attempts)
            if notification_outbox is not None and sender is not None
            else None
        )
        app.state.notification_worker = notification_worker
        app.state.notification_task = None
        del publish_distinct  # consumed through ``authz``; kept explicit for readers
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
        mailer: asyncio.Task[None] | None = None
        supervisor: asyncio.Task[None] | None = None
        try:
            if notification_worker is not None:
                mailer = asyncio.create_task(
                    notification_worker.run_forever(worker_interval), name="notification-worker"
                )
                app.state.notification_task = mailer
                logger.info(
                    "notification worker started (every %ss, level=%s)",
                    worker_interval,
                    getattr(enqueuer, "level", "n/a"),
                )
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
            if coordinator is not None and supervisor_tick and supervisor_tick > 0:
                supervisor = asyncio.create_task(
                    coordinator.run_periodically(supervisor_tick), name="goal-supervisor"
                )
            yield
        finally:
            # Ordered shutdown: stop sweeping, stop accepting, drain within the
            # grace period, cancel the rest, release only our leases, then close.
            for background in (sweeper, mailer, supervisor):
                if background is not None:
                    background.cancel()
                    await asyncio.gather(background, return_exceptions=True)
            if coordinator is not None:
                await coordinator.shutdown(grace)
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
        version="0.5.0",
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

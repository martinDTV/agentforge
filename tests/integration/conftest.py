"""PostgreSQL integration fixtures.

A reachable PostgreSQL server is REQUIRED. Default: the local server on
``localhost:5432``, admin database ``postgres``, current OS user (Homebrew
layout). Override with ``TEST_DATABASE_ADMIN_URL`` (SQLAlchemy or libpq form).

Each session creates throwaway databases named ``agentforge_test_<hex>`` and
drops them at the end. When the server is not reachable the suite FAILS with
an explicit message; it never skips silently.
"""

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.infrastructure.checkpoints import PostgresCheckpointer, build_serializer
from app.infrastructure.db import Database
from app.infrastructure.db.schema import alembic_config
from app.main import create_app
from app.orchestration.limits import RunLimits
from app.orchestration.policy import RecoveryPolicy
from app.security import AuthorizationPolicy, JwtVerifier, VerificationRules
from app.security.dev_keys import DevIssuer
from app.security.tokens import StaticKeys
from app.settings import LIBPQ_SCHEME, SQLALCHEMY_SCHEME

DEFAULT_ADMIN_URL = "postgresql+psycopg://localhost:5432/postgres"
DB_PREFIX = "agentforge_test_"
_HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if _HERE in Path(str(item.fspath)).resolve().parents:
            item.add_marker("integration")


def libpq_dsn(url: str) -> str:
    if url.startswith(SQLALCHEMY_SCHEME):
        return LIBPQ_SCHEME + url[len(SQLALCHEMY_SCHEME) :]
    return url


def sqlalchemy_url(url: str) -> str:
    if url.startswith(LIBPQ_SCHEME):
        return SQLALCHEMY_SCHEME + url[len(LIBPQ_SCHEME) :]
    return url


class TemporaryDatabases:
    """Creates isolated databases on the admin server and drops them all at teardown."""

    def __init__(self, admin_url: str) -> None:
        self.admin_url = sqlalchemy_url(admin_url)
        self._names: list[str] = []

    def _admin(self) -> psycopg.Connection:
        return psycopg.connect(libpq_dsn(self.admin_url), autocommit=True, connect_timeout=3)

    def create(self) -> str:
        name = f"{DB_PREFIX}{uuid.uuid4().hex[:12]}"
        with self._admin() as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        self._names.append(name)
        return make_url(self.admin_url).set(database=name).render_as_string(hide_password=False)

    def name_of(self, url: str) -> str:
        return str(make_url(url).database)

    def set_allow_connections(self, url: str, allowed: bool) -> None:
        """Simulate an outage of one database without touching the server."""
        name = self.name_of(url)
        with self._admin() as conn:
            conn.execute(f'ALTER DATABASE "{name}" WITH ALLOW_CONNECTIONS {str(allowed).lower()}')
            if not allowed:
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )

    def drop_all(self) -> None:
        with self._admin() as conn:
            for name in self._names:
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        self._names.clear()


@pytest.fixture(scope="session")
def temporary_databases() -> Iterator[TemporaryDatabases]:
    admin_url = os.environ.get("TEST_DATABASE_ADMIN_URL", DEFAULT_ADMIN_URL)
    try:
        psycopg.connect(libpq_dsn(admin_url), connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.fail(
            f"PostgreSQL no disponible en {make_url(admin_url).render_as_string()}: {exc}\n"
            "Las pruebas de integración requieren un servidor accesible. "
            "Arranca PostgreSQL local o define TEST_DATABASE_ADMIN_URL.",
            pytrace=False,
        )
    manager = TemporaryDatabases(admin_url)
    try:
        yield manager
    finally:
        manager.drop_all()


def migrate(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)


@pytest.fixture
def empty_database_url(temporary_databases: TemporaryDatabases) -> str:
    """A fresh database with no tables at all (function scope)."""
    return temporary_databases.create()


@pytest.fixture(scope="session")
def migrated_database_url(temporary_databases: TemporaryDatabases) -> str:
    """A database at Alembic head, shared by the session (tests must leave it clean)."""
    url = temporary_databases.create()
    migrate(url)
    return url


@pytest.fixture
def fresh_migrated_database_url(temporary_databases: TemporaryDatabases) -> str:
    """A private database at Alembic head (function scope): safe to break on purpose."""
    url = temporary_databases.create()
    migrate(url)
    return url


@pytest.fixture
def stale_schema_database_url(fresh_migrated_database_url: str) -> str:
    """Migrated, then its Alembic revision is overwritten to simulate an old deployment."""
    engine = create_engine(fresh_migrated_database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '9999'"))
    finally:
        engine.dispose()
    return fresh_migrated_database_url


# --- LangGraph checkpoint tables + app factory -------------------------------

CHECKPOINTS_APP_NAME = "agentforge-checkpoints-test"


def setup_checkpoints(url: str) -> None:
    async def _setup() -> None:
        checkpointer = PostgresCheckpointer(libpq_dsn(url))
        await checkpointer.open(wait=True)
        try:
            await checkpointer.setup()
        finally:
            await checkpointer.close()

    asyncio.run(_setup())


@pytest.fixture(scope="session")
def checkpoint_database_url(temporary_databases: TemporaryDatabases) -> str:
    """Business schema at head + LangGraph checkpoint tables, shared by the session."""
    url = temporary_databases.create()
    migrate(url)
    setup_checkpoints(url)
    return url


@pytest.fixture
def fresh_checkpoint_database_url(fresh_migrated_database_url: str) -> str:
    setup_checkpoints(fresh_migrated_database_url)
    return fresh_migrated_database_url


def make_app(
    llm: Any,
    url: str,
    *,
    key: bytes | None = None,
    policy: RecoveryPolicy | None = None,
    startup_sweep: bool = False,
    recovery_sweep_seconds: float = 0,
    shutdown_grace_seconds: float = 30.0,
    issuer: DevIssuer | None = None,
    authorization: AuthorizationPolicy | None = None,
) -> Any:
    """Fully persistent app: SQLAlchemy repository + Postgres checkpointer on ``url``.

    Sweeps are opt-in here because the session database is shared by many
    tests; a surprise sweep would recover another test's fixtures.
    """
    checkpoints = PostgresCheckpointer(
        libpq_dsn(url), serde=build_serializer(key), application_name=CHECKPOINTS_APP_NAME
    )
    verifier = (
        JwtVerifier(
            StaticKeys(issuer.jwks),
            VerificationRules(issuer=issuer.issuer, audience=issuer.audience),
        )
        if issuer is not None
        else None
    )
    return create_app(
        llm,
        RunLimits(),
        database=Database(url),
        checkpoints=checkpoints,
        policy=policy,
        token_verifier=verifier,
        authorization=authorization,
        startup_sweep=startup_sweep,
        recovery_sweep_seconds=recovery_sweep_seconds,
        shutdown_grace_seconds=shutdown_grace_seconds,
    )

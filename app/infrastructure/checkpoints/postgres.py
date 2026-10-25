"""AsyncPostgresSaver wiring: pool lifecycle, schema verification, setup.

The checkpoint tables belong to ``langgraph-checkpoint-postgres`` and are
created by ``setup()``, which runs from ``scripts/setup_checkpoints.py`` as a
deploy step. The application only verifies they exist and are at the version
the installed library expects; it never creates them on startup.
"""

import logging
from typing import Any

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from app.infrastructure.db.errors import DatabaseUnavailableError
from app.settings import DatabaseSettings

from .errors import CheckpointSchemaError
from .serde import build_serializer

logger = logging.getLogger(__name__)

REQUIRED_TABLES: tuple[str, ...] = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)
_MAX_ERROR_CHARS = 300


def expected_migration_version() -> int:
    """Highest ``v`` that ``setup()`` of the installed library records."""
    return len(AsyncPostgresSaver.MIGRATIONS) - 1


def _safe_message(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:_MAX_ERROR_CHARS] or type(exc).__name__


class PostgresCheckpointer:
    """Owns the psycopg pool behind an ``AsyncPostgresSaver``.

    Lifecycle: ``open()`` in the event loop that will drive the graph
    (``lifespan``), ``saver`` injected into ``build_graph``, ``close()`` only
    after in-flight runs have drained. The pool is opened without waiting so a
    database that is down at startup leaves the app degraded (readiness 503)
    instead of crash-looping; psycopg_pool reconnects in the background.
    """

    def __init__(
        self,
        dsn: str,
        *,
        pool_size: int = 5,
        connect_timeout_seconds: float = 5.0,
        serde: SerializerProtocol | None = None,
        ssl_required: bool = False,
        application_name: str = "agentforge-checkpoints",
    ) -> None:
        extra: dict[str, Any] = {
            "application_name": application_name,
            "connect_timeout": max(1, int(connect_timeout_seconds)),
        }
        if ssl_required and "sslmode=" not in dsn:
            extra["sslmode"] = "require"
        self._timeout = connect_timeout_seconds
        self._serde = serde or build_serializer()
        self._pool: AsyncConnectionPool[psycopg.AsyncConnection[dict[str, Any]]] = (
            AsyncConnectionPool(
                conninfo=make_conninfo(dsn, **extra),
                min_size=1,
                max_size=pool_size,
                open=False,
                timeout=connect_timeout_seconds,
                # Required by AsyncPostgresSaver.
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
        )
        self._saver: AsyncPostgresSaver | None = None

    @classmethod
    def from_settings(cls, settings: DatabaseSettings) -> "PostgresCheckpointer":
        return cls(
            settings.database_dsn.get_secret_value(),
            pool_size=settings.checkpoint_pool_size,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
            serde=build_serializer(settings.checkpoint_encryption_key_bytes),
            ssl_required=settings.database_ssl,
        )

    async def open(self, *, wait: bool = False) -> AsyncPostgresSaver:
        await self._pool.open(wait=wait, timeout=self._timeout)
        self._saver = AsyncPostgresSaver(self._pool, serde=self._serde)
        return self._saver

    @property
    def saver(self) -> AsyncPostgresSaver:
        if self._saver is None:
            raise RuntimeError("PostgresCheckpointer no está abierto: llama a open() primero")
        return self._saver

    @property
    def closed(self) -> bool:
        return self._pool.closed

    async def close(self) -> None:
        await self._pool.close()
        self._saver = None

    async def ping(self) -> None:
        try:
            async with self._pool.connection(timeout=self._timeout) as conn:
                await conn.execute("SELECT 1")
        except (psycopg.OperationalError, PoolTimeout) as exc:
            raise DatabaseUnavailableError(_safe_message(exc)) from exc

    async def current_migration_version(self) -> int | None:
        """``None`` when the tables are absent, else the highest recorded migration."""
        try:
            async with self._pool.connection(timeout=self._timeout) as conn:
                for table in REQUIRED_TABLES:
                    cursor = await conn.execute(
                        "SELECT to_regclass(%s) AS oid", (f"public.{table}",)
                    )
                    row = await cursor.fetchone()
                    if row is None or row["oid"] is None:
                        return None
                cursor = await conn.execute("SELECT max(v) AS v FROM checkpoint_migrations")
                row = await cursor.fetchone()
        except (psycopg.OperationalError, PoolTimeout) as exc:
            raise DatabaseUnavailableError(_safe_message(exc)) from exc
        version = row["v"] if row is not None else None
        return int(version) if version is not None else None

    async def verify_schema(self) -> int:
        expected = expected_migration_version()
        current = await self.current_migration_version()
        if current != expected:
            raise CheckpointSchemaError(expected_version=expected, current_version=current)
        return expected

    async def setup(self) -> int:
        """Create or upgrade the checkpoint tables (deploy step, idempotent)."""
        await self.saver.setup()
        return await self.verify_schema()

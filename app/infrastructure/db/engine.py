import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.settings import DatabaseSettings

from .errors import DatabaseUnavailableError, safe_db_error

logger = logging.getLogger(__name__)


class Database:
    """Owns the async engine and the session factory for the lifetime of the app.

    Created in ``lifespan`` (never at import time), stored on ``app.state`` and
    disposed on shutdown. ``pool_pre_ping`` drops dead connections instead of
    handing them to a request; connect and statement timeouts bound how long a
    request can hang on a sick database.
    """

    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 5,
        pool_timeout_seconds: float = 10.0,
        connect_timeout_seconds: float = 5.0,
        statement_timeout_ms: int = 30_000,
        ssl_required: bool = False,
        application_name: str = "agentforge",
    ) -> None:
        connect_args: dict[str, Any] = {
            "connect_timeout": max(1, int(connect_timeout_seconds)),
            "application_name": application_name,
            "options": f"-c statement_timeout={statement_timeout_ms}",
        }
        if ssl_required and "sslmode=" not in url:
            connect_args["sslmode"] = "require"
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout_seconds,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    @classmethod
    def from_settings(cls, settings: DatabaseSettings) -> "Database":
        return cls(
            settings.database_url.get_secret_value(),
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout_seconds=settings.database_pool_timeout_seconds,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
            statement_timeout_ms=settings.database_statement_timeout_ms,
            ssl_required=settings.database_ssl,
        )

    @property
    def display_url(self) -> str:
        return self.engine.url.render_as_string(hide_password=True)

    async def ping(self) -> None:
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise DatabaseUnavailableError(safe_db_error(exc)) from exc

    async def dispose(self) -> None:
        await self.engine.dispose()

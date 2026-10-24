import psycopg
from psycopg_pool import PoolTimeout
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolExhaustedError

_MAX_ERROR_CHARS = 300


class DatabaseUnavailableError(Exception):
    """PostgreSQL could not be reached or answered with a connection-level error.

    The message is the driver's sanitized text (no credentials, no DSN);
    callers map it to a 503 or to a readiness failure, never to a 500.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SchemaMismatchError(Exception):
    """The database is reachable but its Alembic revision is not the code's head.

    Raised at startup so a deployment never serves traffic against a schema it
    was not written for. Migrations run as a separate deploy step, never here.
    """

    def __init__(self, expected: str, current: str | None) -> None:
        self.expected = expected
        self.current = current
        found = current or "ninguna (tabla alembic_version ausente)"
        super().__init__(
            f"Esquema de base de datos desactualizado: se esperaba la revisión "
            f"'{expected}' y se encontró {found}. Ejecuta 'uv run alembic upgrade head'."
        )


def safe_db_error(exc: BaseException) -> str:
    """Driver message without SQL text or stack; psycopg never includes the password."""
    origin = exc.orig if isinstance(exc, DBAPIError) and exc.orig is not None else exc
    return " ".join(str(origin).split())[:_MAX_ERROR_CHARS] or type(origin).__name__


def as_unavailable(exc: BaseException) -> DatabaseUnavailableError | None:
    """Translate *connection-level* failures only.

    Covered: SQLAlchemy ``OperationalError``/``InterfaceError`` (connection
    refused, terminated backend, timeouts), pool exhaustion, and the raw
    psycopg/psycopg_pool errors the LangGraph checkpointer raises. Not covered,
    on purpose: ``ProgrammingError``, ``DataError``, ``IntegrityError`` and any
    other SQL error, which are bugs and must surface as 500s.
    """
    if isinstance(exc, DatabaseUnavailableError):
        return exc
    if isinstance(exc, OperationalError | InterfaceError | PoolExhaustedError):
        return DatabaseUnavailableError(safe_db_error(exc))
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return DatabaseUnavailableError(safe_db_error(exc))
    if isinstance(exc, psycopg.OperationalError | PoolTimeout):
        return DatabaseUnavailableError(safe_db_error(exc))
    if isinstance(exc, SQLAlchemyError):
        return None
    return None

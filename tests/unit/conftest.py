"""Unit-suite isolation: no PostgreSQL, ever.

Every code path that could open a database connection (SQLAlchemy's psycopg
dialect, psycopg_pool behind the checkpointer) goes through
``psycopg.(Async)Connection.connect``. Blocking those here turns an accidental
dependency on PostgreSQL into an immediate, explicit failure.
"""

import psycopg
import pytest

_MESSAGE = (
    "PostgreSQL está bloqueado en la suite unitaria: esa prueba pertenece a tests/integration"
)


@pytest.fixture(autouse=True)
def _block_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _blocked_async(*args: object, **kwargs: object) -> None:
        raise RuntimeError(_MESSAGE)

    def _blocked_sync(*args: object, **kwargs: object) -> None:
        raise RuntimeError(_MESSAGE)

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _blocked_async)
    monkeypatch.setattr(psycopg.Connection, "connect", _blocked_sync)

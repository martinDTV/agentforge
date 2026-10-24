"""The async engine is configured the way the ADR promises."""

import pytest
from sqlalchemy import text

from app.infrastructure.db import Database, DatabaseUnavailableError
from app.settings import Settings


@pytest.mark.anyio
async def test_connection_carries_timeouts_and_application_name(migrated_database_url: str):
    database = Database(
        migrated_database_url,
        statement_timeout_ms=4321,
        application_name="agentforge-tests",
        pool_size=1,
        max_overflow=0,
    )
    try:
        async with database.engine.connect() as conn:
            assert (await conn.execute(text("SHOW statement_timeout"))).scalar() == "4321ms"
            assert (await conn.execute(text("SHOW application_name"))).scalar() == (
                "agentforge-tests"
            )
        await database.ping()
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_from_settings_wires_pool_and_ssl_policy(migrated_database_url: str):
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        anthropic_vertex_project_id="p",
        database_url=migrated_database_url,
        database_pool_size=2,
        database_max_overflow=1,
        environment="local",
    )
    database = Database.from_settings(settings)
    try:
        pool = database.engine.pool
        assert pool.size() == 2  # type: ignore[attr-defined]
        assert pool._max_overflow == 1  # type: ignore[attr-defined]
        assert database.engine.dialect.name == "postgresql"
        assert database.engine.dialect.driver == "psycopg"
        await database.ping()
        assert database.display_url.startswith("postgresql+psycopg://")
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_ping_maps_connection_failures_to_database_unavailable():
    database = Database("postgresql+psycopg://127.0.0.1:1/agentforge", connect_timeout_seconds=1)
    try:
        with pytest.raises(DatabaseUnavailableError) as info:
            await database.ping()
        assert "password" not in info.value.message.lower()
    finally:
        await database.dispose()

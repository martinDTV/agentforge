"""Business-schema verification against the Alembic revision history.

The app never creates or migrates tables: ``alembic upgrade head`` is a
deploy step. What the app does, once it can reach the database, is refuse
to serve traffic when the schema is not the one it was built for.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from .errors import DatabaseUnavailableError, SchemaMismatchError, safe_db_error

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def alembic_config(database_url: str | None = None) -> Config:
    """Alembic config bound to this repository's migrations directory.

    ``database_url`` overrides the Settings-provided URL (used by tests that
    migrate a temporary database). ``%`` is escaped because Config values go
    through configparser interpolation.
    """
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if database_url is not None:
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def expected_head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError(f"No hay migraciones en {MIGRATIONS_DIR / 'versions'}")
    return head


async def current_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        present = await connection.scalar(text("SELECT to_regclass('public.alembic_version')"))
        if present is None:
            return None
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        return str(version) if version is not None else None


async def verify_schema(engine: AsyncEngine) -> str:
    """Return the verified head revision or raise.

    Raises ``DatabaseUnavailableError`` when the database cannot be queried and
    ``SchemaMismatchError`` when it answers with a different revision.
    """
    expected = expected_head_revision()
    try:
        current = await current_revision(engine)
    except SQLAlchemyError as exc:
        raise DatabaseUnavailableError(safe_db_error(exc)) from exc
    if current != expected:
        raise SchemaMismatchError(expected=expected, current=current)
    return expected

"""Create or upgrade the LangGraph checkpoint tables in PostgreSQL.

Deploy step, run next to ``uv run alembic upgrade head``; the application
never does this at startup. Idempotent: ``AsyncPostgresSaver.setup()`` only
applies the library migrations not yet recorded in ``checkpoint_migrations``.

    uv run python -m scripts.setup_checkpoints          # apply
    uv run python -m scripts.setup_checkpoints --check  # exit 0 only if up to date

Reads only the database settings (``DATABASE_URL`` and pool/encryption knobs)
from the environment/.env: no LLM or authentication configuration is needed.
Never prints the DSN.
"""

import argparse
import asyncio
import sys

from app.infrastructure.checkpoints import PostgresCheckpointer, expected_migration_version
from app.settings import get_database_settings


async def run(check_only: bool) -> int:
    settings = get_database_settings()
    checkpointer = PostgresCheckpointer.from_settings(settings)
    await checkpointer.open(wait=True)
    try:
        expected = expected_migration_version()
        current = await checkpointer.current_migration_version()
        print(f"entorno={settings.environment} esperado=v{expected} actual={_fmt(current)}")
        if check_only:
            ok = current == expected
            print("checkpoints: OK" if ok else "checkpoints: DESACTUALIZADO")
            return 0 if ok else 1
        if current == expected:
            print("Nada que hacer: las tablas de checkpoints ya están al día.")
            return 0
        version = await checkpointer.setup()
        print(f"Tablas de checkpoints listas en v{version}.")
        return 0
    finally:
        await checkpointer.close()


def _fmt(version: int | None) -> str:
    return "ausente" if version is None else f"v{version}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="solo verificar, sin crear nada")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(check_only=args.check)))


if __name__ == "__main__":
    main()

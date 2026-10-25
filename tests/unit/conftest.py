"""Unit-suite isolation: no PostgreSQL, ever.

Every code path that could open a database connection (SQLAlchemy's psycopg
dialect, psycopg_pool behind the checkpointer) goes through
``psycopg.(Async)Connection.connect``. Blocking those here turns an accidental
dependency on PostgreSQL into an immediate, explicit failure.
"""

import os
import subprocess
from typing import Any

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


# The only program the application may spawn is ``git`` (the local adapter,
# which builds its own argv). Anything else — ruff, pytest, python, a shell —
# would mean untrusted code running on the host, so it fails loudly here.
_ALLOWED_LOCAL_PROGRAMS = frozenset({"git"})


@pytest.fixture(autouse=True)
def _block_untrusted_subprocesses(monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.Popen.__init__

    def guarded(self: subprocess.Popen[Any], args: Any, *rest: Any, **kwargs: Any) -> None:
        argv = list(args) if isinstance(args, list | tuple) else [args]
        program = str(argv[0]) if argv else ""
        if os.path.basename(program) not in _ALLOWED_LOCAL_PROGRAMS and "slow-git" not in program:
            raise RuntimeError(
                f"Ejecución local bloqueada en la suite unitaria: {program!r}; "
                "el código no confiable solo corre en el sandbox remoto"
            )
        original(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded)

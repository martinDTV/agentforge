"""Opaque keyset cursors.

A cursor is a position, not a capability: the authorization scope is applied
again on every page, so a forged or replayed cursor can only move the window
inside what the caller may already see. It is bound to the query's filters so
a cursor from one listing cannot be fed to another, and it is validated
strictly (version, kind, field types); anything else is a 400.
"""

import base64
import binascii
import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from app.exceptions import DomainError
from app.goals.ports import GoalCursor
from app.orchestration.ports import RunCursor

CURSOR_VERSION = 1


class InvalidCursorError(DomainError):
    code = "invalid_cursor"

    def __init__(self) -> None:
        super().__init__("El cursor no es válido para esta consulta.")


def fingerprint(**filters: Any) -> str:
    canonical = json.dumps(filters, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _encode(payload: dict[str, Any]) -> str:
    raw = json.dumps({"v": CURSOR_VERSION, **payload}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(token: str, *, kind: str, filters_fingerprint: str) -> dict[str, Any]:
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InvalidCursorError() from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != CURSOR_VERSION
        or payload.get("k") != kind
        or payload.get("f") != filters_fingerprint
    ):
        raise InvalidCursorError()
    return payload


def encode_run_cursor(created_at: datetime, run_id: UUID, *, filters_fingerprint: str) -> str:
    return _encode(
        {"k": "runs", "f": filters_fingerprint, "t": created_at.isoformat(), "id": str(run_id)}
    )


def decode_run_cursor(token: str, *, filters_fingerprint: str) -> RunCursor:
    payload = _decode(token, kind="runs", filters_fingerprint=filters_fingerprint)
    try:
        created_at = datetime.fromisoformat(str(payload["t"]))
        run_id = UUID(str(payload["id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidCursorError() from exc
    if created_at.tzinfo is None:
        raise InvalidCursorError()
    return RunCursor(created_at=created_at, id=run_id)


def encode_event_cursor(event_id: int, *, filters_fingerprint: str) -> str:
    return _encode({"k": "events", "f": filters_fingerprint, "id": event_id})


def decode_event_cursor(token: str, *, filters_fingerprint: str) -> int:
    payload = _decode(token, kind="events", filters_fingerprint=filters_fingerprint)
    event_id = payload.get("id")
    if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 0:
        raise InvalidCursorError()
    return event_id


def encode_goal_cursor(created_at: datetime, goal_id: UUID, *, filters_fingerprint: str) -> str:
    return _encode(
        {"k": "goals", "f": filters_fingerprint, "t": created_at.isoformat(), "id": str(goal_id)}
    )


def decode_goal_cursor(token: str, *, filters_fingerprint: str) -> GoalCursor:
    payload = _decode(token, kind="goals", filters_fingerprint=filters_fingerprint)
    try:
        created_at = datetime.fromisoformat(str(payload["t"]))
        goal_id = UUID(str(payload["id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidCursorError() from exc
    if created_at.tzinfo is None:
        raise InvalidCursorError()
    return GoalCursor(created_at=created_at, id=goal_id)

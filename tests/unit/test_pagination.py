"""Opaque keyset cursors: round trip, binding to filters, strict validation."""

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest

from app.api.pagination import (
    InvalidCursorError,
    decode_event_cursor,
    decode_run_cursor,
    encode_event_cursor,
    encode_run_cursor,
    fingerprint,
)


def test_run_cursor_round_trips_and_is_opaque():
    fp = fingerprint(subject="alice", status=None, project_id="alpha", owner=None)
    created = datetime(2026, 10, 24, 12, 0, 0, 123456, tzinfo=UTC)
    run_id = uuid.uuid4()
    token = encode_run_cursor(created, run_id, filters_fingerprint=fp)
    assert "=" not in token and " " not in token
    payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    assert "alice" not in json.dumps(payload)  # the fingerprint, never the filters themselves
    cursor = decode_run_cursor(token, filters_fingerprint=fp)
    assert (cursor.created_at, cursor.id) == (created, run_id)


def test_cursor_is_bound_to_the_query_filters():
    token = encode_run_cursor(
        datetime.now(UTC), uuid.uuid4(), filters_fingerprint=fingerprint(subject="a", status=None)
    )
    with pytest.raises(InvalidCursorError):
        decode_run_cursor(token, filters_fingerprint=fingerprint(subject="a", status="failed"))
    with pytest.raises(InvalidCursorError):
        decode_run_cursor(token, filters_fingerprint=fingerprint(subject="b", status=None))


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-base64!!",
        base64.urlsafe_b64encode(b"[]").decode(),
        base64.urlsafe_b64encode(b'{"v":1,"k":"runs","f":"x"}').decode(),
        base64.urlsafe_b64encode(
            b'{"v":2,"k":"runs","f":"x","t":"2026-01-01T00:00:00+00:00","id":"nope"}'
        ).decode(),
    ],
)
def test_garbage_cursors_are_rejected(token: str):
    with pytest.raises(InvalidCursorError):
        decode_run_cursor(token, filters_fingerprint="x")


def test_run_cursor_requires_timezone_aware_timestamp_and_valid_uuid():
    fp = "x"
    naive = base64.urlsafe_b64encode(
        json.dumps(
            {"v": 1, "k": "runs", "f": fp, "t": "2026-01-01T00:00:00", "id": str(uuid.uuid4())}
        ).encode()
    ).decode()
    with pytest.raises(InvalidCursorError):
        decode_run_cursor(naive, filters_fingerprint=fp)


def test_event_cursor_round_trips_and_rejects_other_kinds_and_bad_ids():
    fp = fingerprint(subject="alice", run_id="r1")
    token = encode_event_cursor(42, filters_fingerprint=fp)
    assert decode_event_cursor(token, filters_fingerprint=fp) == 42
    with pytest.raises(InvalidCursorError):
        decode_run_cursor(token, filters_fingerprint=fp)  # wrong kind
    for bad in (-1, True, "42"):
        forged = base64.urlsafe_b64encode(
            json.dumps({"v": 1, "k": "events", "f": fp, "id": bad}).encode()
        ).decode()
        with pytest.raises(InvalidCursorError):
            decode_event_cursor(forged, filters_fingerprint=fp)

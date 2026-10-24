"""Checkpoint serializer: strict allowlist, no pickle, optional AES at rest."""

import pickle
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command, Interrupt

from app.infrastructure.checkpoints import build_serializer

KEY = b"0123456789abcdef0123456789abcdef"  # 32 bytes, test only

PLAN = {
    "summary": "Plan de prueba",
    "tasks": [
        {
            "id": "t1",
            "title": "Analizar",
            "description": "Definir contratos",
            "dependencies": [],
            "acceptance_criteria": ["Contrato revisado"],
        }
    ],
    "risks": ["Ninguno relevante"],
}
RUN_STATES: list[dict[str, Any]] = [
    {"task": "Diseña un módulo"},
    {"task": "t", "plan": PLAN, "revision": 0, "llm_calls": 1},
    {
        "task": "t",
        "plan": PLAN,
        "architecture": {"summary": "s", "decisions": [], "issues": ["x"], "approved": False},
        "feedback": ["x"],
        "architect_approved": False,
        "revision": 1,
        "llm_calls": 4,
    },
    {"task": "t", "llm_calls": 1, "error": {"code": "llm_timeout", "message": "sin respuesta"}},
    {"task": "t", "plan": PLAN, "approved": True},
    {
        "kind": "approve_plan",
        "plan": PLAN,
        "architecture": None,
        "revision": 0,
    },  # interrupt payload
    {"approved": False},  # resume payload
]


def roundtrip(serde: SerializerProtocol, obj: Any) -> tuple[str, Any]:
    type_tag, data = serde.dumps_typed(obj)
    return type_tag, serde.loads_typed((type_tag, data))


@pytest.mark.parametrize("state", RUN_STATES)
def test_every_run_state_shape_roundtrips_as_msgpack(state: dict[str, Any]):
    type_tag, back = roundtrip(build_serializer(), state)
    assert type_tag == "msgpack"
    assert back == state


def test_langgraph_control_objects_are_in_the_allowlist():
    serde = build_serializer()
    _, interrupt = roundtrip(serde, Interrupt(value={"kind": "approve_plan"}, id="i1"))
    _, command = roundtrip(serde, Command(resume={"approved": True}))
    assert isinstance(interrupt, Interrupt) and interrupt.value == {"kind": "approve_plan"}
    assert isinstance(command, Command) and command.resume == {"approved": True}


def test_pickle_fallback_is_disabled_and_unknown_types_fail_at_dump_time():
    serde = build_serializer()
    assert isinstance(serde, JsonPlusSerializer) and serde.pickle_fallback is False

    class NotSerializable:
        pass

    with pytest.raises((TypeError, ValueError)):
        serde.dumps_typed(NotSerializable())


def test_pickled_payloads_are_refused_on_load():
    serde = build_serializer()
    with pytest.raises(Exception):
        serde.loads_typed(("pickle", pickle.dumps({"a": 1})))


@dataclass
class NotAllowlisted:
    path: str


def test_arbitrary_python_types_are_blocked_on_load_even_if_valid_msgpack(
    caplog: pytest.LogCaptureFixture,
):
    """Rows written by a permissive (or hostile) producer never instantiate our classes.

    LangGraph's strict mode does not raise: it refuses to rebuild the object,
    logs the block and hands back the raw payload as plain data. No
    constructor runs.
    """
    permissive = JsonPlusSerializer(allowed_msgpack_modules=True)
    type_tag, data = permissive.dumps_typed({"p": NotAllowlisted(path="/etc/passwd")})
    assert type_tag == "msgpack"
    assert permissive.loads_typed((type_tag, data)) == {"p": NotAllowlisted(path="/etc/passwd")}

    strict = build_serializer()
    with caplog.at_level("WARNING", logger="langgraph.checkpoint.serde.jsonplus"):
        loaded = strict.loads_typed((type_tag, data))
    assert not isinstance(loaded["p"], NotAllowlisted)
    assert loaded["p"] == {"path": "/etc/passwd"}  # raw payload, no constructor ran
    assert "Blocked deserialization" in caplog.text and "NotAllowlisted" in caplog.text
    # Allowlisted stdlib types still work.
    assert roundtrip(strict, {"d": Decimal("1.5")})[1] == {"d": Decimal("1.5")}


def test_encryption_roundtrips_and_hides_content():
    serde = build_serializer(KEY)
    type_tag, data = serde.dumps_typed({"plan": PLAN})
    assert b"Plan de prueba" not in data
    assert serde.loads_typed((type_tag, data)) == {"plan": PLAN}
    assert type_tag != "msgpack"  # the tag advertises the cipher, not the payload format


def test_wrong_key_cannot_decrypt():
    type_tag, data = build_serializer(KEY).dumps_typed({"plan": PLAN})
    other = build_serializer(b"f" * 32)
    with pytest.raises(Exception):
        other.loads_typed((type_tag, data))
    with pytest.raises(Exception):
        build_serializer().loads_typed((type_tag, data))

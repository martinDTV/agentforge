"""Checkpoint serializer policy (ADR 0003, sección de seguridad).

- msgpack only, with LangGraph's built-in allowlist of safe types
  (``allowed_msgpack_modules=None``). Our ``RunState`` is JSON-native; the
  allowlist is still needed for LangGraph's own ``Interrupt``/``Command``
  objects that live in checkpoint writes.
- No pickle fallback: an unknown type fails at *dump* time in the process
  that produced it, never at load time from a database row.
- Optional AES-EAX at rest via LangGraph's ``EncryptedSerializer``; the key
  comes from Settings as a ``SecretStr`` and is never logged.
"""

from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


def build_serializer(encryption_key: bytes | None = None) -> SerializerProtocol:
    strict: SerializerProtocol = JsonPlusSerializer(
        pickle_fallback=False,
        allowed_msgpack_modules=None,
    )
    if encryption_key is None:
        return strict
    from langgraph.checkpoint.serde.encrypted import EncryptedSerializer

    return EncryptedSerializer.from_pycryptodome_aes(serde=strict, key=encryption_key)

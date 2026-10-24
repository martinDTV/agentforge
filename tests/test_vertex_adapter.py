"""VertexClaudeAdapter error mapping and repair loop, against a stub client.

No network: the stub replaces ``AnthropicVertex`` and returns canned
responses or raises SDK exceptions in order.
"""

from collections import deque
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from app.infrastructure.llm.errors import (
    LLMMalformedOutputError,
    LLMRequestRejectedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.infrastructure.llm.vertex_claude import VertexClaudeAdapter
from app.settings import Settings

REQUEST = httpx.Request("POST", "https://vertex.test/")


class Answer(BaseModel):
    value: int


def message(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)]
    )


def status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    return cls("error", response=httpx.Response(code, request=REQUEST), body=None)


class StubClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outcomes = deque(outcomes)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_adapter(
    *outcomes: Any, repair_attempts: int = 1
) -> tuple[VertexClaudeAdapter, StubClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        anthropic_vertex_project_id="test-project",
        claude_model="claude-test",
        llm_max_repair_attempts=repair_attempts,
    )
    client = StubClient(list(outcomes))
    return VertexClaudeAdapter(settings, client=client), client  # type: ignore[arg-type]


def ask(adapter: VertexClaudeAdapter) -> Answer:
    return adapter.generate_structured("system", "user", Answer)


def test_valid_json_is_parsed_and_schema_is_in_system_prompt():
    adapter, client = make_adapter(message('{"value": 1}'))
    assert ask(adapter) == Answer(value=1)
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "claude-test"
    assert '"value"' in client.calls[0]["system"]
    assert "JSON" in client.calls[0]["system"]


def test_code_fences_are_tolerated():
    adapter, _ = make_adapter(message('```json\n{"value": 2}\n```'))
    assert ask(adapter) == Answer(value=2)


def test_invalid_output_is_repaired_with_the_validation_error():
    adapter, client = make_adapter(message("esto no es json"), message('{"value": 3}'))

    assert ask(adapter) == Answer(value=3)

    assert len(client.calls) == 2
    repair_messages = client.calls[1]["messages"]
    assert [m["role"] for m in repair_messages] == ["user", "assistant", "user"]
    assert repair_messages[1]["content"] == "esto no es json"
    assert "no es válida" in repair_messages[2]["content"]
    assert "Answer" in repair_messages[2]["content"] or "json" in repair_messages[2]["content"]


def test_repair_budget_exhausted_raises_malformed_output():
    adapter, client = make_adapter(message("x"), message("y"))
    with pytest.raises(LLMMalformedOutputError, match="Answer"):
        ask(adapter)
    assert len(client.calls) == 2


def test_no_repair_when_disabled():
    adapter, client = make_adapter(message("x"), repair_attempts=0)
    with pytest.raises(LLMMalformedOutputError):
        ask(adapter)
    assert len(client.calls) == 1


def test_max_tokens_truncation_is_treated_as_malformed_and_repaired():
    adapter, client = make_adapter(
        message('{"val', stop_reason="max_tokens"), message('{"value": 4}')
    )
    assert ask(adapter) == Answer(value=4)
    repair_messages = client.calls[1]["messages"]
    assert "max_tokens" in repair_messages[2]["content"]


def test_timeout_maps_to_llm_timeout_without_repair():
    adapter, client = make_adapter(anthropic.APITimeoutError(request=REQUEST))
    with pytest.raises(LLMTimeoutError):
        ask(adapter)
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "exc",
    [
        anthropic.APIConnectionError(request=REQUEST),
        status_error(anthropic.RateLimitError, 429),
        status_error(anthropic.OverloadedError, 529),
        status_error(anthropic.InternalServerError, 500),
    ],
    ids=["connection", "rate_limit", "overloaded", "server_error"],
)
def test_transient_failures_map_to_unavailable(exc: Exception):
    adapter, _ = make_adapter(exc)
    with pytest.raises(LLMUnavailableError):
        ask(adapter)


@pytest.mark.parametrize(
    ("cls", "code"),
    [
        (anthropic.BadRequestError, 400),
        (anthropic.AuthenticationError, 401),
        (anthropic.PermissionDeniedError, 403),
        (anthropic.NotFoundError, 404),
    ],
    ids=["bad_request", "auth", "permission", "not_found"],
)
def test_client_errors_map_to_request_rejected(cls: type[anthropic.APIStatusError], code: int):
    adapter, _ = make_adapter(status_error(cls, code))
    with pytest.raises(LLMRequestRejectedError, match=str(code)):
        ask(adapter)


def test_error_chain_preserves_sdk_exception():
    adapter, _ = make_adapter(anthropic.APITimeoutError(request=REQUEST))
    with pytest.raises(LLMTimeoutError) as info:
        ask(adapter)
    assert isinstance(info.value.__cause__, anthropic.APITimeoutError)

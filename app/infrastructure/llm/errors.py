"""Provider-agnostic failures raised by any LLMPort implementation.

Each error carries a stable ``code`` so the orchestrator can persist the
failure in the run state and expose it through the API without leaking
provider payloads or stack traces.
"""


class LLMError(Exception):
    code: str = "llm_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LLMTimeoutError(LLMError):
    code = "llm_timeout"


class LLMUnavailableError(LLMError):
    """Rate limits, overload, connection failures and provider 5xx responses."""

    code = "llm_unavailable"


class LLMRequestRejectedError(LLMError):
    """The provider rejected the request (4xx): credentials, model, policy."""

    code = "llm_request_rejected"


class LLMMalformedOutputError(LLMError):
    """The model answered, but the output does not satisfy the contract."""

    code = "llm_malformed_output"

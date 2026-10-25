"""Security headers for a JSON API.

Conservative on purpose: nothing here breaks clients or the interactive docs.
TLS-related policy (HSTS) is only emitted in staging/production, where the
API is expected to sit behind TLS. Rate limiting is not done here: it belongs
to the gateway in front of the API (see README / ADR 0005).
"""

from collections.abc import Awaitable, Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

BASE_HEADERS: dict[str, str] = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}
API_HEADERS: dict[str, str] = {"cache-control": "no-store"}
HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = False, api_prefix: str = "/api/") -> None:
        self._app = app
        self._hsts = hsts
        self._api_prefix = api_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        send_with_headers = self._wrap(send, api_response=path.startswith(self._api_prefix))
        await self._app(scope, receive, send_with_headers)

    def _wrap(self, send: Send, *, api_response: bool) -> Callable[[Message], Awaitable[None]]:
        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                extra = dict(BASE_HEADERS)
                if api_response:
                    extra.update(API_HEADERS)
                if self._hsts:
                    extra["strict-transport-security"] = HSTS
                for name, value in extra.items():
                    if name.encode() not in present:
                        headers.append((name.encode(), value.encode()))
                message["headers"] = headers
            await send(message)

        return _send

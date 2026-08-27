from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from starlette.responses import JSONResponse


ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[dict]], Callable[..., Awaitable[None]]], Awaitable[None]]


class ChatBodyLimitMiddleware:
    """Reject oversized chat bodies before JSON/Pydantic allocation."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max(1, int(max_bytes))

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/api/chat":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await self._reject(scope, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if replayed:
                return {"type": "http.request", "body": b"", "more_body": False}
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope, receive, send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Chat request body is too large"},
        )
        await response(scope, receive, send)

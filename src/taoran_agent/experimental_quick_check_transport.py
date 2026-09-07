"""Experimental Quick Check response privacy, without buffering SSE."""
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class ExperimentalQuickCheckNoStore:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        protected = scope["type"] == "http" and (
            path == "/quick-check/interactive"
            or path.startswith((
                "/api/v1/quick-check/", "/api/v1/experimental/quick-check-interactive/",
            ))
        )

        async def send_private(message: Message) -> None:
            if protected and message["type"] == "http.response.start":
                message = {**message, "headers": [
                    (key, value) for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]}
            await send(message)

        await self.app(scope, receive, send_private)

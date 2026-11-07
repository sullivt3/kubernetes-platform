"""Pure-ASGI middleware (safe for streaming responses, unlike BaseHTTPMiddleware)."""

from __future__ import annotations

import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import request_id_var
from app.modules.auditing.service import client_ip_var

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


class RequestContextMiddleware:
    """Assigns a request ID, records the client IP for auditing and adds security headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        client = scope.get("client")
        rid_token = request_id_var.set(request_id)
        ip_token = client_ip_var.set(client[0] if client else None)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-request-id", request_id.encode()))
                existing = {name.lower() for name, _ in response_headers}
                response_headers.extend(h for h in SECURITY_HEADERS if h[0] not in existing)
                if b"cache-control" not in existing:
                    response_headers.append((b"cache-control", b"no-store"))
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(rid_token)
            client_ip_var.reset(ip_token)

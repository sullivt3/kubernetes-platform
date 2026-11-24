"""Admin-only interactive sessions, origin-checked single-use WebSocket attachment."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid

import anyio
from fastapi import APIRouter, Depends, Response, WebSocket
from starlette.concurrency import run_in_threadpool

from app.api.auth import get_principal
from app.api.deps import get_kubernetes_service
from app.core.config import get_settings
from app.core.errors import InvalidStateError, PlatformError
from app.core.principal import Principal
from app.core.secrets import get_secret_store
from app.db.session import get_session_factory
from app.modules.command_center import sessions
from app.modules.kubernetes.service import KubernetesService
from app.modules.organizations.scope import scope_session
from app.modules.workloads import terminal

router = APIRouter(prefix="/clusters/{cluster_id}/command-sessions", tags=["interactive command center"])


@router.get("")
def inventory(
    cluster_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    response.headers["Cache-Control"] = "no-store"
    return sessions.list_sessions(service, principal, cluster_id)


@router.post("", status_code=202)
def start(
    cluster_id: uuid.UUID,
    body: sessions.Start,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    response.headers["Cache-Control"] = "no-store"
    return sessions.start(service, principal, cluster_id, body)


@router.post("/{session_id}/close", status_code=202)
def close(
    cluster_id: uuid.UUID,
    session_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    return sessions.close(service, principal, cluster_id, session_id)


class SessionSocket:
    """Bounded transport only; no command text/output is persisted or audited."""

    def __init__(self, websocket):
        self.websocket = websocket
        self.last_input = time.monotonic()
        self.output_bytes = 0
        self.input_bytes = 0
        self.outcome = "closed"

    async def receive_json(self):
        text = await self.websocket.receive_text()
        if len(text.encode()) > 16384:
            self.outcome = "input_limit"
            raise InvalidStateError("Input limit exceeded")
        message = json.loads(text)
        if not isinstance(message, dict):
            raise InvalidStateError("Invalid terminal frame")
        if message.get("type") == "input":
            data = message.get("data")
            if not isinstance(data, str) or len(data.encode()) > 8192:
                raise InvalidStateError("Invalid terminal input")
            self.input_bytes += len(data.encode())
            if self.input_bytes > 1024 * 1024:
                self.outcome = "input_limit"
                raise InvalidStateError("Input limit exceeded")
            self.last_input = time.monotonic()
        elif message.get("type") == "resize":
            if any(type(message.get(key)) is not int or not 1 <= message[key] <= 500 for key in ("cols", "rows")):
                raise InvalidStateError("Invalid terminal dimensions")
        else:
            raise InvalidStateError("Invalid terminal frame")
        return message

    async def send_json(self, message):
        if message.get("type") == "output":
            self.output_bytes += len(message.get("data", "").encode())
            if self.output_bytes > 2 * 1024 * 1024:
                self.outcome = "output_limit"
                raise InvalidStateError("Output limit exceeded")
        await self.websocket.send_json(message)


@router.websocket("/{session_id}/attach")
async def attach(websocket: WebSocket, cluster_id: uuid.UUID, session_id: uuid.UUID):
    # Unlike pod exec, interactive sessions require an explicit browser origin.
    settings = get_settings()
    if websocket.headers.get("origin") not in {settings.public_url.rstrip("/"), *settings.cors_origins}:
        await websocket.close(code=4403, reason="origin not allowed")
        return
    principal = await run_in_threadpool(get_principal, websocket)

    def claim():
        with get_session_factory()() as session:
            scope_session(session, principal)
            service = KubernetesService(session, get_secret_store(), None)  # type: ignore[arg-type]  # Attach performs no operation submission.
            return sessions.attach(service, principal, cluster_id, session_id)

    try:
        name, target = await run_in_threadpool(claim)
    except PlatformError:
        await websocket.close(code=4403, reason="session not available")
        return
    outcome = "closed"
    bridge = None
    adapter = SessionSocket(websocket)
    try:
        await websocket.accept()
        bridge = asyncio.create_task(
            terminal.bridge(adapter, target, name, "runtime", "tools", max_seconds=sessions.LIFETIME)
        )
        while not bridge.done():
            done, _ = await asyncio.wait({bridge}, timeout=5)
            if done:
                break
            current = await run_in_threadpool(websocket.app.state.authenticator.authenticate, websocket)
            await run_in_threadpool(sessions.heartbeat, current, session_id)
            if time.monotonic() - adapter.last_input > 120:
                outcome = "idle_timeout"
                break
        if bridge.done():
            outcome = await bridge
    except Exception:
        outcome = "access_or_transport_ended"
    finally:
        # Shield durable closing from browser/server cancellation.
        with anyio.CancelScope(shield=True):
            if bridge and not bridge.done():
                bridge.cancel()
            if bridge:
                await asyncio.gather(bridge, return_exceptions=True)
            outcome = adapter.outcome if adapter.outcome != "closed" else outcome
            await run_in_threadpool(sessions.mark_closing, session_id, outcome)
            await run_in_threadpool(sessions.enqueue_cleanup, session_id)
            with contextlib.suppress(Exception):
                await websocket.close(code=1000, reason="interactive session ended")

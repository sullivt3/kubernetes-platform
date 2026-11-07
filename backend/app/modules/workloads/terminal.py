"""Container terminals: bridge a browser WebSocket to the Kubernetes exec API.

Kubernetes exec speaks the ``v5.channel.k8s.io`` / ``v4.channel.k8s.io`` WebSocket
subprotocols: every binary frame starts with a channel byte —
0 stdin, 1 stdout, 2 stderr, 3 error/status (JSON), 4 terminal resize (JSON).

The browser side uses small JSON messages:
  -> {"type": "input", "data": "..."} | {"type": "resize", "cols": n, "rows": n}
  <- {"type": "output", "data": "..."} | {"type": "exit", "status": "...", "message": "..."} | {"type": "error", ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from app.modules.kubernetes.client import ConnectionTarget, _ssl_context

logger = logging.getLogger(__name__)

STDIN, STDOUT, STDERR, ERROR, RESIZE = 0, 1, 2, 3, 4
SUBPROTOCOLS = ["v5.channel.k8s.io", "v4.channel.k8s.io"]
# Prefer bash, fall back to sh (minimal images often have only sh).
SHELL = ["/bin/sh", "-c", "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"]
MAX_SESSION_SECONDS = 3600


class BrowserSocket(Protocol):
    async def receive_json(self) -> Any: ...

    async def send_json(self, data: Any) -> None: ...


def exec_url(target: ConnectionTarget, namespace: str, pod: str, container: str | None, command: list[str]) -> str:
    base = target.server.replace("https://", "wss://", 1)
    query: list[tuple[str, str]] = [("stdin", "true"), ("stdout", "true"), ("stderr", "true"), ("tty", "true")]
    if container:
        query.append(("container", container))
    query += [("command", part) for part in command]
    return f"{base}/api/v1/namespaces/{namespace}/pods/{pod}/exec?{urlencode(query)}"


def encode_input(data: str) -> bytes:
    return bytes([STDIN]) + data.encode()


def encode_resize(cols: int, rows: int) -> bytes:
    return bytes([RESIZE]) + json.dumps({"Width": max(1, cols), "Height": max(1, rows)}).encode()


def decode_frame(frame: bytes | str) -> dict[str, Any] | None:
    """Translate one Kubernetes frame into a browser message (None to ignore)."""
    if isinstance(frame, str):
        frame = frame.encode()
    if not frame:
        return None
    channel, payload = frame[0], frame[1:]
    if channel in (STDOUT, STDERR):
        return {"type": "output", "data": payload.decode(errors="replace")} if payload else None
    if channel == ERROR:
        try:
            status = json.loads(payload) if payload else {}
        except ValueError:
            status = {"message": payload.decode(errors="replace")}
        return {"type": "exit", "status": status.get("status", "Unknown"), "message": status.get("message", "")}
    return None


async def bridge(
    browser: BrowserSocket, target: ConnectionTarget, namespace: str, pod: str, container: str | None
) -> str:
    """Run one terminal session. Returns a short outcome description for the audit log."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    headers = {"Authorization": f"Bearer {target.kubeconfig.token}"} if target.kubeconfig.token else {}
    host = urlsplit(target.server).hostname
    try:
        k8s = await connect(
            exec_url(target, namespace, pod, container, SHELL),
            ssl=_ssl_context(target),
            server_hostname=target.verify_name or host,
            additional_headers=headers,
            subprotocols=SUBPROTOCOLS,  # type: ignore[arg-type]
            open_timeout=15,
            max_size=4 * 1024 * 1024,
            ping_interval=30,
        )
    except InvalidStatus as exc:
        code = exc.response.status_code
        message = {401: "unauthorized", 403: "forbidden (RBAC)", 404: "pod or container not found"}.get(
            code, f"HTTP {code}"
        )
        await browser.send_json({"type": "error", "message": f"Kubernetes refused the terminal: {message}"})
        return f"refused ({code})"
    except (OSError, TimeoutError) as exc:
        await browser.send_json({"type": "error", "message": f"Cannot reach the Kubernetes API: {exc}"})
        return "unreachable"

    outcome = "closed"

    async def from_cluster() -> None:
        nonlocal outcome
        try:
            async for frame in k8s:
                message = decode_frame(frame)
                if message:
                    await browser.send_json(message)
                    if message["type"] == "exit":
                        outcome = f"exit ({message['status']})"
        except ConnectionClosed:
            pass

    async def from_browser() -> None:
        while True:
            message = await browser.receive_json()
            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "input":
                await k8s.send(encode_input(str(message.get("data", ""))[:65536]))
            elif kind == "resize":
                await k8s.send(encode_resize(int(message.get("cols", 80)), int(message.get("rows", 24))))

    tasks = [asyncio.create_task(from_cluster()), asyncio.create_task(from_browser())]
    try:
        done, _pending = await asyncio.wait(tasks, timeout=MAX_SESSION_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            outcome = "session time limit reached"
            await browser.send_json({"type": "exit", "status": "Timeout", "message": "Session time limit reached"})
        for task in tasks:
            task.cancel()
    except Exception:  # browser went away
        for task in tasks:
            task.cancel()
    finally:
        await k8s.close()
    return outcome

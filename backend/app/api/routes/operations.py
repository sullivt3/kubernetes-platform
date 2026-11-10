"""Operations API: listing, detail, logs, cancel/retry and live streams (SSE)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.auth import get_principal
from app.api.deps import get_operation_service
from app.api.schemas import ApiModel, Page
from app.core.config import get_settings
from app.core.errors import PlatformError
from app.core.principal import Principal
from app.db.session import get_session_factory
from app.modules.organizations.policy import require
from app.modules.organizations.scope import scope_session
from app.orchestration.events import operation_channel, organization_operations_channel
from app.orchestration.models import OperationStatus
from app.orchestration.runtime import get_dispatcher, get_publisher, load_workflows
from app.orchestration.service import OperationService
from app.orchestration.workflow import workflow_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/operations", tags=["operations"])

LOG_BATCH = 500
KEEPALIVE_SECONDS = 15.0
POLL_FALLBACK_SECONDS = 2.0
# "no-transform" stops intermediaries (including the Next.js compression layer) from
# gzip-buffering the stream; X-Accel-Buffering disables nginx proxy buffering.
SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


class StepOut(ApiModel):
    key: str
    name: str
    position: int
    status: str
    attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None
    output: dict[str, Any]


class OperationSummary(ApiModel):
    id: uuid.UUID
    type: str
    title: str
    status: OperationStatus
    target_type: str | None
    target_id: str | None
    target_name: str | None
    progress: float
    current_step: str | None
    attempt: int
    recovery_count: int
    requested_by_subject: str
    error: dict[str, Any] | None
    created_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class OperationDetail(OperationSummary):
    params: dict[str, Any]
    result: dict[str, Any] | None
    worker_id: str | None
    heartbeat_at: datetime | None
    cancel_requested_at: datetime | None
    parent_operation_id: uuid.UUID | None
    steps: list[StepOut]


class LogOut(ApiModel):
    id: int
    ts: datetime
    level: str
    step_key: str | None
    message: str


@router.get("", response_model=Page[OperationSummary])
def list_operations(
    status: list[OperationStatus] | None = Query(default=None),
    active: bool = False,
    type: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    service: OperationService = Depends(get_operation_service),
) -> Page[OperationSummary]:
    rows, total = service.list_operations(
        principal,
        statuses=status,
        active_only=active,
        type=type,
        target_type=target_type,
        target_id=target_id,
        limit=limit,
        offset=offset,
    )
    return Page(items=[OperationSummary.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{operation_id}", response_model=OperationDetail)
def get_operation(
    operation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: OperationService = Depends(get_operation_service),
) -> OperationDetail:
    return OperationDetail.model_validate(service.get(principal, operation_id))


@router.get("/{operation_id}/logs", response_model=list[LogOut])
def get_logs(
    operation_id: uuid.UUID,
    after: int = Query(default=0, ge=0, description="Return log entries with id greater than this"),
    limit: int = Query(default=LOG_BATCH, ge=1, le=2000),
    principal: Principal = Depends(get_principal),
    service: OperationService = Depends(get_operation_service),
) -> list[LogOut]:
    return [LogOut.model_validate(r) for r in service.logs(principal, operation_id, after_id=after, limit=limit)]


@router.post("/{operation_id}/cancel", response_model=OperationDetail)
def cancel_operation(
    operation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: OperationService = Depends(get_operation_service),
) -> OperationDetail:
    return OperationDetail.model_validate(service.cancel(principal, operation_id))


@router.post("/{operation_id}/retry", response_model=OperationDetail, status_code=202)
def retry_operation(
    operation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: OperationService = Depends(get_operation_service),
) -> OperationDetail:
    return OperationDetail.model_validate(service.retry(principal, operation_id))


# ---------------------------------------------------------------------- streaming


def _sse(event: str, data: str, event_id: int | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.extend(f"data: {line}" for line in data.splitlines() or [""])
    return "\n".join(lines) + "\n\n"


async def _subscribe(channel: str) -> tuple[Any, Any] | None:
    """Subscribe to a doorbell channel; None if Redis is unavailable (callers poll instead)."""
    client = aioredis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1)
    try:
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)
        return client, pubsub
    except Exception as exc:
        logger.info("Live updates unavailable (%s); falling back to polling", exc)
        await client.aclose()
        return None


async def _wait_for_doorbell(subscription: tuple[Any, Any] | None, timeout: float) -> bool:
    """Wait for a change notification. Returns True if one arrived (bursts are coalesced)."""
    if subscription is None:
        await asyncio.sleep(min(timeout, POLL_FALLBACK_SECONDS))
        return True  # poll unconditionally
    _, pubsub = subscription
    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
    if message is None:
        return False
    while await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.05):
        pass
    return True


async def _close(subscription: tuple[Any, Any] | None) -> None:
    if subscription is None:
        return
    client, pubsub = subscription
    try:
        await pubsub.aclose()
        await client.aclose()
    except Exception:  # pragma: no cover
        pass


def _load_state(principal: Principal, operation_id: uuid.UUID, cursor: int) -> tuple[str, list[LogOut], bool]:
    load_workflows()
    with get_session_factory()() as session:
        scope_session(session, principal)
        service = OperationService(
            session, registry=workflow_registry, dispatcher=get_dispatcher(), publisher=get_publisher()
        )
        op = service.get(principal, operation_id)
        snapshot = OperationDetail.model_validate(op).model_dump_json()
        terminal = op.status.is_terminal
        logs = [
            LogOut.model_validate(r) for r in service.logs(principal, operation_id, after_id=cursor, limit=LOG_BATCH)
        ]
    return snapshot, logs, terminal


@router.get(
    "/{operation_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
    summary="Live operation updates (Server-Sent Events)",
    description=(
        "Emits `log` events (id = log id, resumable via Last-Event-ID or ?after=), `operation` snapshots "
        "whenever state changes, and a final `end` event once the operation is terminal."
    ),
)
async def stream_operation(
    operation_id: uuid.UUID,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None),
    principal: Principal = Depends(get_principal),
) -> StreamingResponse:
    cursor = max(after, int(last_event_id) if last_event_id and last_event_id.isdigit() else 0)
    # Validate access before opening the stream so errors return proper HTTP codes.
    await run_in_threadpool(_load_state, principal, operation_id, cursor)

    async def events() -> AsyncIterator[str]:
        nonlocal cursor
        subscription = await _subscribe(operation_channel(operation_id))
        last_snapshot: str | None = None
        try:
            while True:
                try:
                    await run_in_threadpool(_refresh_stream_principal, request, principal)
                except PlatformError:
                    yield _sse("end", json.dumps({"reason": "access_changed"}))
                    return
                snapshot, logs, terminal = await run_in_threadpool(_load_state, principal, operation_id, cursor)
                for entry in logs:
                    cursor = entry.id
                    yield _sse("log", entry.model_dump_json(), entry.id)
                if snapshot != last_snapshot:
                    last_snapshot = snapshot
                    yield _sse("operation", snapshot)
                if len(logs) == LOG_BATCH:
                    continue  # drain backlog before waiting
                if terminal:
                    yield _sse("end", json.dumps({"operation_id": str(operation_id)}))
                    return
                if await request.is_disconnected():
                    return
                if not await _wait_for_doorbell(subscription, KEEPALIVE_SECONDS):
                    yield ": keepalive\n\n"
        finally:
            await _close(subscription)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get(
    "/stream/changes",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
    summary="Organization-wide operation change notifications (Server-Sent Events)",
    description=(
        "Emits lightweight `change` events ({operation_id, kind, status}) for the caller's organization. "
        "Clients refetch what they display. Emits `unavailable` and closes if Redis is down; clients should "
        "then poll."
    ),
)
async def stream_changes(request: Request, principal: Principal = Depends(get_principal)) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        subscription = await _subscribe(organization_operations_channel(principal.organization_id))
        if subscription is None:
            yield _sse("unavailable", json.dumps({"reason": "redis_unavailable"}))
            return
        _, pubsub = subscription
        try:
            yield _sse("ready", "{}")
            while not await request.is_disconnected():
                try:
                    await run_in_threadpool(_refresh_stream_principal, request, principal)
                except PlatformError:
                    yield _sse("end", json.dumps({"reason": "access_changed"}))
                    return
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=KEEPALIVE_SECONDS)
                if message is None:
                    yield ": keepalive\n\n"
                    continue
                data = message["data"].decode() if isinstance(message["data"], bytes) else str(message["data"])
                if principal.project_id is not None:
                    try:
                        operation_id = uuid.UUID(json.loads(data)["operation_id"])
                        await run_in_threadpool(_load_state, principal, operation_id, 0)
                    except (PlatformError, KeyError, ValueError, TypeError):
                        continue
                yield _sse("change", data)
        finally:
            await _close(subscription)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def _refresh_stream_principal(request: Request, original: Principal) -> None:
    current = request.app.state.authenticator.authenticate(request)
    require(current, "operations.read")
    if current.organization_id != original.organization_id or current.project_id != original.project_id:
        from app.core.errors import PermissionDeniedError

        raise PermissionDeniedError("Stream context changed")

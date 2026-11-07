"""Workload management: resources, actions, YAML apply, logs and terminals."""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.api.auth import get_principal
from app.api.deps import get_workload_service
from app.core.config import get_settings
from app.core.errors import PlatformError
from app.core.principal import Principal
from app.core.secrets import get_secret_store
from app.db.session import get_session_factory
from app.modules.auditing import service as audit
from app.modules.kubernetes.service import connection_target
from app.modules.workloads import terminal
from app.modules.workloads.service import WorkloadService

router = APIRouter(prefix="/clusters/{cluster_id}", tags=["workloads"])

NS = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"


class ScaleBody(BaseModel):
    replicas: int = Field(ge=0, le=1000)


class RollbackBody(BaseModel):
    revision: int = Field(ge=1)


class SuspendBody(BaseModel):
    suspend: bool


class ApplyBody(BaseModel):
    manifest: str = Field(max_length=2_000_000)
    namespace: str = Field(default="default", pattern=NS)
    dry_run: bool = True


@router.get("/resources/{kind}")
def list_resources(
    cluster_id: uuid.UUID,
    kind: str,
    namespace: str | None = Query(default=None, pattern=NS),
    principal: Principal = Depends(get_principal),
    service: WorkloadService = Depends(get_workload_service),
) -> dict[str, Any]:
    return service.list_resources(principal, cluster_id, kind, namespace)


@router.get("/resources/{kind}/{namespace}/{name}")
def get_resource(
    cluster_id: uuid.UUID,
    kind: str,
    namespace: str,
    name: str,
    principal: Principal = Depends(get_principal),
    service: WorkloadService = Depends(get_workload_service),
) -> dict[str, Any]:
    """Summary, object (Secret values redacted), edit-ready YAML, events and kind-specific detail."""
    return service.get(principal, cluster_id, kind, namespace, name)


@router.delete("/resources/{kind}/{namespace}/{name}", status_code=204, response_class=Response)
def delete_resource(
    cluster_id: uuid.UUID,
    kind: str,
    namespace: str,
    name: str,
    confirm: str | None = Query(default=None, description="Must equal the resource name"),
    principal: Principal = Depends(get_principal),
    service: WorkloadService = Depends(get_workload_service),
) -> Response:
    service.delete(principal, cluster_id, kind, namespace, name, confirm=confirm)
    return Response(status_code=204)


@router.post("/resources/{kind}/{namespace}/{name}/scale")
def scale(
    cluster_id: uuid.UUID,
    kind: str,
    namespace: str,
    name: str,
    body: ScaleBody,
    principal: Principal = Depends(get_principal),
    service: WorkloadService = Depends(get_workload_service),
) -> dict[str, Any]:
    return service.scale(principal, cluster_id, kind, namespace, name, body.replicas)


@router.post("/resources/{kind}/{namespace}/{name}/restart")
def restart(
    cluster_id: uuid.UUID,
    kind: str,
    namespace: str,
    name: str,
    principal: Principal = Depends(get_principal),
    service: WorkloadService = Depends(get_workload_service),
) -> dict[str, Any]:
    return service.restart(principal, cluster_id, kind, namespace, name)


@router.post("/deployments/{namespace}/{name}/rollback")
def rollback(
    cluster_id: uuid.UUID,
    namespace: str,
    name: str,
    body: RollbackBody,
    principal: Principal = Depends(get_principal),
    service: WorkloadService = Depends(get_workload_service),
) -> dict[str, Any]:
    return service.rollback(principal, cluster_id, namespace, name, body.revision)


@router.post("/cronjobs/{namespace}/{name}/trigger")
def trigger_cronjob(
    cluster_id: uuid.UUID,
    namespace: str,
    name: str,
    principal: Principal = Depends(get_principal),
    service: WorkloadService = Depends(get_workload_service),
) -> dict[str, Any]:
    return service.cronjob_trigger(principal, cluster_id, namespace, name)


@router.post("/cronjobs/{namespace}/{name}/suspend")
def suspend_cronjob(
    cluster_id: uuid.UUID,
    namespace: str,
    name: str,
    body: SuspendBody,
    principal: Principal = Depends(get_principal),
    service: WorkloadService = Depends(get_workload_service),
) -> dict[str, Any]:
    return service.cronjob_suspend(principal, cluster_id, namespace, name, body.suspend)


@router.post("/apply")
def apply_manifest(
    cluster_id: uuid.UUID,
    body: ApplyBody,
    principal: Principal = Depends(get_principal),
    service: WorkloadService = Depends(get_workload_service),
) -> dict[str, Any]:
    """Server-side dry run (default) or apply. Results include a per-object diff and any validation errors."""
    return service.apply(principal, cluster_id, body.manifest, namespace=body.namespace, dry_run=body.dry_run)


@router.post("/secrets/{namespace}/{name}/reveal")
def reveal_secret(
    cluster_id: uuid.UUID,
    namespace: str,
    name: str,
    principal: Principal = Depends(get_principal),
    service: WorkloadService = Depends(get_workload_service),
) -> dict[str, Any]:
    """Decode a Secret's values. Every reveal is recorded in the audit log."""
    return service.reveal_secret(principal, cluster_id, namespace, name)


@router.get("/pods/{namespace}/{name}/logs", response_class=PlainTextResponse)
def pod_logs(
    cluster_id: uuid.UUID,
    namespace: str,
    name: str,
    container: str | None = None,
    tail: int = Query(default=500, ge=1, le=10000),
    previous: bool = False,
    timestamps: bool = False,
    principal: Principal = Depends(get_principal),
    service: WorkloadService = Depends(get_workload_service),
) -> PlainTextResponse:
    text = service.logs(
        principal, cluster_id, namespace, name, container=container, tail=tail, previous=previous, timestamps=timestamps
    )
    return PlainTextResponse(text)


@router.get("/pods/{namespace}/{name}/logs/stream", response_class=StreamingResponse)
def stream_pod_logs(
    cluster_id: uuid.UUID,
    namespace: str,
    name: str,
    container: str | None = None,
    tail: int = Query(default=200, ge=0, le=5000),
    principal: Principal = Depends(get_principal),
    service: WorkloadService = Depends(get_workload_service),
) -> StreamingResponse:
    """Follow logs (Server-Sent Events: `line` events, then `end`). Sessions are capped at 15 minutes."""
    lines = service.follow_logs(principal, cluster_id, namespace, name, container=container, tail=tail)

    def events() -> Iterator[str]:
        try:
            for line in lines:
                yield f"event: line\ndata: {json.dumps(line)}\n\n"
        except PlatformError as exc:
            yield f"event: error\ndata: {json.dumps(exc.message)}\n\n"
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------- terminal (WebSocket)


def _origin_allowed(websocket: WebSocket) -> bool:
    """Reject cross-site WebSocket hijacking: the Origin must be this host or an allowed origin."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True  # non-browser clients
    if origin in get_settings().cors_origins:
        return True
    host = websocket.headers.get("host", "")
    return urlsplit(origin).netloc == host


@router.websocket("/pods/{namespace}/{name}/exec")
async def pod_terminal(
    websocket: WebSocket, cluster_id: uuid.UUID, namespace: str, name: str, container: str | None = None
) -> None:
    import re

    if not _origin_allowed(websocket):
        await websocket.close(code=4403, reason="origin not allowed")
        return
    valid = re.compile(NS)
    if (
        not valid.match(namespace)
        or not re.match(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$", name)
        or (container and not valid.match(container))
    ):
        await websocket.close(code=4400, reason="invalid pod reference")
        return
    principal: Principal = websocket.app.state.authenticator.authenticate(websocket)

    def prepare() -> tuple[Any, Any]:
        from app.modules.kubernetes.service import KubernetesService

        with get_session_factory()() as session:
            kubernetes = KubernetesService(session, get_secret_store(), None)  # type: ignore[arg-type]
            cluster = kubernetes.cluster(principal, cluster_id)
            target = connection_target(session, get_secret_store(), cluster)
            audit.record(
                session,
                principal=principal,
                action="pod.exec",
                resource_type="kubernetes.pod",
                resource_id=f"{cluster.id}/{namespace}/{name}",
                resource_name=f"{cluster.name}:{namespace}/{name}",
                details={"cluster": cluster.name, "namespace": namespace, "container": container, "event": "start"},
            )
            session.commit()
            return cluster, target

    try:
        cluster, target = await run_in_threadpool(prepare)
    except PlatformError as exc:
        await websocket.close(code=4404, reason=exc.message[:120])
        return

    await websocket.accept()
    started = time.monotonic()
    outcome = "closed"
    try:
        outcome = await terminal.bridge(websocket, target, namespace, name, container)
    except WebSocketDisconnect:
        outcome = "browser disconnected"
    finally:

        def record_end() -> None:
            with get_session_factory()() as session:
                audit.record(
                    session,
                    principal=principal,
                    action="pod.exec.end",
                    resource_type="kubernetes.pod",
                    resource_id=f"{cluster.id}/{namespace}/{name}",
                    resource_name=f"{cluster.name}:{namespace}/{name}",
                    details={"container": container, "outcome": outcome, "seconds": round(time.monotonic() - started)},
                )
                session.commit()

        await run_in_threadpool(record_end)
        with contextlib.suppress(RuntimeError):
            await websocket.close()

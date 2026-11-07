"""Liveness and readiness.

``/health/live``  process is up (no dependencies checked) — for container liveness probes.
``/health/ready`` dependency status — 503 only when the API cannot serve requests
                  (database down). Missing workers or Redis degrade, not fail, the
                  platform: the UI stays usable for troubleshooting.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Literal

import redis
from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.config import get_settings
from app.core.secrets import FernetSecretStore, get_secret_store
from app.db.migrations import migration_state
from app.db.session import get_session_factory

router = APIRouter(prefix="/health", tags=["health"])

Status = Literal["ok", "degraded", "unavailable"]


class ComponentHealth(BaseModel):
    name: str
    status: Status
    message: str
    latency_ms: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ReadinessReport(BaseModel):
    status: Status
    checked_at: datetime
    components: list[ComponentHealth]


def _timed(fn: Any) -> tuple[Any, float]:
    started = time.perf_counter()
    value = fn()
    return value, round((time.perf_counter() - started) * 1000, 1)


def check_database() -> ComponentHealth:
    try:
        with get_session_factory()() as session:
            _, latency = _timed(lambda: session.execute(text("SELECT 1")))
            migrations = migration_state(session)
    except Exception as exc:
        return ComponentHealth(name="database", status="unavailable", message=f"PostgreSQL unreachable: {exc}")
    if not migrations["up_to_date"]:
        return ComponentHealth(
            name="database",
            status="degraded",
            message="Schema migrations pending — run 'alembic upgrade head'",
            latency_ms=latency,
            details=migrations,
        )
    return ComponentHealth(
        name="database", status="ok", message="PostgreSQL reachable", latency_ms=latency, details=migrations
    )


def check_redis() -> ComponentHealth:
    client = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1)
    try:
        _, latency = _timed(client.ping)
    except redis.RedisError as exc:
        return ComponentHealth(
            name="redis",
            status="unavailable",
            message=(f"Redis unreachable; operations cannot be queued and live updates fall back to polling ({exc})"),
        )
    finally:
        client.close()
    return ComponentHealth(name="redis", status="ok", message="Redis reachable", latency_ms=latency)


def check_workers(redis_ok: bool) -> ComponentHealth:
    if get_settings().orchestration_inline:
        return ComponentHealth(name="workers", status="ok", message="Inline execution (test mode)")
    if not redis_ok:
        return ComponentHealth(name="workers", status="unavailable", message="Cannot query workers without Redis")
    from app.orchestration.celery_app import celery_app

    try:
        replies, latency = _timed(lambda: celery_app.control.ping(timeout=1.0))
    except Exception as exc:
        return ComponentHealth(name="workers", status="unavailable", message=f"Worker ping failed: {exc}")
    names = sorted(name for reply in replies for name in reply)
    if not names:
        return ComponentHealth(
            name="workers",
            status="degraded",
            message="No orchestration workers are responding; new operations will wait in the queue",
            latency_ms=latency,
        )
    return ComponentHealth(
        name="workers",
        status="ok",
        message=f"{len(names)} worker(s) responding",
        latency_ms=latency,
        details={"workers": names},
    )


def check_secret_store() -> ComponentHealth:
    store = get_secret_store()
    details: dict[str, Any] = {"backend": store.backend_name}
    if isinstance(store, FernetSecretStore):
        details["primary_key_id"] = store.primary_key_id
    return ComponentHealth(
        name="secret_store", status="ok", message="Credential encryption configured", details=details
    )


@router.get("/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", response_model=ReadinessReport)
def ready(response: Response) -> ReadinessReport:
    database = check_database()
    redis_health = check_redis()
    components = [database, redis_health, check_workers(redis_health.status == "ok"), check_secret_store()]
    if database.status == "unavailable":
        overall: Status = "unavailable"
        response.status_code = 503
    elif any(c.status != "ok" for c in components):
        overall = "degraded"
    else:
        overall = "ok"
    return ReadinessReport(status=overall, checked_at=datetime.now(UTC), components=components)

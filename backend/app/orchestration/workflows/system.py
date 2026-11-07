"""Platform self-check: verifies, from a worker's point of view, that every
dependency the orchestration engine relies on is functioning.

This is a genuine diagnostic (it performs real queries and round-trips), and it
exercises the full operation lifecycle end to end: API submission, broker
dispatch, worker claim, step checkpoints, logs and live updates.
"""

from __future__ import annotations

import os
import platform
import secrets as pysecrets
import time
from typing import Any

import redis
from pydantic import BaseModel
from sqlalchemy import text

from app.core.secrets import SealedSecret
from app.db.migrations import migration_state
from app.modules.cluster_lifecycle.distributions.registry import distribution_registry
from app.modules.providers.registry import provider_registry
from app.orchestration.context import StepContext
from app.orchestration.workflow import (
    OperationTarget,
    RetryableStepError,
    StepDefinition,
    StepFailedError,
    WorkflowDefinition,
    workflow_registry,
)

SELF_CHECK = "system.self_check"


class SelfCheckParams(BaseModel):
    pass


def check_worker(ctx: StepContext) -> dict[str, Any]:
    info = {
        "hostname": platform.node(),
        "pid": os.getpid(),
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
    }
    ctx.info(f"Executing on {info['hostname']} (pid {info['pid']}, Python {info['python']})")
    return info


def check_database(ctx: StepContext) -> dict[str, Any]:
    started = time.perf_counter()
    with ctx.db() as session:
        version = session.scalar(text("SHOW server_version"))
        migrations = migration_state(session)
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    ctx.info(f"PostgreSQL {version} responded in {latency_ms} ms")
    if not migrations["up_to_date"]:
        raise StepFailedError(
            f"Database schema is at revision {migrations['current']} but code expects {migrations['head']}. "
            "Run 'alembic upgrade head'.",
            code="migrations_pending",
        )
    ctx.info(f"Schema revision {migrations['current']} is current")
    return {"server_version": version, "latency_ms": latency_ms, "schema_revision": migrations["current"]}


def check_redis(ctx: StepContext) -> dict[str, Any]:
    client = redis.Redis.from_url(ctx.settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
    try:
        started = time.perf_counter()
        client.ping()
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        version = client.info("server").get("redis_version")
    except redis.RedisError as exc:
        raise RetryableStepError(f"Redis is not reachable: {exc}") from exc
    finally:
        client.close()
    ctx.info(f"Redis {version} responded in {latency_ms} ms")
    return {"redis_version": version, "latency_ms": latency_ms}


def check_secret_store(ctx: StepContext) -> dict[str, Any]:
    probe = pysecrets.token_bytes(32)
    sealed = ctx.secret_store.seal(probe)
    if ctx.secret_store.unseal(SealedSecret(sealed.key_id, sealed.ciphertext)) != probe:
        raise StepFailedError("Secret store round-trip returned different data", code="secret_store_corrupt")
    ctx.info(f"Encryption round-trip succeeded using key '{sealed.key_id}'")
    return {"backend": ctx.secret_store.backend_name, "primary_key_id": sealed.key_id}


def check_catalogs(ctx: StepContext) -> dict[str, Any]:
    providers = provider_registry.describe_all()
    distributions = distribution_registry.describe_all()

    def summarize(items: list[Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            counts[item.implementation_status.value] = counts.get(item.implementation_status.value, 0) + 1
        return counts

    provider_counts, distribution_counts = summarize(providers), summarize(distributions)
    ctx.info(f"Infrastructure providers registered: {len(providers)} {provider_counts}")
    ctx.info(f"Kubernetes distributions registered: {len(distributions)} {distribution_counts}")
    ctx.set_result(
        {
            "worker": ctx.outputs.get("worker", {}),
            "database": ctx.outputs.get("database", {}),
            "redis": ctx.outputs.get("redis", {}),
            "secret_store": ctx.outputs.get("secret_store", {}),
            "providers": provider_counts,
            "distributions": distribution_counts,
        }
    )
    return {"providers": provider_counts, "distributions": distribution_counts}


self_check_workflow = workflow_registry.register(
    WorkflowDefinition(
        type=SELF_CHECK,
        title="Platform self-check",
        description="Verify database, Redis, secret storage and catalogs from an orchestration worker.",
        params_model=SelfCheckParams,
        plan=lambda _p: [
            StepDefinition("worker", "Identify execution worker", check_worker),
            StepDefinition("database", "Check PostgreSQL and schema revision", check_database),
            StepDefinition("redis", "Check Redis", check_redis, max_attempts=3, retry_backoff_seconds=2),
            StepDefinition("secret_store", "Verify credential encryption", check_secret_store),
            StepDefinition("catalogs", "Load provider and distribution catalogs", check_catalogs),
        ],
        target=lambda _p: OperationTarget(type="platform", id="self-check", name="Platform"),
        user_startable=True,
    )
)

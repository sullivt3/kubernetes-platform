"""Celery application: operation execution and periodic recovery.

Run a worker:     celery -A app.orchestration.celery_app worker --loglevel=INFO
                  (on Windows add --pool=solo; prefork is unsupported there)
Run scheduler:    celery -A app.orchestration.celery_app beat --loglevel=INFO
"""

from __future__ import annotations

import logging
import uuid

from celery import Celery
from celery.signals import setup_logging, worker_process_init

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()

celery_app = Celery("kplatform", broker=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    # Acknowledge only after the task finishes so a crashed worker's message is redelivered.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_default_queue="operations",
    beat_schedule={
        "recover-operations": {"task": "operations.recover", "schedule": 30.0},
        "probe-clusters": {"task": "clusters.probe", "schedule": 60.0},
        "probe-machines": {"task": "machines.probe", "schedule": float(settings.machine_probe_interval_seconds)},
    },
    timezone="UTC",
)

logger = logging.getLogger(__name__)


@setup_logging.connect
def _configure_logging(**_: object) -> None:
    configure_logging(settings.log_level, settings.log_json)


@worker_process_init.connect
def _reset_connections(**_: object) -> None:
    # Never share DB connections across forked worker processes.
    from app.db.session import get_engine

    get_engine().dispose(close=False)


@celery_app.task(name="operations.execute")
def execute_operation(operation_id: str) -> str | None:
    from app.orchestration.runtime import build_executor

    status = build_executor().execute(uuid.UUID(operation_id))
    return status.value if status else None


@celery_app.task(name="operations.recover")
def recover() -> dict[str, list[str]]:
    from app.db.session import get_session_factory
    from app.orchestration.runtime import get_dispatcher, get_publisher, load_workflows
    from app.orchestration.service import recover_operations
    from app.orchestration.workflow import workflow_registry

    load_workflows()
    report = recover_operations(
        get_session_factory(),
        registry=workflow_registry,
        dispatcher=get_dispatcher(),
        publisher=get_publisher(),
        settings=get_settings(),
    )
    summary = report.as_dict()
    if any(summary.values()):
        logger.warning("Operation recovery: %s", summary)
    return summary


@celery_app.task(name="clusters.probe")
def probe_clusters() -> dict[str, int]:
    """Observe the health of every connected cluster and record it."""
    from app.core.secrets import get_secret_store
    from app.db.session import get_session_factory
    from app.modules.kubernetes.service import probe_clusters as run_probe
    from app.orchestration.runtime import load_workflows

    load_workflows()
    return run_probe(get_session_factory(), get_secret_store())


@celery_app.task(name="machines.probe")
def probe_machines() -> dict[str, int]:
    from app.core.secrets import get_secret_store
    from app.db.session import get_session_factory
    from app.modules.infrastructure.health import probe_machines as run_probe
    from app.orchestration.runtime import load_workflows

    load_workflows()
    return run_probe(get_session_factory(), get_secret_store())


# `celery -A app.orchestration.celery_app` looks for `app` or `celery` attributes.
app = celery_app

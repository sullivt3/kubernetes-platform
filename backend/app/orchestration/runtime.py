"""Process-wide wiring of the orchestration engine (publisher, dispatcher, executor)."""

from __future__ import annotations

import os
import socket
from functools import lru_cache

from app.core.config import get_settings
from app.core.secrets import get_secret_store
from app.db.session import get_session_factory
from app.orchestration.dispatch import CeleryDispatcher, InlineDispatcher, OperationDispatcher
from app.orchestration.events import EventPublisher, RedisEventPublisher
from app.orchestration.executor import OperationExecutor
from app.orchestration.workflow import workflow_registry


def load_workflows() -> None:
    """Import every model (complete mapper registry) and workflow module (idempotent).

    Worker processes don't import the API routes, so the model registry must be
    loaded explicitly or relationships/foreign keys to unimported tables fail.
    """
    import app.db.models
    import app.orchestration.workflows  # noqa: F401


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@lru_cache
def get_publisher() -> EventPublisher:
    return RedisEventPublisher(get_settings().redis_url)


def build_executor(worker_id: str | None = None) -> OperationExecutor:
    load_workflows()
    return OperationExecutor(
        session_factory=get_session_factory(),
        registry=workflow_registry,
        publisher=get_publisher(),
        settings=get_settings(),
        secret_store=get_secret_store(),
        worker_id=worker_id or worker_identity(),
    )


@lru_cache
def get_dispatcher() -> OperationDispatcher:
    if get_settings().orchestration_inline:
        return InlineDispatcher(lambda operation_id: build_executor("inline").execute(operation_id))
    return CeleryDispatcher()

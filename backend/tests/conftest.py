"""Test configuration.

Tests run against a real PostgreSQL database (``kplatform_test``) and Redis from
the development docker-compose stack. The schema is built with the real Alembic
migrations, so migrations are exercised on every run.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet

# Must be set before any application module reads settings.
os.environ["ENVIRONMENT"] = "test"
os.environ["ORCHESTRATION_INLINE"] = "true"
os.environ["AUTH_MODE"] = "dev"
os.environ["SECRET_ENCRYPTION_KEYS"] = f"test:{Fernet.generate_key().decode()}"
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL", "postgresql+psycopg://kplatform:kplatform-dev-only@localhost:5432/kplatform_test"
    ),
)
os.environ.setdefault("REDIS_URL", os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"))

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.principal import Principal
from app.core.secrets import get_secret_store
from app.db.migrations import alembic_config
from app.db.models import Base
from app.db.session import get_engine, get_session_factory
from app.modules.organizations.service import ensure_default_tenancy, ensure_dev_principal
from app.orchestration.dispatch import InlineDispatcher, OperationDispatcher
from app.orchestration.events import RecordingEventPublisher
from app.orchestration.executor import OperationExecutor
from app.orchestration.service import OperationService
from app.orchestration.workflow import workflow_registry


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    url = get_settings().database_url
    assert "test" in url.rsplit("/", 1)[-1], f"Refusing to run tests against non-test database: {url}"
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    config = alembic_config()
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(database: None) -> Iterator[None]:
    yield
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def session() -> Iterator[Session]:
    s = get_session_factory()()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def principal(session: Session) -> Principal:
    return ensure_dev_principal(session, ensure_default_tenancy(session))


@pytest.fixture
def publisher() -> RecordingEventPublisher:
    return RecordingEventPublisher()


class RecordingDispatcher(OperationDispatcher):
    """Accepts operations without running them (lets tests drive execution explicitly)."""

    def __init__(self, fail: bool = False) -> None:
        self.dispatched: list[uuid.UUID] = []
        self.fail = fail

    def dispatch(self, operation_id: uuid.UUID) -> str | None:
        if self.fail:
            raise ConnectionError("broker unavailable")
        self.dispatched.append(operation_id)
        return f"recorded-{len(self.dispatched)}"


@pytest.fixture
def executor(publisher: RecordingEventPublisher) -> OperationExecutor:
    return OperationExecutor(
        session_factory=get_session_factory(),
        registry=workflow_registry,
        publisher=publisher,
        settings=get_settings(),
        secret_store=get_secret_store(),
        worker_id="test-worker",
        sleep=lambda _s: None,
    )


@pytest.fixture
def recording_dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def inline_service(
    session: Session, executor: OperationExecutor, publisher: RecordingEventPublisher
) -> OperationService:
    return OperationService(
        session,
        registry=workflow_registry,
        dispatcher=InlineDispatcher(executor.execute),
        publisher=publisher,
    )


@pytest.fixture
def deferred_service(
    session: Session, recording_dispatcher: RecordingDispatcher, publisher: RecordingEventPublisher
) -> OperationService:
    return OperationService(
        session, registry=workflow_registry, dispatcher=recording_dispatcher, publisher=publisher
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as c:
        yield c

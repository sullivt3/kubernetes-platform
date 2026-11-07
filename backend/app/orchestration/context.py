"""The runtime context handed to every workflow step."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.secrets import SecretStore
from app.orchestration.models import LogLevel, Operation
from app.orchestration.workflow import OperationCancelledError

LogFn = Callable[[LogLevel, str, str | None], None]
ProgressFn = Callable[[float], None]


class StepContext:
    """Facilities available to a step.

    Steps should not hold database transactions open across slow work: use
    ``with ctx.db() as session:`` for short, focused transactions.
    """

    def __init__(
        self,
        *,
        operation_id: uuid.UUID,
        organization_id: uuid.UUID,
        step_key: str,
        attempt: int,
        params: BaseModel,
        outputs: dict[str, dict[str, Any]],
        session_factory: sessionmaker[Session],
        settings: Settings,
        secret_store: SecretStore,
        redactions: set[str],
        log_fn: LogFn,
        progress_fn: ProgressFn,
    ) -> None:
        self.operation_id = operation_id
        self.organization_id = organization_id
        self.step_key = step_key
        self.attempt = attempt
        self.params = params
        self.outputs = outputs
        self.settings = settings
        self.secret_store = secret_store
        self._session_factory = session_factory
        self._redactions = redactions
        self._log_fn = log_fn
        self._progress_fn = progress_fn
        self.result: dict[str, Any] | None = None

    # ---- Logging --------------------------------------------------------------
    def log(self, message: str, level: LogLevel = LogLevel.INFO) -> None:
        self._log_fn(level, message, self.step_key)

    def info(self, message: str) -> None:
        self.log(message, LogLevel.INFO)

    def warning(self, message: str) -> None:
        self.log(message, LogLevel.WARNING)

    def error(self, message: str) -> None:
        self.log(message, LogLevel.ERROR)

    def redact(self, *values: str) -> None:
        """Register secret values that must be masked in every subsequent log line."""
        for value in values:
            if value and len(value) >= 4:
                self._redactions.add(value)

    # ---- Progress / cancellation ----------------------------------------------
    def set_progress(self, fraction: float) -> None:
        """Report progress within the current step (0.0 to 1.0)."""
        self._progress_fn(max(0.0, min(1.0, fraction)))

    def is_cancel_requested(self) -> bool:
        with self.db() as session:
            requested = session.scalar(select(Operation.cancel_requested_at).where(Operation.id == self.operation_id))
        return requested is not None

    def check_cancelled(self) -> None:
        if self.is_cancel_requested():
            raise OperationCancelledError()

    # ---- Data -----------------------------------------------------------------
    @contextmanager
    def db(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def set_result(self, result: dict[str, Any]) -> None:
        """Set the operation's final result (shown on the operation detail page)."""
        self.result = result

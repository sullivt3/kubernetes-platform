"""Hand-off of operations to an execution backend.

``CeleryDispatcher`` is the production path. ``InlineDispatcher`` executes in the
calling thread and exists for tests. Both satisfy the same interface, so a durable
workflow engine such as Temporal can be introduced later behind it.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable


class OperationDispatcher(ABC):
    @abstractmethod
    def dispatch(self, operation_id: uuid.UUID) -> str | None:
        """Submit the operation for execution; returns a backend-specific dispatch id.

        Raises on failure. Duplicate dispatches are safe (executors claim atomically).
        """


class CeleryDispatcher(OperationDispatcher):
    def dispatch(self, operation_id: uuid.UUID) -> str | None:
        from app.orchestration.celery_app import execute_operation

        result = execute_operation.apply_async(args=[str(operation_id)], retry=False)
        return result.id


class InlineDispatcher(OperationDispatcher):
    def __init__(self, run: Callable[[uuid.UUID], object]) -> None:
        self._run = run

    def dispatch(self, operation_id: uuid.UUID) -> str | None:
        self._run(operation_id)
        return f"inline-{operation_id}"

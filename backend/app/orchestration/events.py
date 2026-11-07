"""Change notifications for live UI updates.

The database is the single source of truth for operation state and logs. Redis
pub/sub is used only as a *doorbell*: publishers announce "operation X changed",
and stream endpoints react by reading the database from their cursor. Therefore a
lost notification can delay an update but never lose or reorder data, and if
Redis is unavailable streams degrade to periodic polling.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from enum import StrEnum

import redis

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "kplatform"


def operation_channel(operation_id: uuid.UUID | str) -> str:
    return f"{CHANNEL_PREFIX}:operation:{operation_id}"


def organization_operations_channel(organization_id: uuid.UUID | str) -> str:
    """Per-organization feed used by the operations center (keeps tenants isolated)."""
    return f"{CHANNEL_PREFIX}:org:{organization_id}:operations"


class ChangeKind(StrEnum):
    STATUS = "status"
    STEP = "step"
    LOG = "log"
    PROGRESS = "progress"


class EventPublisher(ABC):
    @abstractmethod
    def publish(
        self, organization_id: uuid.UUID, operation_id: uuid.UUID, kind: ChangeKind, status: str | None = None
    ) -> None:
        """Announce a change. Must never raise."""


class NullEventPublisher(EventPublisher):
    def publish(
        self, organization_id: uuid.UUID, operation_id: uuid.UUID, kind: ChangeKind, status: str | None = None
    ) -> None:
        return None


class RecordingEventPublisher(EventPublisher):
    """Test double that records every notification."""

    def __init__(self) -> None:
        self.events: list[tuple[uuid.UUID, uuid.UUID, ChangeKind, str | None]] = []

    def publish(
        self, organization_id: uuid.UUID, operation_id: uuid.UUID, kind: ChangeKind, status: str | None = None
    ) -> None:
        self.events.append((organization_id, operation_id, kind, status))


class RedisEventPublisher(EventPublisher):
    _WARN_INTERVAL = 60.0

    def __init__(self, redis_url: str) -> None:
        self._client = redis.Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1)
        self._last_warning = 0.0

    def publish(
        self, organization_id: uuid.UUID, operation_id: uuid.UUID, kind: ChangeKind, status: str | None = None
    ) -> None:
        payload = json.dumps({"operation_id": str(operation_id), "kind": kind.value, "status": status})
        try:
            pipe = self._client.pipeline(transaction=False)
            pipe.publish(operation_channel(operation_id), payload)
            pipe.publish(organization_operations_channel(organization_id), payload)
            pipe.execute()
        except redis.RedisError as exc:
            now = time.monotonic()
            if now - self._last_warning > self._WARN_INTERVAL:
                self._last_warning = now
                logger.warning("Could not publish operation change notification: %s", exc)

"""Persistent state of long-running operations.

``Operation``      one requested unit of work (create cluster, join node, self-check...)
``OperationStep``  ordered, checkpointed steps of that work. Steps that succeeded
                   are never re-run when an operation is resumed or retried.
``OperationLog``   append-only execution log. The global identity column gives a
                   total order that live streams use as a resume cursor.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JsonDict, OrganizationScoped, Timestamps, UUIDPrimaryKey, str_enum


class OperationStatus(StrEnum):
    PENDING = "pending"  # persisted; not yet handed to the broker (or hand-off failed)
    QUEUED = "queued"  # handed to the broker, waiting for a worker
    RUNNING = "running"
    CANCELLING = "cancelling"  # cancellation requested; worker will stop at the next safe point
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset({OperationStatus.SUCCEEDED, OperationStatus.FAILED, OperationStatus.CANCELLED})
ACTIVE_STATUSES = frozenset(set(OperationStatus) - TERMINAL_STATUSES)


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


_ACTIVE_SQL = ", ".join(f"'{s.value}'" for s in sorted(ACTIVE_STATUSES))


class Operation(UUIDPrimaryKey, Timestamps, OrganizationScoped, Base):
    __tablename__ = "operations"
    __table_args__ = (
        Index("ix_operations_status", "status"),
        Index("ix_operations_target", "target_type", "target_id"),
        Index("ix_operations_created_at", "created_at"),
        # Mutual exclusion: at most one ACTIVE operation per concurrency key (for
        # example "cluster:<id>"), enforced by the database rather than by hope.
        Index(
            "uq_operations_active_concurrency_key",
            "concurrency_key",
            unique=True,
            postgresql_where=text(f"concurrency_key IS NOT NULL AND status IN ({_ACTIVE_SQL})"),
        ),
        UniqueConstraint("organization_id", "idempotency_key"),
    )

    type: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[OperationStatus] = mapped_column(str_enum(OperationStatus, 16), default=OperationStatus.PENDING)

    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(128))
    target_name: Mapped[str | None] = mapped_column(String(255))

    params: Mapped[JsonDict] = mapped_column(default=dict)
    result: Mapped[JsonDict | None]
    # {"code", "message", "step", "retryable"}
    error: Mapped[JsonDict | None]

    progress: Mapped[float] = mapped_column(default=0.0)
    current_step: Mapped[str | None] = mapped_column(String(128))

    concurrency_key: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))

    attempt: Mapped[int] = mapped_column(default=1)  # incremented by manual retry
    recovery_count: Mapped[int] = mapped_column(default=0)  # automatic resumes after worker loss

    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    requested_by_subject: Mapped[str] = mapped_column(String(512))
    parent_operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operations.id", ondelete="SET NULL"))

    dispatch_id: Mapped[str | None] = mapped_column(String(128))
    worker_id: Mapped[str | None] = mapped_column(String(255))
    cancel_requested_at: Mapped[datetime | None]
    queued_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]

    steps: Mapped[list[OperationStep]] = relationship(
        back_populates="operation",
        order_by="OperationStep.position",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class OperationStep(UUIDPrimaryKey, Base):
    __tablename__ = "operation_steps"
    __table_args__ = (UniqueConstraint("operation_id", "key"),)

    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("operations.id", ondelete="CASCADE"))
    position: Mapped[int]
    key: Mapped[str] = mapped_column(String(128))  # stable identifier used for resume
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[StepStatus] = mapped_column(str_enum(StepStatus, 16), default=StepStatus.PENDING)
    attempts: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    error: Mapped[JsonDict | None]
    # Checkpoint data produced by the step; available to later steps and on resume.
    output: Mapped[JsonDict] = mapped_column(default=dict)

    operation: Mapped[Operation] = relationship(back_populates="steps")


class OperationLog(Base):
    __tablename__ = "operation_logs"
    __table_args__ = (Index("ix_operation_logs_operation_id_id", "operation_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("operations.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(server_default=func.now())
    level: Mapped[LogLevel] = mapped_column(str_enum(LogLevel, 16))
    step_key: Mapped[str | None] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)

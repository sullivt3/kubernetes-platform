"""Operation lifecycle: submission, cancellation, retry, queries and recovery."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import ConfirmationRequiredError, ConflictError, InvalidStateError, NotFoundError
from app.core.principal import Principal
from app.db.base import OrganizationScoped
from app.modules.auditing import service as audit
from app.orchestration.dispatch import OperationDispatcher
from app.orchestration.events import ChangeKind, EventPublisher
from app.orchestration.executor import utcnow
from app.orchestration.hooks import run_status_hook
from app.orchestration.models import (
    ACTIVE_STATUSES,
    LogLevel,
    Operation,
    OperationLog,
    OperationStatus,
    OperationStep,
    StepStatus,
)
from app.orchestration.workflow import WorkflowRegistry

logger = logging.getLogger(__name__)

_CONCURRENCY_CONSTRAINT = "uq_operations_active_concurrency_key"
_IDEMPOTENCY_CONSTRAINT = "uq_operations_organization_id"


def _constraint_name(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


class OperationService:
    def __init__(
        self,
        session: Session,
        *,
        registry: WorkflowRegistry,
        dispatcher: OperationDispatcher,
        publisher: EventPublisher,
    ) -> None:
        self.session = session
        self.registry = registry
        self.dispatcher = dispatcher
        self.publisher = publisher

    # ------------------------------------------------------------------ commands

    def submit(
        self,
        principal: Principal,
        workflow_type: str,
        params: dict[str, Any],
        *,
        project_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
        parent_operation_id: uuid.UUID | None = None,
        confirm: str | None = None,
    ) -> Operation:
        workflow = self.registry.get(workflow_type)
        validated = workflow.validate_params(params)
        target = workflow.target(validated)

        # Organization-wide administrators may operate on project-owned targets.
        # Keep the resulting operation and worker-created resources in that project.
        if target is not None:
            from app.modules.cluster_lifecycle.models import Cluster
            from app.modules.infrastructure.models import Machine, ProviderConnection

            models = {
                "cluster": Cluster,
                "node": Cluster,
                "machine": Machine,
                "provider_connection": ProviderConnection,
            }
            model = models.get(target.type)
            if model is not None:
                try:
                    target_uuid = uuid.UUID(target.id.split("/")[0])
                except ValueError:
                    target_uuid = None
                entity = self.session.get(model, target_uuid) if target_uuid else None
                if isinstance(entity, OrganizationScoped):
                    if entity.organization_id != principal.organization_id:
                        raise NotFoundError("Operation target not found")
                    project_id = entity.project_id
        if project_id is None:
            project_id = principal.project_id

        if workflow.destructive:
            expected = (target.name or target.id) if target else workflow.type
            if confirm != expected:
                raise ConfirmationRequiredError(
                    "This operation is destructive. Repeat the target name to confirm.",
                    details={"expected_confirmation": expected},
                )

        if idempotency_key:
            existing = self._find_idempotent(principal.organization_id, idempotency_key)
            if existing is not None:
                return self._idempotent_match(existing, workflow_type)

        plan = workflow.build_plan(validated)
        op_id = uuid.uuid4()
        op = Operation(
            id=op_id,
            organization_id=principal.organization_id,
            project_id=project_id,
            type=workflow.type,
            title=workflow.render_title(validated),
            status=OperationStatus.PENDING,
            target_type=target.type if target else None,
            target_id=target.id if target else None,
            target_name=target.name if target else None,
            concurrency_key=target.concurrency_key if target else None,
            idempotency_key=idempotency_key,
            params=validated.model_dump(mode="json"),
            requested_by_id=principal.user_id,
            requested_by_subject=principal.subject,
            parent_operation_id=parent_operation_id,
            steps=[
                OperationStep(position=i, key=step.key, name=step.name, status=StepStatus.PENDING)
                for i, step in enumerate(plan)
            ],
        )
        self.session.add(op)
        self._log(op_id, f"Operation requested by {principal.display_name}")
        audit.record(
            self.session,
            principal=principal,
            action="operation.submit",
            resource_type="operation",
            resource_id=op_id,
            resource_name=op.title,
            details={"type": workflow.type, "target": op.concurrency_key},
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            constraint = _constraint_name(exc)
            if constraint == _CONCURRENCY_CONSTRAINT:
                raise ConflictError(
                    f"Another operation is already in progress for {target.name or target.id}"
                    if target
                    else "Another operation is already in progress",
                    details={"concurrency_key": target.concurrency_key if target else None},
                ) from exc
            if constraint == _IDEMPOTENCY_CONSTRAINT and idempotency_key:
                existing = self._find_idempotent(principal.organization_id, idempotency_key)
                if existing is not None:
                    return self._idempotent_match(existing, workflow_type)
            raise

        self.publisher.publish(principal.organization_id, op_id, ChangeKind.STATUS, OperationStatus.PENDING.value)
        self.dispatch(op_id)
        return self.get(principal, op_id)

    def dispatch(self, operation_id: uuid.UUID) -> bool:
        """Hand the operation to the execution backend. Failure leaves it PENDING for the recovery job."""
        organization_id = self.session.scalar(select(Operation.organization_id).where(Operation.id == operation_id))
        self.session.commit()  # end the read transaction before a potentially slow hand-off
        if organization_id is None:
            raise NotFoundError("Operation not found")
        try:
            dispatch_id = self.dispatcher.dispatch(operation_id)
        except Exception as exc:
            logger.warning("Dispatch of operation %s failed: %s", operation_id, exc)
            self._log(
                operation_id,
                "Could not reach the execution backend; the operation will be dispatched automatically "
                "when it becomes available",
                LogLevel.WARNING,
            )
            self.session.commit()
            self.publisher.publish(organization_id, operation_id, ChangeKind.LOG)
            return False
        self.session.execute(
            update(Operation)
            .where(Operation.id == operation_id, Operation.status == OperationStatus.PENDING)
            .values(status=OperationStatus.QUEUED, queued_at=utcnow(), dispatch_id=dispatch_id)
        )
        self.session.commit()
        self.publisher.publish(organization_id, operation_id, ChangeKind.STATUS)
        return True

    def cancel(self, principal: Principal, operation_id: uuid.UUID) -> Operation:
        op = self._get_for_update(principal, operation_id)
        if op.status.is_terminal:
            raise InvalidStateError(f"Operation is already {op.status.value}")
        now = utcnow()
        if op.status in (OperationStatus.PENDING, OperationStatus.QUEUED):
            op.status = OperationStatus.CANCELLED
            op.cancel_requested_at = now
            op.finished_at = now
            message = f"Cancelled by {principal.display_name} before execution started"
        elif op.status is OperationStatus.RUNNING:
            op.status = OperationStatus.CANCELLING
            op.cancel_requested_at = now
            message = f"Cancellation requested by {principal.display_name}; stopping at the next safe point"
        else:  # already CANCELLING
            return op
        self._log(op.id, message, LogLevel.WARNING)
        audit.record(
            self.session,
            principal=principal,
            action="operation.cancel",
            resource_type="operation",
            resource_id=op.id,
            resource_name=op.title,
        )
        self.session.commit()
        if op.status is OperationStatus.CANCELLED:
            run_status_hook(self.session, self.registry, op.id)
        self.publisher.publish(op.organization_id, op.id, ChangeKind.STATUS, op.status.value)
        return self.get(principal, operation_id)

    def retry(self, principal: Principal, operation_id: uuid.UUID) -> Operation:
        op = self._get_for_update(principal, operation_id)
        if op.status not in (OperationStatus.FAILED, OperationStatus.CANCELLED):
            raise InvalidStateError("Only failed or cancelled operations can be retried")
        self.registry.get(op.type)  # the workflow must still exist

        completed = sum(1 for s in op.steps if s.status is StepStatus.SUCCEEDED)
        for step in op.steps:
            if step.status is not StepStatus.SUCCEEDED:
                step.status = StepStatus.PENDING
                step.attempts = 0
                step.error = None
                step.started_at = None
                step.finished_at = None
        op.status = OperationStatus.PENDING
        op.attempt += 1
        op.recovery_count = 0
        op.error = None
        op.result = None
        op.finished_at = None
        op.cancel_requested_at = None
        op.heartbeat_at = None
        op.worker_id = None
        op.current_step = None
        op.progress = round(completed / len(op.steps) * 100, 1) if op.steps else 0.0
        self._log(
            op.id,
            f"Retry requested by {principal.display_name} (attempt {op.attempt}); "
            f"{completed} completed step(s) will be skipped",
        )
        audit.record(
            self.session,
            principal=principal,
            action="operation.retry",
            resource_type="operation",
            resource_id=op.id,
            resource_name=op.title,
            details={"attempt": op.attempt},
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            if _constraint_name(exc) == _CONCURRENCY_CONSTRAINT:
                raise ConflictError("Another operation is already in progress for this target") from exc
            raise
        self.publisher.publish(op.organization_id, op.id, ChangeKind.STATUS, OperationStatus.PENDING.value)
        self.dispatch(op.id)
        return self.get(principal, operation_id)

    # ------------------------------------------------------------------ queries

    def get(self, principal: Principal, operation_id: uuid.UUID) -> Operation:
        op = self.session.scalar(
            select(Operation)
            .where(Operation.id == operation_id, Operation.organization_id == principal.organization_id)
            .execution_options(populate_existing=True)
        )
        if op is None:
            raise NotFoundError("Operation not found")
        return op

    def list_operations(
        self,
        principal: Principal,
        *,
        statuses: list[OperationStatus] | None = None,
        active_only: bool = False,
        type: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Operation], int]:
        query = select(Operation).where(Operation.organization_id == principal.organization_id)
        if active_only:
            query = query.where(Operation.status.in_(ACTIVE_STATUSES))
        if statuses:
            query = query.where(Operation.status.in_(statuses))
        if type:
            query = query.where(Operation.type == type)
        if target_type:
            query = query.where(Operation.target_type == target_type)
        if target_id:
            query = query.where(Operation.target_id == target_id)
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.session.scalars(
            query.order_by(Operation.created_at.desc(), Operation.id).limit(limit).offset(offset)
        ).all()
        return list(rows), total

    def logs(
        self, principal: Principal, operation_id: uuid.UUID, *, after_id: int = 0, limit: int = 500
    ) -> list[OperationLog]:
        self.get(principal, operation_id)  # enforces tenancy
        return list(
            self.session.scalars(
                select(OperationLog)
                .where(OperationLog.operation_id == operation_id, OperationLog.id > after_id)
                .order_by(OperationLog.id)
                .limit(limit)
            ).all()
        )

    # ------------------------------------------------------------------ helpers

    def _find_idempotent(self, organization_id: uuid.UUID, key: str) -> Operation | None:
        return self.session.scalar(
            select(Operation).where(Operation.organization_id == organization_id, Operation.idempotency_key == key)
        )

    @staticmethod
    def _idempotent_match(existing: Operation, workflow_type: str) -> Operation:
        if existing.type != workflow_type:
            raise ConflictError("Idempotency key was already used for a different operation type")
        return existing

    def _get_for_update(self, principal: Principal, operation_id: uuid.UUID) -> Operation:
        op = self.session.scalar(
            select(Operation)
            .where(Operation.id == operation_id, Operation.organization_id == principal.organization_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if op is None:
            raise NotFoundError("Operation not found")
        return op

    def _log(self, operation_id: uuid.UUID, message: str, level: LogLevel = LogLevel.INFO) -> None:
        self.session.add(OperationLog(operation_id=operation_id, level=level, message=message))


# ---------------------------------------------------------------------- recovery


@dataclass
class RecoveryReport:
    resumed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    redispatched: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "resumed": self.resumed,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "redispatched": self.redispatched,
        }


def recover_operations(
    session_factory: sessionmaker[Session],
    *,
    registry: WorkflowRegistry,
    dispatcher: OperationDispatcher,
    publisher: EventPublisher,
    settings: Settings,
    now: datetime | None = None,
) -> RecoveryReport:
    """Repair operations left behind by crashed workers or lost broker messages.

    * RUNNING with a stale heartbeat  -> resumed from the interrupted step (bounded
      by OPERATION_MAX_RECOVERIES), otherwise failed with ``worker_lost``.
    * CANCELLING with a stale heartbeat -> cancelled.
    * PENDING for too long (dispatch failed) -> dispatched again.
    * QUEUED for too long (message may be lost) -> dispatched again (duplicates are safe).
    """
    now = now or utcnow()
    stale_cutoff = now - timedelta(seconds=settings.operation_stale_after_seconds)
    pending_cutoff = now - timedelta(seconds=settings.operation_pending_redispatch_seconds)
    report = RecoveryReport()
    to_dispatch: list[uuid.UUID] = []

    with session_factory() as session:
        stale = session.scalars(
            select(Operation)
            .where(
                Operation.status.in_((OperationStatus.RUNNING, OperationStatus.CANCELLING)),
                Operation.heartbeat_at < stale_cutoff,
            )
            .with_for_update(skip_locked=True)
        ).all()
        for op in stale:
            interrupted = [s for s in op.steps if s.status is StepStatus.RUNNING]
            lost = f"Worker {op.worker_id or 'unknown'} stopped responding"
            if op.status is OperationStatus.CANCELLING:
                op.status = OperationStatus.CANCELLED
                op.finished_at = now
                for step in interrupted:
                    step.status, step.finished_at = StepStatus.CANCELLED, now
                session.add(OperationLog(operation_id=op.id, level=LogLevel.WARNING, message=f"{lost}; cancelled"))
                report.cancelled.append(str(op.id))
            elif op.recovery_count < settings.operation_max_recoveries:
                op.status = OperationStatus.PENDING
                op.recovery_count += 1
                op.worker_id = None
                for step in interrupted:
                    step.status = StepStatus.PENDING
                session.add(
                    OperationLog(
                        operation_id=op.id,
                        level=LogLevel.WARNING,
                        message=f"{lost}; resuming from the interrupted step "
                        f"(recovery {op.recovery_count}/{settings.operation_max_recoveries})",
                    )
                )
                to_dispatch.append(op.id)
                report.resumed.append(str(op.id))
            else:
                error = {"code": "worker_lost", "message": f"{lost} too many times", "retryable": True}
                op.status = OperationStatus.FAILED
                op.finished_at = now
                op.error = error
                for step in interrupted:
                    step.status, step.finished_at, step.error = StepStatus.FAILED, now, error
                session.add(OperationLog(operation_id=op.id, level=LogLevel.ERROR, message=error["message"]))
                report.failed.append(str(op.id))

        pending = session.scalars(
            select(Operation.id).where(
                Operation.status == OperationStatus.PENDING, Operation.updated_at < pending_cutoff
            )
        ).all()
        queued = session.scalars(
            select(Operation)
            .where(Operation.status == OperationStatus.QUEUED, Operation.queued_at < stale_cutoff)
            .with_for_update(skip_locked=True)
        ).all()
        for op in queued:
            op.queued_at = now  # avoid re-dispatch storms while the queue drains
            to_dispatch.append(op.id)
        to_dispatch.extend(pending)
        session.commit()

        for op in stale:
            if op.status.is_terminal:
                run_status_hook(session, registry, op.id)
            publisher.publish(op.organization_id, op.id, ChangeKind.STATUS)

        # Dispatch failures are logged on the operation and retried on the next pass.
        service = OperationService(session, registry=registry, dispatcher=dispatcher, publisher=publisher)
        resumed = set(report.resumed)
        for op_id in dict.fromkeys(to_dispatch):
            if str(op_id) not in resumed:
                report.redispatched.append(str(op_id))
            service.dispatch(op_id)
    return report

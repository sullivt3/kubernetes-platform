"""Executes operations step by step with checkpointing.

Guarantees
----------
* **Single executor per operation.** A worker must atomically *claim* the
  operation (``pending|queued -> running``). Duplicate broker deliveries and
  re-dispatches are therefore harmless.
* **Resumable.** Step outcomes are persisted as they happen. A resumed or retried
  operation skips completed steps and continues from the first incomplete one.
* **Observable.** Every transition and log line is persisted and announced.
* **Liveness.** A heartbeat thread updates ``heartbeat_at``; the recovery job
  resumes (or fails) operations whose worker disappeared.
* **Cancellable** at step boundaries and wherever a step calls
  ``ctx.check_cancelled()``.
"""

from __future__ import annotations

import logging
import threading
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.secrets import SecretStore
from app.orchestration.context import StepContext
from app.orchestration.events import ChangeKind, EventPublisher
from app.orchestration.hooks import run_status_hook_new_session
from app.orchestration.models import (
    LogLevel,
    Operation,
    OperationLog,
    OperationStatus,
    OperationStep,
    StepStatus,
)
from app.orchestration.workflow import (
    OperationCancelledError,
    RetryableStepError,
    StepDefinition,
    StepFailedError,
    WorkflowRegistry,
)

logger = logging.getLogger(__name__)

_CLAIMABLE = (OperationStatus.PENDING, OperationStatus.QUEUED)
_EXECUTING = (OperationStatus.RUNNING, OperationStatus.CANCELLING)
_REDACTED = "********"
_MAX_LOG_CHARS = 16_000


def utcnow() -> datetime:
    return datetime.now(UTC)


class _Heartbeat:
    def __init__(self, beat: Callable[[], None], interval: float) -> None:
        self._beat = beat
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="operation-heartbeat", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._beat()
            except Exception:  # pragma: no cover - heartbeat must never kill the worker
                logger.exception("Heartbeat update failed")

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


class OperationExecutor:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        registry: WorkflowRegistry,
        publisher: EventPublisher,
        settings: Settings,
        secret_store: SecretStore,
        worker_id: str,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._sf = session_factory
        self._registry = registry
        self._publisher = publisher
        self._settings = settings
        self._secret_store = secret_store
        self._worker_id = worker_id
        self._stop_sleep = threading.Event()
        self._orgs: dict[uuid.UUID, uuid.UUID] = {}
        self._sleep = sleep or (lambda seconds: self._stop_sleep.wait(seconds))

    # ------------------------------------------------------------------ public

    def execute(self, operation_id: uuid.UUID) -> OperationStatus | None:
        """Run the operation if it can be claimed. Returns the final status, or None if not claimed."""
        organization_id = self._claim(operation_id)
        if organization_id is None:
            logger.info("Operation %s not claimable (already running or finished); skipping", operation_id)
            return None
        self._orgs[operation_id] = organization_id
        run_status_hook_new_session(self._sf, self._registry, operation_id)
        self._publish(operation_id, ChangeKind.STATUS, OperationStatus.RUNNING.value)
        redactions: set[str] = set()
        with _Heartbeat(lambda: self._heartbeat(operation_id), self._settings.operation_heartbeat_seconds):
            try:
                return self._run(operation_id, redactions)
            except Exception as exc:  # engine-level failure (bug, DB loss...)
                logger.exception("Engine failure while executing operation %s", operation_id)
                self._log(operation_id, LogLevel.ERROR, f"Internal engine error: {exc}", None, redactions)
                self._finish(
                    operation_id,
                    OperationStatus.FAILED,
                    error={"code": "engine_error", "message": str(exc), "retryable": True},
                )
                return OperationStatus.FAILED
            finally:
                self._orgs.pop(operation_id, None)

    def _publish(self, operation_id: uuid.UUID, kind: ChangeKind, status: str | None = None) -> None:
        organization_id = self._orgs.get(operation_id)
        if organization_id is not None:
            self._publisher.publish(organization_id, operation_id, kind, status)

    # ------------------------------------------------------------------ core

    def _run(self, operation_id: uuid.UUID, redactions: set[str]) -> OperationStatus:
        with self._sf() as session:
            op = session.get(Operation, operation_id)
            assert op is not None
            workflow = self._registry.get(op.type)
            params = workflow.validate_params(op.params)
            org_id, attempt, recovery = op.organization_id, op.attempt, op.recovery_count
            rows = {s.key: (s.id, s.status, dict(s.output or {})) for s in op.steps}

        plan = workflow.build_plan(params)
        if [s.key for s in plan] != list(rows):
            message = "Workflow plan no longer matches the persisted steps; the workflow changed incompatibly"
            self._log(operation_id, LogLevel.ERROR, message, None, redactions)
            self._finish(operation_id, OperationStatus.FAILED, error={"code": "plan_mismatch", "message": message})
            return OperationStatus.FAILED

        resumed = any(status is StepStatus.SUCCEEDED for _, status, _ in rows.values())
        banner = f"Operation started on worker {self._worker_id} (attempt {attempt}"
        banner += f", recovery {recovery})" if recovery else ")"
        self._log(operation_id, LogLevel.INFO, banner, None, redactions)
        if resumed:
            self._log(operation_id, LogLevel.INFO, "Resuming: completed steps will be skipped", None, redactions)

        outputs: dict[str, dict[str, Any]] = {
            key: output for key, (_, status, output) in rows.items() if status is StepStatus.SUCCEEDED
        }
        total = len(plan)
        final_result: dict[str, Any] | None = None

        for index, step in enumerate(plan):
            step_id, status, _ = rows[step.key]
            if status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED):
                continue
            if self._cancel_requested(operation_id):
                self._mark_step(step_id, StepStatus.CANCELLED, finished=True)
                return self._finish_cancelled(operation_id, redactions)

            outcome, result = self._run_step(
                operation_id, org_id, attempt, step, step_id, params, outputs, redactions, index, total
            )
            if outcome is StepStatus.CANCELLED:
                return self._finish_cancelled(operation_id, redactions)
            if outcome is StepStatus.FAILED:
                return OperationStatus.FAILED
            if result is not None:
                final_result = result
            self._set_progress(operation_id, (index + 1) / total)

        self._log(operation_id, LogLevel.INFO, "Operation completed successfully", None, redactions)
        self._finish(operation_id, OperationStatus.SUCCEEDED, result=final_result or {})
        return OperationStatus.SUCCEEDED

    def _run_step(
        self,
        operation_id: uuid.UUID,
        organization_id: uuid.UUID,
        attempt: int,
        step: StepDefinition,
        step_id: uuid.UUID,
        params: Any,
        outputs: dict[str, dict[str, Any]],
        redactions: set[str],
        index: int,
        total: int,
    ) -> tuple[StepStatus, dict[str, Any] | None]:
        while True:
            step_attempt = self._start_step(operation_id, step_id, step.key)
            suffix = f" (attempt {step_attempt}/{step.max_attempts})" if step.max_attempts > 1 else ""
            self._log(operation_id, LogLevel.INFO, f"▶ {step.name}{suffix}", step.key, redactions)

            ctx = StepContext(
                operation_id=operation_id,
                organization_id=organization_id,
                step_key=step.key,
                attempt=attempt,
                params=params,
                outputs=outputs,
                session_factory=self._sf,
                settings=self._settings,
                secret_store=self._secret_store,
                redactions=redactions,
                log_fn=lambda level, msg, key: self._log(operation_id, level, msg, key, redactions),
                progress_fn=lambda f: self._set_progress(operation_id, (index + f) / total),
            )
            try:
                output = step.fn(ctx) or {}
            except OperationCancelledError:
                self._mark_step(step_id, StepStatus.CANCELLED, finished=True)
                self._log(operation_id, LogLevel.WARNING, f"Step '{step.name}' cancelled", step.key, redactions)
                return StepStatus.CANCELLED, None
            except RetryableStepError as exc:
                message = self._redact(str(exc), redactions)
                if step_attempt < step.max_attempts:
                    delay = step.retry_backoff_seconds * (2 ** (step_attempt - 1))
                    self._log(
                        operation_id,
                        LogLevel.WARNING,
                        f"Transient failure: {message}. Retrying in {delay:.0f}s",
                        step.key,
                        redactions,
                    )
                    self._mark_step(step_id, StepStatus.PENDING)
                    if self._interruptible_sleep(operation_id, delay):
                        self._mark_step(step_id, StepStatus.CANCELLED, finished=True)
                        return StepStatus.CANCELLED, None
                    continue
                self._fail_step(operation_id, step, step_id, "retries_exhausted", message, True, redactions)
                return StepStatus.FAILED, None
            except StepFailedError as exc:
                message = self._redact(str(exc), redactions)
                self._fail_step(operation_id, step, step_id, exc.code, message, False, redactions)
                return StepStatus.FAILED, None
            except Exception as exc:
                message = self._redact(f"{type(exc).__name__}: {exc}", redactions)
                tb = self._redact(traceback.format_exc(), redactions)
                self._log(operation_id, LogLevel.DEBUG, tb, step.key, redactions)
                self._fail_step(operation_id, step, step_id, "unexpected_error", message, True, redactions)
                return StepStatus.FAILED, None

            outputs[step.key] = output
            self._mark_step(step_id, StepStatus.SUCCEEDED, finished=True, output=output)
            self._log(operation_id, LogLevel.INFO, f"✔ {step.name}", step.key, redactions)
            return StepStatus.SUCCEEDED, ctx.result

    # ------------------------------------------------------------------ persistence helpers

    def _claim(self, operation_id: uuid.UUID) -> uuid.UUID | None:
        """Atomically take ownership; returns the organization id when claimed."""
        with self._sf() as session:
            now = utcnow()
            claimed = session.execute(
                update(Operation)
                .where(Operation.id == operation_id, Operation.status.in_(_CLAIMABLE))
                .values(
                    status=OperationStatus.RUNNING,
                    started_at=func.coalesce(Operation.started_at, now),
                    heartbeat_at=now,
                    worker_id=self._worker_id,
                )
                .returning(Operation.organization_id)
            ).scalar_one_or_none()
            session.commit()
        return claimed

    def _heartbeat(self, operation_id: uuid.UUID) -> None:
        with self._sf() as session:
            session.execute(
                update(Operation)
                .where(Operation.id == operation_id, Operation.status.in_(_EXECUTING))
                .values(heartbeat_at=utcnow())
            )
            session.commit()

    def _cancel_requested(self, operation_id: uuid.UUID) -> bool:
        with self._sf() as session:
            return session.scalar(select(Operation.cancel_requested_at).where(Operation.id == operation_id)) is not None

    def _interruptible_sleep(self, operation_id: uuid.UUID, seconds: float) -> bool:
        """Sleep in short slices; returns True if cancellation was requested meanwhile."""
        remaining = seconds
        while remaining > 0:
            if self._cancel_requested(operation_id):
                return True
            chunk = min(1.0, remaining)
            self._sleep(chunk)
            remaining -= chunk
        return self._cancel_requested(operation_id)

    def _start_step(self, operation_id: uuid.UUID, step_id: uuid.UUID, key: str) -> int:
        with self._sf() as session:
            attempts = session.execute(
                update(OperationStep)
                .where(OperationStep.id == step_id)
                .values(
                    status=StepStatus.RUNNING,
                    attempts=OperationStep.attempts + 1,
                    started_at=func.coalesce(OperationStep.started_at, utcnow()),
                    error=None,
                )
                .returning(OperationStep.attempts)
            ).scalar_one()
            session.execute(update(Operation).where(Operation.id == operation_id).values(current_step=key))
            session.commit()
        self._publish(operation_id, ChangeKind.STEP)
        return attempts

    def _mark_step(
        self,
        step_id: uuid.UUID,
        status: StepStatus,
        *,
        finished: bool = False,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if finished:
            values["finished_at"] = utcnow()
        if output is not None:
            values["output"] = output
        if error is not None:
            values["error"] = error
        with self._sf() as session:
            operation_id = session.execute(
                update(OperationStep)
                .where(OperationStep.id == step_id)
                .values(**values)
                .returning(OperationStep.operation_id)
            ).scalar_one()
            session.commit()
        self._publish(operation_id, ChangeKind.STEP)

    def _fail_step(
        self,
        operation_id: uuid.UUID,
        step: StepDefinition,
        step_id: uuid.UUID,
        code: str,
        message: str,
        retryable: bool,
        redactions: set[str],
    ) -> None:
        error = {"code": code, "message": message, "retryable": retryable}
        self._mark_step(step_id, StepStatus.FAILED, finished=True, error=error)
        self._log(operation_id, LogLevel.ERROR, f"✖ {step.name}: {message}", step.key, redactions)
        self._finish(operation_id, OperationStatus.FAILED, error={**error, "step": step.key})

    def _set_progress(self, operation_id: uuid.UUID, fraction: float) -> None:
        with self._sf() as session:
            session.execute(
                update(Operation)
                .where(Operation.id == operation_id)
                .values(progress=round(max(0.0, min(1.0, fraction)) * 100, 1))
            )
            session.commit()
        self._publish(operation_id, ChangeKind.PROGRESS)

    def _finish_cancelled(self, operation_id: uuid.UUID, redactions: set[str]) -> OperationStatus:
        self._log(operation_id, LogLevel.WARNING, "Operation cancelled", None, redactions)
        self._finish(operation_id, OperationStatus.CANCELLED)
        return OperationStatus.CANCELLED

    def _finish(
        self,
        operation_id: uuid.UUID,
        status: OperationStatus,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "finished_at": utcnow(),
            "current_step": None,
            "error": error,
        }
        if status is OperationStatus.SUCCEEDED:
            values["progress"] = 100.0
            values["result"] = result
        with self._sf() as session:
            session.execute(
                update(Operation).where(Operation.id == operation_id, Operation.status.in_(_EXECUTING)).values(**values)
            )
            session.commit()
        run_status_hook_new_session(self._sf, self._registry, operation_id)
        self._publish(operation_id, ChangeKind.STATUS, status.value)

    def _log(
        self,
        operation_id: uuid.UUID,
        level: LogLevel,
        message: str,
        step_key: str | None,
        redactions: set[str],
    ) -> None:
        message = self._redact(message, redactions)
        if len(message) > _MAX_LOG_CHARS:
            message = message[:_MAX_LOG_CHARS] + "… [truncated]"
        logger.log(logging.getLevelNamesMapping()[level.value.upper()], "[op %s] %s", operation_id, message)
        with self._sf() as session:
            session.add(OperationLog(operation_id=operation_id, level=level, step_key=step_key, message=message))
            session.commit()
        self._publish(operation_id, ChangeKind.LOG)

    @staticmethod
    def _redact(text: str, redactions: set[str]) -> str:
        for secret in sorted(redactions, key=len, reverse=True):
            text = text.replace(secret, _REDACTED)
        return text

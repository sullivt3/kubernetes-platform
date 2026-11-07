"""Orchestration engine behaviour: execution, checkpoints, retries, cancellation,
mutual exclusion, idempotency, redaction and crash recovery."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConfirmationRequiredError, ConflictError, InvalidStateError, ValidationFailedError
from app.core.principal import Principal
from app.db.session import get_session_factory
from app.modules.auditing.models import AuditEvent
from app.orchestration.context import StepContext
from app.orchestration.dispatch import InlineDispatcher
from app.orchestration.events import ChangeKind, RecordingEventPublisher
from app.orchestration.executor import OperationExecutor, utcnow
from app.orchestration.models import (
    LogLevel,
    Operation,
    OperationLog,
    OperationStatus,
    OperationStep,
    StepStatus,
)
from app.orchestration.service import OperationService, recover_operations
from app.orchestration.workflow import (
    OperationTarget,
    RetryableStepError,
    StepDefinition,
    StepFailedError,
    WorkflowDefinition,
    workflow_registry,
)
from tests.conftest import RecordingDispatcher

CALLS: Counter[str] = Counter()
BEHAVIOUR: dict[str, Any] = {}


class Params(BaseModel):
    target: str = "alpha"
    fail_step: str | None = None
    transient_failures: int = 0
    secret: str | None = None


def _step(name: str):  # type: ignore[no-untyped-def]
    def run(ctx: StepContext) -> dict[str, Any]:
        CALLS[name] += 1
        p: Params = ctx.params  # type: ignore[assignment]
        if name == "two" and CALLS[name] <= p.transient_failures:
            raise RetryableStepError("temporary glitch")
        if p.fail_step == name and not BEHAVIOUR.get("heal"):
            raise StepFailedError(f"{name} failed deliberately", code="deliberate")
        if name == "two" and BEHAVIOUR.get("explode"):
            raise RuntimeError("boom")
        if name == "two" and p.secret:
            ctx.redact(p.secret)
            ctx.info(f"connecting with password {p.secret}")
        if name == "two" and BEHAVIOUR.get("cancel_during"):
            with get_session_factory()() as s:
                s.execute(
                    update(Operation)
                    .where(Operation.id == ctx.operation_id)
                    .values(status=OperationStatus.CANCELLING, cancel_requested_at=utcnow())
                )
                s.commit()
            ctx.check_cancelled()
        if name == "three":
            ctx.set_result({"seen": ctx.outputs["one"]["value"]})
        return {"value": f"{name}-done"}

    return run


TEST_WORKFLOW = "test.three_steps"
DESTRUCTIVE_WORKFLOW = "test.destroy"


@pytest.fixture(autouse=True)
def test_workflows() -> Iterator[None]:
    CALLS.clear()
    BEHAVIOUR.clear()
    workflow_registry.register(
        WorkflowDefinition(
            type=TEST_WORKFLOW,
            title="Three steps",
            description="test",
            params_model=Params,
            plan=lambda p: [
                StepDefinition("one", "Step one", _step("one")),
                StepDefinition("two", "Step two", _step("two"), max_attempts=3, retry_backoff_seconds=1),
                StepDefinition("three", "Step three", _step("three")),
            ],
            target=lambda p: OperationTarget(type="thing", id=p.target, name=p.target),
            title_for=lambda p: f"Three steps on {p.target}",
        )
    )
    workflow_registry.register(
        WorkflowDefinition(
            type=DESTRUCTIVE_WORKFLOW,
            title="Destroy",
            description="test",
            params_model=Params,
            plan=lambda p: [StepDefinition("one", "Destroy", _step("one"))],
            target=lambda p: OperationTarget(type="thing", id=p.target, name=p.target),
            destructive=True,
        )
    )
    yield
    workflow_registry.unregister(TEST_WORKFLOW)
    workflow_registry.unregister(DESTRUCTIVE_WORKFLOW)


def logs_of(session: Session, op_id: uuid.UUID) -> list[OperationLog]:
    return list(
        session.scalars(
            select(OperationLog).where(OperationLog.operation_id == op_id).order_by(OperationLog.id)
        )
    )


def steps_of(op: Operation) -> dict[str, StepStatus]:
    return {s.key: s.status for s in op.steps}


# ---------------------------------------------------------------------------- happy path


def test_successful_operation(
    inline_service: OperationService,
    principal: Principal,
    session: Session,
    publisher: RecordingEventPublisher,
) -> None:
    op = inline_service.submit(principal, TEST_WORKFLOW, {"target": "alpha"})

    assert op.status is OperationStatus.SUCCEEDED
    assert op.title == "Three steps on alpha"
    assert op.progress == 100.0
    assert op.result == {"seen": "one-done"}
    assert steps_of(op) == {"one": "succeeded", "two": "succeeded", "three": "succeeded"}
    assert op.steps[0].output == {"value": "one-done"}
    assert op.started_at and op.finished_at and op.worker_id == "test-worker"
    assert op.concurrency_key is None or op.concurrency_key == "thing:alpha"

    messages = [log.message for log in logs_of(session, op.id)]
    assert messages[0].startswith("Operation requested by")
    assert any("Step two" in m for m in messages)
    assert messages[-1] == "Operation completed successfully"

    kinds = {kind for _, op_id, kind, _ in publisher.events if op_id == op.id}
    assert {ChangeKind.STATUS, ChangeKind.STEP, ChangeKind.LOG, ChangeKind.PROGRESS} <= kinds
    assert all(org == principal.organization_id for org, *_ in publisher.events)

    audit = session.scalars(select(AuditEvent).where(AuditEvent.action == "operation.submit")).all()
    assert len(audit) == 1 and audit[0].resource_id == str(op.id)


def test_invalid_params_rejected(inline_service: OperationService, principal: Principal) -> None:
    with pytest.raises(ValidationFailedError):
        inline_service.submit(principal, TEST_WORKFLOW, {"transient_failures": "lots"})
    with pytest.raises(ValidationFailedError):
        inline_service.submit(principal, "does.not.exist", {})


# ---------------------------------------------------------------------------- failures & retries


def test_transient_failure_is_retried(inline_service: OperationService, principal: Principal) -> None:
    op = inline_service.submit(principal, TEST_WORKFLOW, {"transient_failures": 2})
    assert op.status is OperationStatus.SUCCEEDED
    assert CALLS["two"] == 3
    assert next(s for s in op.steps if s.key == "two").attempts == 3


def test_retries_exhausted(inline_service: OperationService, principal: Principal) -> None:
    op = inline_service.submit(principal, TEST_WORKFLOW, {"transient_failures": 5})
    assert op.status is OperationStatus.FAILED
    assert op.error and op.error["code"] == "retries_exhausted" and op.error["step"] == "two"
    assert steps_of(op) == {"one": "succeeded", "two": "failed", "three": "pending"}
    assert CALLS["three"] == 0


def test_failed_operation_resumes_from_failed_step_on_retry(
    inline_service: OperationService, principal: Principal, session: Session
) -> None:
    op = inline_service.submit(principal, TEST_WORKFLOW, {"fail_step": "two"})
    assert op.status is OperationStatus.FAILED
    assert op.error == {
        "code": "deliberate",
        "message": "two failed deliberately",
        "retryable": False,
        "step": "two",
    }

    BEHAVIOUR["heal"] = True
    retried = inline_service.retry(principal, op.id)

    assert retried.status is OperationStatus.SUCCEEDED
    assert retried.attempt == 2
    assert CALLS["one"] == 1, "completed steps must not run again"
    assert CALLS["two"] == 2
    assert any("1 completed step(s) will be skipped" in log.message for log in logs_of(session, op.id))


def test_unexpected_exception_is_captured(
    inline_service: OperationService, principal: Principal, session: Session
) -> None:
    BEHAVIOUR["explode"] = True
    op = inline_service.submit(principal, TEST_WORKFLOW, {})
    assert op.status is OperationStatus.FAILED
    assert op.error and op.error["code"] == "unexpected_error" and "RuntimeError: boom" in op.error["message"]
    debug = [log for log in logs_of(session, op.id) if log.level is LogLevel.DEBUG]
    assert debug and "Traceback" in debug[0].message


def test_only_failed_or_cancelled_can_be_retried(
    inline_service: OperationService, principal: Principal
) -> None:
    op = inline_service.submit(principal, TEST_WORKFLOW, {})
    with pytest.raises(InvalidStateError):
        inline_service.retry(principal, op.id)


# ---------------------------------------------------------------------------- cancellation


def test_cancel_before_execution(
    deferred_service: OperationService, principal: Principal, executor: OperationExecutor
) -> None:
    op = deferred_service.submit(principal, TEST_WORKFLOW, {})
    assert op.status is OperationStatus.QUEUED

    cancelled = deferred_service.cancel(principal, op.id)
    assert cancelled.status is OperationStatus.CANCELLED
    assert executor.execute(op.id) is None, "cancelled operations must not be claimed"
    assert CALLS["one"] == 0
    with pytest.raises(InvalidStateError):
        deferred_service.cancel(principal, op.id)


def test_cancel_during_execution(inline_service: OperationService, principal: Principal) -> None:
    BEHAVIOUR["cancel_during"] = True
    op = inline_service.submit(principal, TEST_WORKFLOW, {})
    assert op.status is OperationStatus.CANCELLED
    assert steps_of(op) == {"one": "succeeded", "two": "cancelled", "three": "pending"}
    assert CALLS["three"] == 0


def test_cancelled_operation_can_be_retried(
    deferred_service: OperationService, principal: Principal, executor: OperationExecutor, session: Session
) -> None:
    op = deferred_service.submit(principal, TEST_WORKFLOW, {})
    deferred_service.cancel(principal, op.id)
    deferred_service.retry(principal, op.id)
    assert executor.execute(op.id) is OperationStatus.SUCCEEDED


# ---------------------------------------------------------------------------- exclusivity & idempotency


def test_single_claim(
    deferred_service: OperationService, principal: Principal, executor: OperationExecutor
) -> None:
    op = deferred_service.submit(principal, TEST_WORKFLOW, {})
    assert executor.execute(op.id) is OperationStatus.SUCCEEDED
    assert executor.execute(op.id) is None  # duplicate delivery is a no-op
    assert CALLS["one"] == 1


def test_one_active_operation_per_target(deferred_service: OperationService, principal: Principal) -> None:
    deferred_service.submit(principal, TEST_WORKFLOW, {"target": "alpha"})
    with pytest.raises(ConflictError, match="already in progress for alpha"):
        deferred_service.submit(principal, TEST_WORKFLOW, {"target": "alpha"})
    # A different target is unaffected.
    assert (
        deferred_service.submit(principal, TEST_WORKFLOW, {"target": "beta"}).status is OperationStatus.QUEUED
    )


def test_target_is_free_again_after_completion(
    inline_service: OperationService, principal: Principal
) -> None:
    inline_service.submit(principal, TEST_WORKFLOW, {"target": "alpha"})
    assert (
        inline_service.submit(principal, TEST_WORKFLOW, {"target": "alpha"}).status
        is OperationStatus.SUCCEEDED
    )


def test_idempotency_key_returns_existing_operation(
    deferred_service: OperationService, principal: Principal, recording_dispatcher: RecordingDispatcher
) -> None:
    first = deferred_service.submit(principal, TEST_WORKFLOW, {}, idempotency_key="abc")
    second = deferred_service.submit(principal, TEST_WORKFLOW, {}, idempotency_key="abc")
    assert first.id == second.id
    assert recording_dispatcher.dispatched == [first.id]
    with pytest.raises(ConflictError):
        deferred_service.submit(principal, DESTRUCTIVE_WORKFLOW, {}, idempotency_key="abc", confirm="alpha")


def test_destructive_operations_require_confirmation(
    inline_service: OperationService, principal: Principal
) -> None:
    with pytest.raises(ConfirmationRequiredError) as exc_info:
        inline_service.submit(principal, DESTRUCTIVE_WORKFLOW, {"target": "alpha"})
    assert exc_info.value.details == {"expected_confirmation": "alpha"}
    with pytest.raises(ConfirmationRequiredError):
        inline_service.submit(principal, DESTRUCTIVE_WORKFLOW, {"target": "alpha"}, confirm="wrong")
    op = inline_service.submit(principal, DESTRUCTIVE_WORKFLOW, {"target": "alpha"}, confirm="alpha")
    assert op.status is OperationStatus.SUCCEEDED


# ---------------------------------------------------------------------------- observability


def test_registered_secrets_are_redacted_from_logs(
    inline_service: OperationService, principal: Principal, session: Session
) -> None:
    op = inline_service.submit(principal, TEST_WORKFLOW, {"secret": "hunter2-pass"})
    text = "\n".join(log.message for log in logs_of(session, op.id))
    assert "hunter2-pass" not in text
    assert "connecting with password ********" in text


def test_tenancy_isolation(
    deferred_service: OperationService, principal: Principal, session: Session
) -> None:
    from app.core.errors import NotFoundError
    from app.modules.organizations.models import Organization

    op = deferred_service.submit(principal, TEST_WORKFLOW, {})
    other_org = Organization(slug="other", name="Other")
    session.add(other_org)
    session.commit()
    outsider = Principal(user_id=None, subject="x", display_name="x", organization_id=other_org.id)
    with pytest.raises(NotFoundError):
        deferred_service.get(outsider, op.id)
    assert deferred_service.list_operations(outsider) == ([], 0)


# ---------------------------------------------------------------------------- dispatch & recovery


def test_dispatch_failure_leaves_operation_pending(
    session: Session, principal: Principal, publisher: RecordingEventPublisher
) -> None:
    service = OperationService(
        session, registry=workflow_registry, dispatcher=RecordingDispatcher(fail=True), publisher=publisher
    )
    op = service.submit(principal, TEST_WORKFLOW, {})
    assert op.status is OperationStatus.PENDING
    assert any("dispatched automatically" in log.message for log in logs_of(session, op.id))


def _recover(dispatcher: Any, publisher: RecordingEventPublisher, now: Any = None) -> dict[str, list[str]]:
    return recover_operations(
        get_session_factory(),
        registry=workflow_registry,
        dispatcher=dispatcher,
        publisher=publisher,
        settings=get_settings(),
        now=now,
    ).as_dict()


def _simulate_crash(session: Session, op_id: uuid.UUID, *, cancelling: bool = False) -> None:
    """Make the operation look like its worker died in the middle of step 'two'."""
    stale = utcnow() - timedelta(hours=1)
    session.execute(
        update(Operation)
        .where(Operation.id == op_id)
        .values(
            status=OperationStatus.CANCELLING if cancelling else OperationStatus.RUNNING,
            heartbeat_at=stale,
            worker_id="dead-worker",
        )
    )
    session.execute(
        update(OperationStep)
        .where(OperationStep.operation_id == op_id, OperationStep.key == "one")
        .values(status=StepStatus.SUCCEEDED, output={"value": "one-done"})
    )
    session.execute(
        update(OperationStep)
        .where(OperationStep.operation_id == op_id, OperationStep.key == "two")
        .values(status=StepStatus.RUNNING, attempts=1)
    )
    session.commit()


def test_crashed_operation_is_resumed(
    deferred_service: OperationService,
    principal: Principal,
    session: Session,
    executor: OperationExecutor,
    publisher: RecordingEventPublisher,
) -> None:
    op = deferred_service.submit(principal, TEST_WORKFLOW, {})
    _simulate_crash(session, op.id)

    report = _recover(InlineDispatcher(executor.execute), publisher)

    assert report["resumed"] == [str(op.id)]
    final = deferred_service.get(principal, op.id)
    assert final.status is OperationStatus.SUCCEEDED
    assert final.recovery_count == 1
    assert final.result == {"seen": "one-done"}
    assert CALLS["one"] == 0, "the completed step must not be re-run after a crash"
    assert CALLS["two"] == 1
    assert any("stopped responding" in log.message for log in logs_of(session, op.id))


def test_repeated_crashes_eventually_fail(
    deferred_service: OperationService,
    principal: Principal,
    session: Session,
    publisher: RecordingEventPublisher,
) -> None:
    op = deferred_service.submit(principal, TEST_WORKFLOW, {})
    _simulate_crash(session, op.id)
    session.execute(
        update(Operation)
        .where(Operation.id == op.id)
        .values(recovery_count=get_settings().operation_max_recoveries)
    )
    session.commit()

    report = _recover(RecordingDispatcher(), publisher)

    assert report["failed"] == [str(op.id)]
    final = deferred_service.get(principal, op.id)
    assert final.status is OperationStatus.FAILED
    assert final.error and final.error["code"] == "worker_lost"


def test_crashed_cancelling_operation_becomes_cancelled(
    deferred_service: OperationService,
    principal: Principal,
    session: Session,
    publisher: RecordingEventPublisher,
) -> None:
    op = deferred_service.submit(principal, TEST_WORKFLOW, {})
    _simulate_crash(session, op.id, cancelling=True)
    assert _recover(RecordingDispatcher(), publisher)["cancelled"] == [str(op.id)]
    assert deferred_service.get(principal, op.id).status is OperationStatus.CANCELLED


def test_stuck_pending_and_queued_operations_are_redispatched(
    session: Session, principal: Principal, publisher: RecordingEventPublisher
) -> None:
    failing = OperationService(
        session, registry=workflow_registry, dispatcher=RecordingDispatcher(fail=True), publisher=publisher
    )
    pending = failing.submit(principal, TEST_WORKFLOW, {"target": "p"})
    working_dispatcher = RecordingDispatcher()
    queued = OperationService(
        session, registry=workflow_registry, dispatcher=working_dispatcher, publisher=publisher
    ).submit(principal, TEST_WORKFLOW, {"target": "q"})

    # Nothing is old enough yet.
    assert _recover(working_dispatcher, publisher)["redispatched"] == []

    later = utcnow() + timedelta(hours=1)
    report = _recover(working_dispatcher, publisher, now=later)
    assert sorted(report["redispatched"]) == sorted([str(pending.id), str(queued.id)])
    session.expire_all()
    assert session.get(Operation, pending.id).status is OperationStatus.QUEUED  # type: ignore[union-attr]


def test_healthy_running_operation_is_left_alone(
    deferred_service: OperationService,
    principal: Principal,
    session: Session,
    publisher: RecordingEventPublisher,
) -> None:
    op = deferred_service.submit(principal, TEST_WORKFLOW, {})
    session.execute(
        update(Operation)
        .where(Operation.id == op.id)
        .values(status=OperationStatus.RUNNING, heartbeat_at=utcnow())
    )
    session.commit()
    report = _recover(RecordingDispatcher(), publisher)
    assert report == {"resumed": [], "failed": [], "cancelled": [], "redispatched": []}

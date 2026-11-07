"""Workflow status hooks.

A workflow may declare ``on_status(session, operation)``. It is invoked whenever the
operation starts running or reaches a terminal status — from the executor, from
direct cancellation and from crash recovery — so domain records (a cluster's
lifecycle state, a machine's readiness) always follow the operation.

Hooks run in their own transaction and are best-effort: a failing hook is logged
and never changes the operation's outcome.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session, sessionmaker

from app.orchestration.models import Operation
from app.orchestration.workflow import WorkflowRegistry

logger = logging.getLogger(__name__)


def run_status_hook(session: Session, registry: WorkflowRegistry, operation_id: uuid.UUID) -> None:
    try:
        op = session.get(Operation, operation_id, populate_existing=True)
        if op is None:
            return
        try:
            workflow = registry.get(op.type)
        except Exception:
            return
        if workflow.on_status is None:
            return
        workflow.on_status(session, op)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Status hook failed for operation %s", operation_id)


def run_status_hook_new_session(
    session_factory: sessionmaker[Session], registry: WorkflowRegistry, operation_id: uuid.UUID
) -> None:
    with session_factory() as session:
        run_status_hook(session, registry, operation_id)

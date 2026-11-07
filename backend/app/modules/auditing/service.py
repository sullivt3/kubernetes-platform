"""Audit recording and querying.

``record`` adds the audit row to the caller's session, so the audit entry commits
or rolls back atomically with the change it describes.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import request_id_var
from app.core.principal import Principal
from app.modules.auditing.models import AuditEvent, AuditOutcome

client_ip_var: ContextVar[str | None] = ContextVar("client_ip", default=None)

_SENSITIVE_KEYS = (
    "password",
    "secret",
    "token",
    "private_key",
    "kubeconfig",
    "credential_value",
    "key_material",
)


def _scrub(details: dict[str, Any]) -> dict[str, Any]:
    """Defence in depth: drop values whose keys look secret, even if a caller slips."""
    clean: dict[str, Any] = {}
    for key, value in details.items():
        if any(marker in key.lower() for marker in _SENSITIVE_KEYS):
            clean[key] = "[redacted]"
        elif isinstance(value, dict):
            clean[key] = _scrub(value)
        else:
            clean[key] = value
    return clean


def record(
    session: Session,
    *,
    principal: Principal,
    action: str,
    resource_type: str,
    resource_id: str | uuid.UUID | None = None,
    resource_name: str | None = None,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        organization_id=principal.organization_id,
        actor_id=principal.user_id,
        actor_subject=principal.subject,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        resource_name=resource_name,
        outcome=outcome,
        request_id=request_id_var.get(),
        client_ip=client_ip_var.get(),
        details=_scrub(details or {}),
    )
    session.add(event)
    return event


def list_events(
    session: Session,
    *,
    organization_id: uuid.UUID,
    resource_type: str | None = None,
    resource_id: str | None = None,
    action: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditEvent], int]:
    query = select(AuditEvent).where(AuditEvent.organization_id == organization_id)
    if resource_type:
        query = query.where(AuditEvent.resource_type == resource_type)
    if resource_id:
        query = query.where(AuditEvent.resource_id == resource_id)
    if action:
        query = query.where(AuditEvent.action == action)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = session.scalars(query.order_by(AuditEvent.id.desc()).limit(limit).offset(offset)).all()
    return list(rows), total

"""Audit log API (read-only)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import get_principal
from app.api.deps import get_db
from app.api.schemas import ApiModel, Page
from app.core.principal import Principal
from app.modules.auditing import service as audit

router = APIRouter(prefix="/audit-events", tags=["audit"])


class AuditEventOut(ApiModel):
    id: int
    occurred_at: datetime
    actor_id: uuid.UUID | None
    actor_subject: str
    action: str
    resource_type: str
    resource_id: str | None
    resource_name: str | None
    outcome: str
    request_id: str | None
    client_ip: str | None
    details: dict[str, Any]


@router.get("", response_model=Page[AuditEventOut])
def list_audit_events(
    resource_type: str | None = None,
    resource_id: str | None = None,
    action: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_db),
) -> Page[AuditEventOut]:
    rows, total = audit.list_events(
        session,
        organization_id=principal.organization_id,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        limit=limit,
        offset=offset,
    )
    return Page(items=[AuditEventOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)

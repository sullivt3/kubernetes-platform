"""Platform information and self-check."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from app.api.auth import get_principal
from app.api.deps import get_operation_service
from app.api.routes.operations import OperationDetail
from app.core.config import get_settings
from app.core.principal import Principal
from app.core.secrets import FernetSecretStore, get_secret_store
from app.orchestration.service import OperationService
from app.orchestration.workflows.system import SELF_CHECK

router = APIRouter(prefix="/system", tags=["system"])


class CurrentUser(BaseModel):
    subject: str
    display_name: str
    roles: list[str]


class SystemInfo(BaseModel):
    app_name: str
    version: str
    environment: str
    auth_mode: str
    orchestration_mode: str
    secret_backend: str
    secret_primary_key_id: str | None
    current_user: CurrentUser


@router.get("/info", response_model=SystemInfo)
def system_info(principal: Principal = Depends(get_principal)) -> SystemInfo:
    settings = get_settings()
    store = get_secret_store()
    return SystemInfo(
        app_name=settings.app_name,
        version=settings.version,
        environment=settings.environment.value,
        auth_mode=settings.auth_mode.value,
        orchestration_mode="inline" if settings.orchestration_inline else "celery",
        secret_backend=store.backend_name,
        secret_primary_key_id=store.primary_key_id if isinstance(store, FernetSecretStore) else None,
        current_user=CurrentUser(
            subject=principal.subject, display_name=principal.display_name, roles=sorted(principal.roles)
        ),
    )


@router.post("/self-check", response_model=OperationDetail, status_code=202)
def run_self_check(
    idempotency_key: str | None = Header(default=None, max_length=255),
    principal: Principal = Depends(get_principal),
    service: OperationService = Depends(get_operation_service),
) -> OperationDetail:
    """Start the platform self-check workflow. Only one may run at a time (409 otherwise)."""
    op = service.submit(principal, SELF_CHECK, {}, idempotency_key=idempotency_key)
    return OperationDetail.model_validate(op)

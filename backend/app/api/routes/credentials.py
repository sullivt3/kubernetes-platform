"""Credentials API. Secret values are write-only: no endpoint ever returns them."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field, SecretStr

from app.api.auth import get_principal
from app.api.deps import get_credential_service
from app.api.schemas import ApiModel, Page
from app.core.principal import Principal
from app.modules.credentials.models import CredentialKind
from app.modules.credentials.service import CredentialService

router = APIRouter(prefix="/credentials", tags=["credentials"])

NAME_PATTERN = r"^[a-z0-9]([-a-z0-9_.]{0,126}[a-z0-9])?$"


class CredentialCreate(BaseModel):
    name: str = Field(pattern=NAME_PATTERN, description="Lowercase identifier, unique per organization")
    kind: CredentialKind
    secret_value: SecretStr = Field(description="Secret material. Encrypted at rest and never returned.")
    description: str | None = Field(default=None, max_length=1000)
    public_metadata: dict[str, str] = Field(
        default_factory=dict, description="Non-secret descriptive values (e.g. username, access key ID)"
    )
    project_id: uuid.UUID | None = None


class CredentialSecretReplace(BaseModel):
    secret_value: SecretStr


class CredentialOut(ApiModel):
    id: uuid.UUID
    name: str
    kind: CredentialKind
    description: str | None
    public_metadata: dict[str, str]
    secret_backend: str
    key_id: str
    project_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    rotated_at: datetime | None
    last_used_at: datetime | None


class CredentialDetail(CredentialOut):
    used_by: list[str]


@router.post("", response_model=CredentialOut, status_code=201)
def create_credential(
    body: CredentialCreate,
    principal: Principal = Depends(get_principal),
    service: CredentialService = Depends(get_credential_service),
) -> CredentialOut:
    credential = service.create(
        principal,
        name=body.name,
        kind=body.kind,
        secret_value=body.secret_value.get_secret_value(),
        description=body.description,
        public_metadata=body.public_metadata,
        project_id=body.project_id,
    )
    return CredentialOut.model_validate(credential)


@router.get("", response_model=Page[CredentialOut])
def list_credentials(
    kind: CredentialKind | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    service: CredentialService = Depends(get_credential_service),
) -> Page[CredentialOut]:
    rows, total = service.list_credentials(principal, kind=kind, limit=limit, offset=offset)
    return Page(
        items=[CredentialOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.get("/{credential_id}", response_model=CredentialDetail)
def get_credential(
    credential_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: CredentialService = Depends(get_credential_service),
) -> CredentialDetail:
    credential = service.get(principal, credential_id)
    base = CredentialOut.model_validate(credential).model_dump()
    return CredentialDetail(**base, used_by=service.usages(credential.id))


@router.put("/{credential_id}/secret", response_model=CredentialOut)
def replace_secret(
    credential_id: uuid.UUID,
    body: CredentialSecretReplace,
    principal: Principal = Depends(get_principal),
    service: CredentialService = Depends(get_credential_service),
) -> CredentialOut:
    """Rotate the secret material. The new value is re-encrypted with the current primary key."""
    return CredentialOut.model_validate(
        service.replace_secret(principal, credential_id, body.secret_value.get_secret_value())
    )


@router.delete("/{credential_id}", status_code=204, response_class=Response)
def delete_credential(
    credential_id: uuid.UUID,
    confirm: str | None = Query(default=None, description="Must equal the credential name"),
    principal: Principal = Depends(get_principal),
    service: CredentialService = Depends(get_credential_service),
) -> Response:
    service.delete(principal, credential_id, confirm=confirm)
    return Response(status_code=204)

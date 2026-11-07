"""Machine inventory API (existing Linux servers managed over SSH)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app.api.auth import get_principal
from app.api.deps import get_machine_service
from app.api.routes.operations import OperationDetail
from app.api.schemas import ApiModel
from app.core.principal import Principal
from app.modules.infrastructure.models import Machine
from app.modules.infrastructure.service import MachineService

router = APIRouter(prefix="/machines", tags=["machines"])


class MachineRegister(BaseModel):
    name: str = Field(description="Lowercase DNS label; becomes the Kubernetes node name")
    host: str = Field(description="Hostname or IP address reachable from the platform workers")
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=64)
    credential_id: uuid.UUID
    labels: dict[str, str] = Field(default_factory=dict)


class MachineInspect(BaseModel):
    accept_new_host_key: bool = False


class MachineOut(ApiModel):
    id: uuid.UUID
    name: str
    lifecycle_state: str
    ssh_host: str | None
    ssh_port: int
    ssh_username: str | None
    access_credential_id: uuid.UUID | None
    host_key_fingerprint: str | None
    addresses: list[dict[str, Any]]
    os_info: dict[str, Any]
    hardware: dict[str, Any]
    checks: list[dict[str, Any]]
    labels: dict[str, str]
    last_error: str | None
    inspected_at: datetime | None
    created_at: datetime
    cluster_name: str | None = None
    cluster_role: str | None = None


class MachineRegistered(BaseModel):
    machine: MachineOut
    operation: OperationDetail


def _out(machine: Machine, cluster: str | None, role: str | None) -> MachineOut:
    out = MachineOut.model_validate(machine)
    out.cluster_name, out.cluster_role = cluster, role
    return out


@router.get("", response_model=list[MachineOut])
def list_machines(
    principal: Principal = Depends(get_principal), service: MachineService = Depends(get_machine_service)
) -> list[MachineOut]:
    return [_out(m, c, r) for m, c, r in service.list_machines(principal)]


@router.post("", response_model=MachineRegistered, status_code=201)
def register_machine(
    body: MachineRegister,
    principal: Principal = Depends(get_principal),
    service: MachineService = Depends(get_machine_service),
) -> MachineRegistered:
    """Register an existing server and start inspecting it (connectivity, host key, OS, hardware)."""
    machine, operation = service.register(
        principal,
        name=body.name,
        host=body.host,
        port=body.port,
        username=body.username,
        credential_id=body.credential_id,
        labels=body.labels,
    )
    return MachineRegistered(
        machine=_out(machine, None, None), operation=OperationDetail.model_validate(operation)
    )


@router.get("/{machine_id}", response_model=MachineOut)
def get_machine(
    machine_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: MachineService = Depends(get_machine_service),
) -> MachineOut:
    machine = service.get(principal, machine_id)
    return _out(machine, *service.assignment(machine.id))


@router.post("/{machine_id}/inspect", response_model=OperationDetail, status_code=202)
def inspect_machine(
    machine_id: uuid.UUID,
    body: MachineInspect,
    confirm: str | None = Query(
        default=None, description="Machine name; required when accepting a new host key"
    ),
    principal: Principal = Depends(get_principal),
    service: MachineService = Depends(get_machine_service),
) -> OperationDetail:
    operation = service.inspect(
        principal, machine_id, accept_new_host_key=body.accept_new_host_key, confirm=confirm
    )
    return OperationDetail.model_validate(operation)


@router.delete("/{machine_id}", status_code=204, response_class=Response)
def delete_machine(
    machine_id: uuid.UUID,
    confirm: str | None = Query(default=None, description="Must equal the machine name"),
    principal: Principal = Depends(get_principal),
    service: MachineService = Depends(get_machine_service),
) -> Response:
    """Remove the machine from the inventory. Nothing is changed on the server itself."""
    service.delete(principal, machine_id, confirm=confirm)
    return Response(status_code=204)

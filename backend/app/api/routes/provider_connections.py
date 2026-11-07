"""Provider connections (Proxmox, local Docker, clouds), provisioning and network discovery."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app.api.auth import get_principal
from app.api.deps import get_connection_service
from app.api.routes.machines import MachineOut
from app.api.routes.operations import OperationDetail
from app.api.schemas import ApiModel
from app.core.principal import Principal
from app.modules.infrastructure.connections import ProviderConnectionService
from app.modules.infrastructure.models import ProviderConnection
from app.modules.providers.registry import provider_registry
from app.modules.providers.types import ProviderType

router = APIRouter(tags=["provider connections"])

SSH_USER = r"^[a-z_][a-z0-9_-]{0,31}$"


class ConnectionOut(ApiModel):
    id: uuid.UUID
    name: str
    provider_type: ProviderType
    provider_name: str = ""
    category: str = ""
    implementation_status: str = ""
    capabilities: list[str] = Field(default_factory=list)
    config: dict[str, Any]
    credential_id: uuid.UUID | None
    status: str
    status_message: str | None
    last_validated_at: datetime | None
    created_at: datetime
    machine_count: int = 0


def _out(connection: ProviderConnection, machines: int) -> ConnectionOut:
    out = ConnectionOut.model_validate(connection)
    cls = provider_registry.get_class(connection.provider_type)
    out.provider_name = cls.display_name
    out.category = cls.category.value
    out.implementation_status = cls.implementation_status.value
    out.capabilities = sorted(c.value for c in cls.capabilities)
    out.machine_count = machines
    return out


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    provider_type: ProviderType
    config: dict[str, Any] = Field(default_factory=dict)
    credential_id: uuid.UUID | None = None


class ConnectionUpdate(BaseModel):
    config: dict[str, Any] | None = None
    credential_id: uuid.UUID | None = None
    clear_credential: bool = False


class ConnectionWithOperation(BaseModel):
    connection: ConnectionOut
    operation: OperationDetail


class ProvisionRequest(BaseModel):
    names: list[str] = Field(min_length=1, max_length=20, description="One machine per name")
    cpu_cores: int = Field(ge=1, le=512)
    memory_mb: int = Field(ge=512, le=4_194_304)
    disk_gb: int = Field(ge=10, le=65_536)
    credential_id: uuid.UUID = Field(description="SSH private key credential; its public key is installed")
    ssh_username: str = Field(default="platform", pattern=SSH_USER)
    network: str | None = Field(default=None, max_length=128)
    labels: dict[str, str] = Field(default_factory=dict)
    provider_options: dict[str, Any] = Field(default_factory=dict)


class ProvisionedMachine(BaseModel):
    machine: MachineOut
    operation: OperationDetail


class ImportRequest(BaseModel):
    external_id: str = Field(min_length=1, max_length=255)
    name: str
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = Field(default=None, pattern=SSH_USER)
    credential_id: uuid.UUID
    kube_api_host: str | None = None
    kube_api_port: int | None = Field(default=None, ge=1, le=65535)


class NetworkScanRequest(BaseModel):
    ranges: list[str] = Field(min_length=1, max_length=32, description="CIDRs, single IPs or 10.0.0.10-20 ranges")
    ports: list[int] = Field(default_factory=lambda: [22], min_length=1, max_length=8)
    timeout_seconds: float = Field(default=1.5, ge=0.2, le=10)


@router.get("/provider-connections", response_model=list[ConnectionOut])
def list_connections(
    principal: Principal = Depends(get_principal),
    service: ProviderConnectionService = Depends(get_connection_service),
) -> list[ConnectionOut]:
    return [_out(c, n) for c, n in service.list_connections(principal)]


@router.post("/provider-connections", response_model=ConnectionWithOperation, status_code=201)
def create_connection(
    body: ConnectionCreate,
    principal: Principal = Depends(get_principal),
    service: ProviderConnectionService = Depends(get_connection_service),
) -> ConnectionWithOperation:
    """Create a connection and start validating it (`provider.validate`)."""
    connection, operation = service.create(
        principal,
        name=body.name,
        provider_type=body.provider_type,
        config=body.config,
        credential_id=body.credential_id,
    )
    return ConnectionWithOperation(connection=_out(connection, 0), operation=OperationDetail.model_validate(operation))


@router.get("/provider-connections/{connection_id}", response_model=ConnectionOut)
def get_connection(
    connection_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: ProviderConnectionService = Depends(get_connection_service),
) -> ConnectionOut:
    connection = service.get(principal, connection_id)
    return _out(connection, service.machine_count(connection.id))


@router.put("/provider-connections/{connection_id}", response_model=ConnectionWithOperation)
def update_connection(
    connection_id: uuid.UUID,
    body: ConnectionUpdate,
    principal: Principal = Depends(get_principal),
    service: ProviderConnectionService = Depends(get_connection_service),
) -> ConnectionWithOperation:
    connection, operation = service.update(
        principal,
        connection_id,
        config=body.config,
        credential_id=body.credential_id,
        clear_credential=body.clear_credential,
    )
    return ConnectionWithOperation(
        connection=_out(connection, service.machine_count(connection.id)),
        operation=OperationDetail.model_validate(operation),
    )


@router.delete("/provider-connections/{connection_id}", status_code=204, response_class=Response)
def delete_connection(
    connection_id: uuid.UUID,
    confirm: str | None = Query(default=None, description="Must equal the connection name"),
    principal: Principal = Depends(get_principal),
    service: ProviderConnectionService = Depends(get_connection_service),
) -> Response:
    service.delete(principal, connection_id, confirm=confirm)
    return Response(status_code=204)


@router.post("/provider-connections/{connection_id}/validate", response_model=OperationDetail, status_code=202)
def validate_connection(
    connection_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: ProviderConnectionService = Depends(get_connection_service),
) -> OperationDetail:
    return OperationDetail.model_validate(service.validate(principal, connection_id))


@router.post("/provider-connections/{connection_id}/discover", response_model=OperationDetail, status_code=202)
def discover_machines(
    connection_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: ProviderConnectionService = Depends(get_connection_service),
) -> OperationDetail:
    """Start a read-only inventory of the provider; results are in the operation's `result.machines`."""
    return OperationDetail.model_validate(service.discover(principal, connection_id))


@router.post("/provider-connections/{connection_id}/machines", response_model=list[ProvisionedMachine], status_code=202)
def provision_machines(
    connection_id: uuid.UUID,
    body: ProvisionRequest,
    principal: Principal = Depends(get_principal),
    service: ProviderConnectionService = Depends(get_connection_service),
) -> list[ProvisionedMachine]:
    """Create machines through the provider; one `machine.provision` operation per machine."""
    results = service.provision(
        principal,
        connection_id,
        names=body.names,
        cpu_cores=body.cpu_cores,
        memory_mb=body.memory_mb,
        disk_gb=body.disk_gb,
        credential_id=body.credential_id,
        ssh_username=body.ssh_username,
        network=body.network,
        labels=body.labels,
        provider_options=body.provider_options,
    )
    return [
        ProvisionedMachine(machine=MachineOut.model_validate(m), operation=OperationDetail.model_validate(o))
        for m, o in results
    ]


@router.post("/provider-connections/{connection_id}/import", response_model=ProvisionedMachine, status_code=201)
def import_machine(
    connection_id: uuid.UUID,
    body: ImportRequest,
    principal: Principal = Depends(get_principal),
    service: ProviderConnectionService = Depends(get_connection_service),
) -> ProvisionedMachine:
    """Add a machine found by discovery to the inventory and inspect it."""
    machine, operation = service.import_machine(
        principal,
        connection_id,
        external_id=body.external_id,
        name=body.name,
        host=body.host,
        port=body.port,
        username=body.username,
        credential_id=body.credential_id,
        kube_api_host=body.kube_api_host,
        kube_api_port=body.kube_api_port,
    )
    return ProvisionedMachine(
        machine=MachineOut.model_validate(machine), operation=OperationDetail.model_validate(operation)
    )


@router.post("/network-discovery", response_model=OperationDetail, status_code=202)
def scan_network(
    body: NetworkScanRequest,
    principal: Principal = Depends(get_principal),
    service: ProviderConnectionService = Depends(get_connection_service),
) -> OperationDetail:
    """Probe address ranges for SSH servers; results are in the operation's `result.hosts`."""
    return OperationDetail.model_validate(
        service.scan_network(principal, ranges=body.ranges, ports=body.ports, timeout_seconds=body.timeout_seconds)
    )

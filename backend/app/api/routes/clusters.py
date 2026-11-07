"""Cluster lifecycle API: validate/plan, create, inspect and delete clusters."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.auth import get_principal
from app.api.deps import get_cluster_service, get_operation_service
from app.api.routes.operations import OperationDetail, OperationSummary
from app.api.schemas import ApiModel
from app.core.principal import Principal
from app.modules.cluster_lifecycle.distributions.spec import ClusterSpec
from app.modules.cluster_lifecycle.models import Cluster
from app.modules.cluster_lifecycle.service import ClusterService
from app.orchestration.service import OperationService

router = APIRouter(prefix="/clusters", tags=["clusters"])


class ClusterOut(ApiModel):
    id: uuid.UUID
    name: str
    display_name: str | None
    description: str | None
    distribution: str
    origin: str
    lifecycle_state: str
    kubernetes_version: str | None
    api_endpoint: str | None
    last_contact_at: datetime | None
    created_at: datetime
    node_count: int = 0
    active_operation_id: uuid.UUID | None = None
    kubeconfig_context: str | None = None
    api_server_override: str | None = None
    tls_server_name: str | None = None
    connected: bool = False
    health: dict[str, Any] | None = None
    health_error: dict[str, Any] | None = None
    observed_at: str | None = None


class ClusterNode(BaseModel):
    machine_id: uuid.UUID
    machine_name: str
    node_name: str | None
    role: str
    membership_state: str
    address: str | None
    os: str | None
    cpu_cores: int | None
    memory_mb: int | None


class ClusterDetail(ClusterOut):
    spec: dict[str, Any]
    status_detail: dict[str, Any]
    has_kubeconfig: bool
    nodes: list[ClusterNode]
    operations: list[OperationSummary]


class ClusterCreate(BaseModel):
    spec: ClusterSpec
    description: str | None = Field(default=None, max_length=1000)


class ClusterValidation(BaseModel):
    valid: bool
    problems: list[str]
    steps: list[dict[str, str]]
    nodes: list[dict[str, Any]]
    notes: list[str] = []


class ClusterCreated(BaseModel):
    cluster: ClusterOut
    operation: OperationDetail


def _summary(service: ClusterService, cluster: Cluster, counts: dict[uuid.UUID, int]) -> ClusterOut:
    out = ClusterOut.model_validate(cluster)
    detail = cluster.status_detail or {}
    out.connected = cluster.access_credential_id is not None
    out.health = detail.get("health")
    out.health_error = detail.get("health_error")
    out.observed_at = detail.get("observed_at")
    out.node_count = counts.get(cluster.id) or (out.health or {}).get("nodes", {}).get("total", 0)
    active = service.active_operation(cluster.id)
    out.active_operation_id = active.id if active else None
    return out


@router.get("", response_model=list[ClusterOut])
def list_clusters(
    principal: Principal = Depends(get_principal), service: ClusterService = Depends(get_cluster_service)
) -> list[ClusterOut]:
    counts = service.node_counts(principal)
    return [_summary(service, c, counts) for c in service.list_clusters(principal)]


@router.post("/validate", response_model=ClusterValidation)
def validate_cluster(
    body: ClusterCreate,
    principal: Principal = Depends(get_principal),
    service: ClusterService = Depends(get_cluster_service),
) -> ClusterValidation:
    """Check a specification against the inventory and preview the installation plan. No side effects."""
    result = service.validate(principal, body.spec)
    return ClusterValidation(
        valid=result.valid,
        problems=result.problems,
        steps=result.steps,
        nodes=[n.model_dump(mode="json") for n in result.nodes],
        notes=result.notes,
    )


@router.post("", response_model=ClusterCreated, status_code=202)
def create_cluster(
    body: ClusterCreate,
    principal: Principal = Depends(get_principal),
    service: ClusterService = Depends(get_cluster_service),
) -> ClusterCreated:
    cluster, operation = service.create(principal, body.spec, description=body.description)
    return ClusterCreated(
        cluster=_summary(service, cluster, service.node_counts(principal)),
        operation=OperationDetail.model_validate(operation),
    )


@router.get("/{cluster_id}", response_model=ClusterDetail)
def get_cluster(
    cluster_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: ClusterService = Depends(get_cluster_service),
    operations: OperationService = Depends(get_operation_service),
) -> ClusterDetail:
    cluster = service.get(principal, cluster_id)
    summary = _summary(service, cluster, service.node_counts(principal))
    nodes = [
        ClusterNode(
            machine_id=machine.id,
            machine_name=machine.name,
            node_name=membership.node_name,
            role=membership.role.value,
            membership_state=membership.membership_state.value,
            address=next((a["address"] for a in machine.addresses if a.get("type") == "primary"), None),
            os=machine.os_info.get("pretty_name"),
            cpu_cores=machine.hardware.get("cpu_cores"),
            memory_mb=machine.hardware.get("memory_mb"),
        )
        for membership, machine in service.node_details(cluster.id)
    ]
    history, _ = operations.list_operations(principal, target_type="cluster", target_id=str(cluster.id), limit=20)
    return ClusterDetail(
        **summary.model_dump(),
        spec=cluster.spec,
        status_detail=cluster.status_detail,
        has_kubeconfig=cluster.access_credential_id is not None,
        nodes=nodes,
        operations=[OperationSummary.model_validate(o) for o in history],
    )


@router.delete("/{cluster_id}", response_model=OperationDetail, status_code=202)
def delete_cluster(
    cluster_id: uuid.UUID,
    confirm: str | None = Query(default=None, description="Must equal the cluster name"),
    force: bool = Query(default=False, description="Continue when some nodes are unreachable"),
    principal: Principal = Depends(get_principal),
    service: ClusterService = Depends(get_cluster_service),
) -> OperationDetail:
    """Run `kubeadm reset` on every node, remove the stored kubeconfig and release the machines."""
    return OperationDetail.model_validate(service.delete(principal, cluster_id, confirm=confirm, force=force))

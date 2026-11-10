"""Phase 6 DNS diagnostics and CSI add-on operations."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.auth import get_principal
from app.api.deps import get_kubernetes_service
from app.api.routes.operations import OperationDetail
from app.core.principal import Principal
from app.modules.kubernetes import dns, phase6, storage
from app.modules.kubernetes.service import KubernetesService

router = APIRouter(prefix="/clusters", tags=["networking and storage"])


@router.post("/{cluster_id}/dns/lookup", response_model=OperationDetail, status_code=202)
def dns_lookup(
    cluster_id: uuid.UUID,
    body: dns.DnsLookup,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> OperationDetail:
    return OperationDetail.model_validate(
        phase6.submit(service, principal, cluster_id, dns.DNS_LOOKUP, body.model_dump())
    )


@router.get("/{cluster_id}/storage/nfs")
def nfs_status(
    cluster_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict[str, Any]:
    _, api = service.api(principal, cluster_id)
    return storage.status(api)


@router.post("/{cluster_id}/storage/nfs", response_model=OperationDetail, status_code=202)
def install_nfs(
    cluster_id: uuid.UUID,
    body: storage.NfsInstall,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> OperationDetail:
    return OperationDetail.model_validate(
        phase6.submit(service, principal, cluster_id, storage.NFS_INSTALL, body.model_dump())
    )


@router.delete("/{cluster_id}/storage/nfs", response_model=OperationDetail, status_code=202)
def remove_nfs(
    cluster_id: uuid.UUID,
    confirm: str | None = Query(default=None, description="Repeat the cluster name"),
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> OperationDetail:
    return OperationDetail.model_validate(
        phase6.submit(service, principal, cluster_id, storage.NFS_REMOVE, {}, confirm=confirm)
    )

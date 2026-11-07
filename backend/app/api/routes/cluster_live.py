"""Cluster connectivity and live Kubernetes data.

Reads go to the cluster's API at request time (with timeouts). When a cluster is
unreachable these endpoints return 503 ``cluster_unreachable``; the rest of the
platform keeps working.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field, SecretStr

from app.api.auth import get_principal
from app.api.deps import get_kubernetes_service
from app.api.routes.operations import OperationDetail
from app.core.principal import Principal
from app.modules.kubernetes.service import KubernetesService

router = APIRouter(prefix="/clusters", tags=["cluster connectivity"])


class KubeconfigInspect(BaseModel):
    kubeconfig: SecretStr


class ClusterImport(BaseModel):
    name: str = Field(description="Lowercase DNS label")
    kubeconfig: SecretStr = Field(description="Kubeconfig YAML; only the selected context is stored (encrypted)")
    context: str | None = None
    server_override: str | None = Field(default=None, description="Reach the API server through this https URL")
    tls_server_name: str | None = Field(default=None, description="Name/IP to verify in the API server certificate")
    description: str | None = Field(default=None, max_length=1000)


class ConnectionSettings(BaseModel):
    server_override: str | None = None
    tls_server_name: str | None = None


class ClusterImported(BaseModel):
    cluster_id: uuid.UUID
    operation: OperationDetail


class MetricsServerInstall(BaseModel):
    kubelet_insecure_tls: bool = False


@router.post("/import/inspect")
def inspect_kubeconfig(
    body: KubeconfigInspect, service: KubernetesService = Depends(get_kubernetes_service)
) -> dict[str, Any]:
    """List the contexts in a kubeconfig and whether each can be used. Nothing is stored."""
    return service.inspect_kubeconfig(body.kubeconfig.get_secret_value())


@router.post("/import", response_model=ClusterImported, status_code=202)
def import_cluster(
    body: ClusterImport,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> ClusterImported:
    cluster, operation = service.import_cluster(
        principal,
        name=body.name,
        kubeconfig_text=body.kubeconfig.get_secret_value(),
        context=body.context,
        server_override=body.server_override,
        tls_server_name=body.tls_server_name,
        description=body.description,
    )
    return ClusterImported(cluster_id=cluster.id, operation=OperationDetail.model_validate(operation))


@router.put("/{cluster_id}/connection", response_model=OperationDetail, status_code=202)
def update_connection(
    cluster_id: uuid.UUID,
    body: ConnectionSettings,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> OperationDetail:
    """Change how the platform reaches the API server, then re-verify the connection."""
    return OperationDetail.model_validate(
        service.update_connection(
            principal, cluster_id, server_override=body.server_override, tls_server_name=body.tls_server_name
        )
    )


@router.post("/{cluster_id}/connect", response_model=OperationDetail, status_code=202)
def reconnect(
    cluster_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> OperationDetail:
    return OperationDetail.model_validate(service.connect(principal, cluster_id))


@router.post("/{cluster_id}/unregister", status_code=204, response_class=Response)
def unregister(
    cluster_id: uuid.UUID,
    confirm: str | None = Query(default=None, description="Must equal the cluster name"),
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> Response:
    """Forget an imported cluster and its stored kubeconfig. The cluster itself is not changed."""
    service.unregister(principal, cluster_id, confirm=confirm)
    return Response(status_code=204)


@router.post("/{cluster_id}/addons/metrics-server", response_model=OperationDetail, status_code=202)
def install_metrics_server(
    cluster_id: uuid.UUID,
    body: MetricsServerInstall,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> OperationDetail:
    return OperationDetail.model_validate(
        service.install_metrics_server(principal, cluster_id, kubelet_insecure_tls=body.kubelet_insecure_tls)
    )


# ---------------------------------------------------------------------------- live reads


@router.get("/{cluster_id}/health")
def health(
    cluster_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict[str, Any]:
    return service.health(principal, cluster_id)


@router.get("/{cluster_id}/nodes")
def nodes(
    cluster_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> list[dict[str, Any]]:
    return service.nodes(principal, cluster_id)


@router.get("/{cluster_id}/nodes/{node_name}")
def node(
    cluster_id: uuid.UUID,
    node_name: str,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict[str, Any]:
    return service.node(principal, cluster_id, node_name)


@router.get("/{cluster_id}/namespaces")
def namespaces(
    cluster_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> list[dict[str, Any]]:
    return service.namespaces(principal, cluster_id)


@router.get("/{cluster_id}/pods")
def pods(
    cluster_id: uuid.UUID,
    namespace: str | None = Query(default=None, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"),
    node: str | None = Query(default=None, pattern=r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"),
    problems: bool = False,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict[str, Any]:
    return service.pods(principal, cluster_id, namespace=namespace, node=node, problems_only=problems)


@router.get("/{cluster_id}/events")
def events(
    cluster_id: uuid.UUID,
    namespace: str | None = Query(default=None, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"),
    warnings: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> list[dict[str, Any]]:
    return service.events(principal, cluster_id, namespace=namespace, warnings_only=warnings, limit=limit)


@router.get("/{cluster_id}/topology")
def topology(
    cluster_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict[str, Any]:
    return service.topology(principal, cluster_id)

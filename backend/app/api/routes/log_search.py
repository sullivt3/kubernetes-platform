"""Loki connection settings and historical pod log searches."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app.api.auth import get_principal
from app.api.deps import get_kubernetes_service
from app.api.routes.operations import OperationDetail
from app.core.errors import InvalidStateError
from app.core.principal import Principal
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import ClusterLifecycleState
from app.modules.kubernetes import client as kc
from app.modules.kubernetes import log_collection, log_search
from app.modules.kubernetes.client import ClusterAPI
from app.modules.kubernetes.phase6 import submit
from app.modules.kubernetes.service import KubernetesService
from app.modules.kubernetes.service_proxy import ServiceEndpoint

router = APIRouter(prefix="/clusters/{cluster_id}/logs", tags=["log search"])


class LokiConnectionSettings(BaseModel):
    connection: ServiceEndpoint | None


@router.get("/connection")
def get_connection(
    cluster_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> LokiConnectionSettings:
    cluster = service.cluster(principal, cluster_id)
    return LokiConnectionSettings.model_validate({"connection": cluster.loki_connection})


@router.put("/connection")
def update_connection(
    cluster_id: uuid.UUID,
    body: LokiConnectionSettings,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> LokiConnectionSettings:
    cluster = service.cluster(principal, cluster_id)
    if cluster.lifecycle_state in {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}:
        raise InvalidStateError("The cluster is being deleted")
    before = cluster.loki_connection
    after = body.connection.model_dump() if body.connection else None
    if before != after:
        cluster.loki_connection = after
        audit.record(
            service.session,
            principal=principal,
            action="cluster.logs.configure",
            resource_type="cluster",
            resource_id=cluster.id,
            resource_name=cluster.name,
            details={"before": before, "after": after},
        )
        service.session.commit()
    return body


def _configured_loki(
    service: KubernetesService, principal: Principal, cluster_id: uuid.UUID
) -> tuple[ClusterAPI, ServiceEndpoint]:
    cluster = service.cluster(principal, cluster_id)
    if not cluster.loki_connection:
        raise InvalidStateError("Connect a Loki Service for this cluster")
    _, api = service.api(principal, cluster_id)
    return api, ServiceEndpoint.model_validate(cluster.loki_connection)


@router.get("/namespaces")
def list_namespaces(
    cluster_id: uuid.UUID,
    response: Response,
    window: log_search.Window = "15m",
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> list[str]:
    api, connection = _configured_loki(service, principal, cluster_id)
    response.headers["Cache-Control"] = "no-store"
    return log_search.list_labels(api, connection, "namespace", window)


@router.get("/pods")
def list_pods(
    cluster_id: uuid.UUID,
    response: Response,
    namespace: log_search.DNSLabel,
    window: log_search.Window = "15m",
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> list[str]:
    api, connection = _configured_loki(service, principal, cluster_id)
    response.headers["Cache-Control"] = "no-store"
    return log_search.list_labels(api, connection, "pod", window, log_search.SearchFilters(namespace=namespace))


@router.get("/containers")
def list_containers(
    cluster_id: uuid.UUID,
    response: Response,
    namespace: log_search.DNSLabel,
    pod: log_search.PodName | None = None,
    window: log_search.Window = "15m",
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> list[str]:
    api, connection = _configured_loki(service, principal, cluster_id)
    response.headers["Cache-Control"] = "no-store"
    return log_search.list_labels(
        api, connection, "container", window, log_search.SearchFilters(namespace=namespace, pod=pod)
    )


@router.get("")
def query_logs(
    cluster_id: uuid.UUID,
    response: Response,
    filters: Annotated[log_search.SearchFilters, Query()],
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> log_search.LogResults:
    api, connection = _configured_loki(service, principal, cluster_id)
    response.headers["Cache-Control"] = "no-store"
    return log_search.search(api, connection, filters)


class CollectionSettings(BaseModel):
    namespaces: list[log_search.DNSLabel] = Field(min_length=1, max_length=30)


@router.get("/collection")
def get_collection(
    cluster_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict:
    cluster = service.cluster(principal, cluster_id)
    if not cluster.loki_connection:
        raise InvalidStateError("Connect Loki first")
    _, api = service.api(principal, cluster_id)
    ns = cluster.loki_connection["namespace"]
    deploy = log_collection.optional(api, f"/apis/apps/v1/namespaces/{ns}/deployments/alloy")
    managed = bool(deploy and log_collection.owned(deploy, str(cluster.id)))
    selected = []
    if managed and deploy is not None:
        volumes = deploy["spec"]["template"]["spec"]["volumes"]
        config = next(v["configMap"]["name"] for v in volumes if v["name"] == "config")
        obj = kc.get_json(api, log_collection.path(ns, "configmaps", config))
        import json

        selected = json.loads(
            (obj["metadata"].get("annotations") or {}).get("kubernetes-platform.io/collected-namespaces", "[]")
        )
    rows, truncated = kc.list_items(api, "/api/v1/namespaces")
    return {
        "managed": managed,
        "selected": selected,
        "available": sorted(n["metadata"]["name"] for n in rows),
        "truncated": truncated,
    }


@router.post("/collection", status_code=202)
def update_collection(
    cluster_id: uuid.UUID,
    body: CollectionSettings,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> OperationDetail:
    cluster = service.cluster(principal, cluster_id)
    if not cluster.loki_connection:
        raise InvalidStateError("Connect Loki first")
    params = {"collector_namespace": cluster.loki_connection["namespace"], "namespaces": body.namespaces}
    return OperationDetail.model_validate(submit(service, principal, cluster_id, "cluster.logs.collection", params))

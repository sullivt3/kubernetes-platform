"""Monitoring connection settings and Prometheus queries for the selected cluster."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from app.api.auth import get_principal
from app.api.deps import get_kubernetes_service
from app.core.errors import InvalidStateError
from app.core.principal import Principal
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import ClusterLifecycleState
from app.modules.kubernetes import alerts, monitoring
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.service import KubernetesService, connection_target
from app.modules.kubernetes.service_proxy import DNSLabel

router = APIRouter(prefix="/clusters/{cluster_id}/monitoring", tags=["monitoring"])


class ConnectionSettings(BaseModel):
    connection: monitoring.PrometheusService | None


@router.get("/connection")
def get_monitoring_connection(
    cluster_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> ConnectionSettings:
    cluster = service.cluster(principal, cluster_id)
    return ConnectionSettings.model_validate({"connection": cluster.prometheus_connection})


@router.put("/connection")
def update_monitoring_connection(
    cluster_id: uuid.UUID,
    body: ConnectionSettings,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> ConnectionSettings:
    cluster = service.cluster(principal, cluster_id)
    if cluster.lifecycle_state in {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}:
        raise InvalidStateError("The cluster is being deleted")
    before = cluster.prometheus_connection
    after = body.connection.model_dump(mode="json", exclude_none=True) if body.connection else None
    if before != after:
        cluster.prometheus_connection = after
        audit.record(
            service.session,
            principal=principal,
            action="cluster.monitoring.configure",
            resource_type="cluster",
            resource_id=cluster.id,
            resource_name=cluster.name,
            details={"before": before, "after": after},
        )
        service.session.commit()
    return body


@router.get("")
def read_monitoring(
    cluster_id: uuid.UUID,
    response: Response,
    window: monitoring.Window = "1h",
    namespace: DNSLabel | None = None,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> monitoring.MonitoringReport:
    cluster = service.cluster(principal, cluster_id)
    if not cluster.prometheus_connection:
        raise InvalidStateError("Connect a Prometheus Service for this cluster")
    _, api = service.api(principal, cluster_id)
    connection = monitoring.PrometheusService.model_validate(cluster.prometheus_connection)
    response.headers["Cache-Control"] = "no-store"
    return monitoring.collect(api, connection, window, namespace)


@router.get("/alerts")
def read_alerts(
    cluster_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict:
    cluster, api = service.api(principal, cluster_id)
    response.headers["Cache-Control"] = "no-store"
    connection = (
        monitoring.PrometheusService.model_validate(cluster.prometheus_connection)
        if cluster.prometheus_connection
        else None
    )
    result = alerts.collect(api, connection)
    try:
        target = connection_target(service.session, service.secret_store, cluster)
        expires = datetime.fromisoformat(kc.serving_certificate_expiry(target))
        days = (expires - datetime.now(UTC)).days
        result["coverage"]["certificate"] = "ok"
        if days < 30:
            result["findings"].append(
                {
                    "source": "certificate",
                    "name": "API server",
                    "namespace": None,
                    "title": "API certificate expires soon",
                    "severity": "critical" if days < 7 else "warning",
                    "evidence": f"Verified serving certificate expires {expires.isoformat()} ({days} days).",
                    "guidance": "Review certificate renewal for this distribution before the expiration date.",
                }
            )
    except Exception:
        result["coverage"]["certificate"] = "unavailable"
    return result

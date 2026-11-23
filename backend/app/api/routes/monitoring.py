"""Monitoring connection settings and Prometheus queries for the selected cluster."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.api.auth import get_principal
from app.api.deps import get_kubernetes_service
from app.api.routes.operations import OperationDetail
from app.core.errors import ConfirmationRequiredError, InvalidStateError, NotFoundError
from app.core.principal import Principal
from app.db.session import get_session_factory
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import ClusterLifecycleState
from app.modules.kubernetes import alerts, monitoring, remediation
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.remediation_models import RemediationRequest
from app.modules.kubernetes.service import KubernetesService, connection_target
from app.modules.kubernetes.service_proxy import DNSLabel
from app.modules.organizations.policy import require
from app.orchestration.models import Operation

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


@router.get("/remediation")
def get_remediation(
    cluster_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict:
    cluster = service.cluster(principal, cluster_id)
    response.headers["Cache-Control"] = "no-store"
    rows = service.session.scalars(
        select(RemediationRequest)
        .where(
            RemediationRequest.cluster_id == cluster_id,
        )
        .order_by(RemediationRequest.created_at.desc())
        .limit(100)
    ).all()
    operations = {
        op.id: op
        for op in service.session.scalars(
            select(Operation).where(
                Operation.id.in_([r.operation_id for r in rows if r.operation_id]),
            )
        )
    }
    return {
        "policy": remediation.RemediationPolicy.model_validate(cluster.remediation_policy or {}).model_dump(),
        "observation": (cluster.status_detail or {}).get("remediation"),
        "requests": [
            {
                "id": r.id,
                "namespace": r.namespace,
                "pod_name": r.pod_name,
                "deployment_name": r.snapshot["deployment_name"],
                "reason": r.reason,
                "status": r.status,
                "first_seen_at": r.first_seen_at,
                "last_seen_at": r.last_seen_at,
                "expires_at": r.expires_at,
                "approved_by": r.approved_by,
                "operation_id": r.operation_id,
                "operation_status": operations[r.operation_id].status if r.operation_id in operations else None,
                "confirmation": cluster.name,
            }
            for r in rows
        ],
    }


@router.put("/remediation")
def configure_remediation(
    cluster_id: uuid.UUID,
    body: remediation.RemediationPolicy,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict:
    require(principal, "organization.manage")
    cluster = service.cluster(principal, cluster_id)
    if cluster.lifecycle_state in remediation.INACTIVE:
        raise InvalidStateError("The cluster is being deleted")
    locked_cluster = service.session.scalar(
        select(type(cluster)).where(type(cluster).id == cluster_id).with_for_update()
    )
    assert locked_cluster is not None
    cluster = locked_cluster
    cluster.remediation_policy = body.model_dump()
    # Every configuration edit invalidates outstanding observations and approvals.
    for row in service.session.scalars(
        select(RemediationRequest)
        .where(
            RemediationRequest.cluster_id == cluster_id,
            RemediationRequest.status.in_(["observing", "pending", "approved"]),
        )
        .with_for_update()
    ):
        row.status = "revoked"
    audit.record(
        service.session,
        principal=principal,
        action="remediation.configure",
        resource_type="cluster",
        resource_id=cluster.id,
        resource_name=cluster.name,
        details=body.model_dump(),
    )
    service.session.commit()
    return body.model_dump()


@router.post("/remediation/scan")
def scan_remediation(
    cluster_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict:
    require(principal, "organization.manage")
    service.cluster(principal, cluster_id)
    return remediation.scan_cluster(get_session_factory(), service.secret_store, cluster_id)


class RemediationApproval(BaseModel):
    confirm: str


@router.post("/remediation/{request_id}/approve", status_code=202)
def approve_remediation(
    cluster_id: uuid.UUID,
    request_id: uuid.UUID,
    body: RemediationApproval,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> OperationDetail:
    require(principal, "organization.manage")
    cluster = service.cluster(principal, cluster_id)
    locked_cluster = service.session.scalar(
        select(type(cluster)).where(type(cluster).id == cluster_id).with_for_update()
    )
    assert locked_cluster is not None
    cluster = locked_cluster
    row = service.session.scalar(
        select(RemediationRequest)
        .where(
            RemediationRequest.id == request_id,
            RemediationRequest.cluster_id == cluster_id,
        )
        .with_for_update()
    )
    if row is None:
        raise NotFoundError("Remediation request not found")
    if body.confirm != cluster.name:
        raise ConfirmationRequiredError("Repeat the cluster name to approve replacing this pod")
    if row.operation_id:
        return OperationDetail.model_validate(service.operations.get(principal, row.operation_id))
    policy = remediation.RemediationPolicy.model_validate(cluster.remediation_policy or {})
    if (
        not policy.enabled
        or row.namespace not in policy.namespaces
        or cluster.lifecycle_state in remediation.INACTIVE
        or row.status != "pending"
        or row.expires_at is None
        or row.expires_at < datetime.now(UTC)
    ):
        raise InvalidStateError("Proposal is not pending, has expired, or the policy is disabled")
    if remediation.cooldown(service.session, cluster_id, row.snapshot["deployment_uid"], policy.cooldown_minutes):
        raise InvalidStateError("This Deployment is within its remediation cooldown")
    row.status, row.approved_by, row.approved_at = "approved", principal.subject, datetime.now(UTC)
    audit.record(
        service.session,
        principal=principal,
        action="remediation.approve",
        resource_type="cluster",
        resource_id=cluster.id,
        resource_name=cluster.name,
        details={"request_id": str(row.id), "pod": row.pod_name},
    )
    op = service.operations.submit(
        principal,
        remediation.WORKFLOW,
        {
            "cluster_id": str(cluster.id),
            "cluster_name": cluster.name,
            "request_id": str(row.id),
        },
        confirm=cluster.name,
        idempotency_key=f"remediation:{row.id}",
    )
    # The worker may already have attached the same operation during dispatch.
    service.session.refresh(row)
    row.operation_id = op.id
    service.session.commit()
    return OperationDetail.model_validate(op)


@router.post("/remediation/{request_id}/dismiss")
def dismiss_remediation(
    cluster_id: uuid.UUID,
    request_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict:
    require(principal, "organization.manage")
    service.cluster(principal, cluster_id)
    row = service.session.scalar(
        select(RemediationRequest)
        .where(
            RemediationRequest.id == request_id,
            RemediationRequest.cluster_id == cluster_id,
        )
        .with_for_update()
    )
    if row is None:
        raise NotFoundError("Remediation request not found")
    if row.status not in {"observing", "pending"}:
        raise InvalidStateError("Only an unapproved request can be dismissed")
    row.status = "dismissed"
    audit.record(
        service.session,
        principal=principal,
        action="remediation.dismiss",
        resource_type="cluster",
        resource_id=cluster_id,
        details={"request_id": str(row.id)},
    )
    service.session.commit()
    return {"status": row.status}

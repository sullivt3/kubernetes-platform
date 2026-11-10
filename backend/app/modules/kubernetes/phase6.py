"""Shared connection and submission guards for networking and storage operations."""

from __future__ import annotations

import uuid
from typing import Any

from app.core.errors import InvalidStateError
from app.core.principal import Principal
from app.modules.cluster_lifecycle.models import Cluster, ClusterLifecycleState
from app.modules.kubernetes.client import ClusterAPI
from app.modules.kubernetes.service import KubernetesService, api_for
from app.orchestration.context import StepContext
from app.orchestration.models import Operation
from app.orchestration.workflow import StepFailedError


def submit(
    service: KubernetesService,
    principal: Principal,
    cluster_id: uuid.UUID,
    workflow: str,
    params: dict[str, Any],
    *,
    confirm: str | None = None,
) -> Operation:
    cluster = service.cluster(principal, cluster_id)
    if cluster.lifecycle_state in (ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED):
        raise InvalidStateError("The cluster is being deleted")
    if cluster.access_credential_id is None:
        raise InvalidStateError("The cluster is not connected yet")
    return service.operations.submit(
        principal,
        workflow,
        {**params, "cluster_id": str(cluster.id), "cluster_name": cluster.name},
        confirm=confirm,
    )


def workflow_api(ctx: StepContext, cluster_id: uuid.UUID) -> ClusterAPI:
    with ctx.db() as session:
        cluster = session.get(Cluster, cluster_id)
        if cluster is None or cluster.organization_id != ctx.organization_id:
            raise StepFailedError("Cluster no longer exists", code="cluster_missing")
        if cluster.lifecycle_state in (ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED):
            raise StepFailedError("The cluster is being deleted", code="cluster_deleting")
        return api_for(session, ctx.secret_store, cluster)

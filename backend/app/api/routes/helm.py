"""Scoped Helm repositories, public chart browsing and reviewed release operations."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field, StringConstraints

from app.api.auth import get_principal
from app.api.deps import get_kubernetes_service
from app.api.routes.operations import OperationDetail
from app.core.errors import InvalidStateError, NotFoundError
from app.core.principal import Principal
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import ClusterLifecycleState
from app.modules.helm import catalog, credentials
from app.modules.helm import service as hs
from app.modules.helm.runtime import Runner, public_history, public_release
from app.modules.kubernetes.service import KubernetesService, connection_target
from app.modules.kubernetes.service_proxy import DNSLabel

router = APIRouter(prefix="/clusters/{cluster_id}/helm", tags=["Helm"])
ReleaseName = Annotated[str, StringConstraints(pattern=hs.RELEASE_PATTERN, max_length=53)]
ChartName = Annotated[str, StringConstraints(pattern=catalog.NAME, max_length=128)]
ChartVersion = Annotated[str, StringConstraints(pattern=catalog.VERSION)]


class Repositories(BaseModel):
    repositories: list[credentials.RepositoryInput] = Field(max_length=20)


@router.get("/repositories")
def repositories(
    cluster_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> list[dict]:
    return service.cluster(principal, cluster_id).helm_repositories or []


@router.put("/repositories")
def save_repositories(
    cluster_id: uuid.UUID,
    body: Repositories,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> list[dict]:
    cluster = service.cluster(principal, cluster_id)
    if cluster.lifecycle_state in {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}:
        raise InvalidStateError("The cluster is being deleted")
    cluster.helm_repositories = credentials.save(service, principal, cluster, body.repositories)
    audit.record(
        service.session,
        principal=principal,
        action="helm.repositories.configure",
        resource_type="cluster",
        resource_id=cluster.id,
        details={"names": [r.name for r in body.repositories]},
    )
    service.session.commit()
    return cluster.helm_repositories


@router.get("/charts")
def charts(
    cluster_id: uuid.UUID,
    repository: DNSLabel,
    search: str = Query(default="", max_length=100),
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict:
    cluster = service.cluster(principal, cluster_id)
    repo = hs.repository(cluster, repository)
    auth = credentials.authorization(service.session, service.secret_store, cluster, repo)
    entries = catalog.index(repo, auth) if auth else catalog.index(repo)
    matching = sorted(name for name in entries if search.lower() in name.lower())
    return {
        "items": [
            {
                "name": name,
                "versions": [
                    {
                        "version": v["version"],
                        "app_version": str(v.get("appVersion", ""))[:128],
                        "description": str(v.get("description", ""))[:500],
                    }
                    for v in entries[name]
                ],
            }
            for name in matching[:500]
        ],
        "truncated": len(matching) > 500,
    }


@router.get("/charts/values")
def chart_values(
    cluster_id: uuid.UUID,
    response: Response,
    repository: DNSLabel,
    chart: ChartName,
    version: ChartVersion,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict:
    cluster = service.cluster(principal, cluster_id)
    repo = hs.repository(cluster, repository)
    auth = credentials.authorization(service.session, service.secret_store, cluster, repo)
    spec = catalog.chart(repo, chart, version, auth) if auth else catalog.chart(repo, chart, version)
    data = catalog.archive(spec, auth) if auth else catalog.archive(spec)
    with Runner() as helm:
        values = helm.run(["show", "values", str(helm.write("chart.tgz", data))])
        if len(values) > 128 * 1024:
            raise InvalidStateError("Chart defaults exceed the supported values editor size")
    response.headers["Cache-Control"] = "no-store"
    return {"source": spec, "values_yaml": values.decode()}


@router.get("/releases")
def releases(
    cluster_id: uuid.UUID,
    response: Response,
    namespace: DNSLabel | None = None,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict:
    cluster = service.cluster(principal, cluster_id)
    with Runner(connection_target(service.session, service.secret_store, cluster)) as helm:
        rows = helm.releases(namespace)
    response.headers["Cache-Control"] = "no-store"
    return {"items": [public_release(r) for r in rows[:256]], "truncated": len(rows) > 256}


@router.get("/releases/{namespace}/{release}/history")
def history(
    cluster_id: uuid.UUID,
    namespace: DNSLabel,
    release: ReleaseName,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> list[dict]:
    cluster = service.cluster(principal, cluster_id)
    with Runner(connection_target(service.session, service.secret_store, cluster)) as helm:
        if not helm.release(namespace, release):
            raise NotFoundError("Helm release not found")
        rows = helm.history(namespace, release)
    response.headers["Cache-Control"] = "no-store"
    return [public_history(r) for r in rows]


@router.post("/preview")
def preview(
    cluster_id: uuid.UUID,
    body: hs.PlanInput,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return hs.preview(service, principal, cluster_id, body)


class ExecutePlan(BaseModel):
    plan_id: uuid.UUID
    confirm: str | None = None


@router.post("/execute", status_code=202)
def execute(
    cluster_id: uuid.UUID,
    body: ExecutePlan,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> OperationDetail:
    return OperationDetail.model_validate(hs.submit(service, principal, cluster_id, body.plan_id, body.confirm))

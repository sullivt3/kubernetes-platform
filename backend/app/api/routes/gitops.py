"""Cluster-scoped GitOps application configuration, reviews, history and sync."""

import uuid

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.api.auth import get_principal
from app.api.deps import get_kubernetes_service
from app.api.routes.operations import OperationDetail
from app.core.errors import ConfirmationRequiredError
from app.core.principal import Principal
from app.modules.auditing import service as audit
from app.modules.gitops import automation
from app.modules.gitops import service as gs
from app.modules.gitops.models import GitOpsApplication, GitOpsReview
from app.modules.gitops.policy import PolicyInput, ReviewInput
from app.modules.gitops.source import ApplicationInput
from app.modules.kubernetes.service import KubernetesService

router = APIRouter(prefix="/clusters/{cluster_id}/gitops", tags=["GitOps"])


@router.get("/applications")
def applications(
    cluster_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    service.cluster(principal, cluster_id)
    response.headers["Cache-Control"] = "no-store"
    return [
        gs.public(row)
        for row in service.session.scalars(
            select(GitOpsApplication)
            .where(GitOpsApplication.cluster_id == cluster_id)
            .order_by(GitOpsApplication.name)
            .limit(100)
        )
    ]


@router.post("/applications", status_code=201)
def create(
    cluster_id: uuid.UUID,
    body: ApplicationInput,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    return gs.save(service, principal, cluster_id, body)


@router.put("/applications/{application_id}")
def update(
    cluster_id: uuid.UUID,
    application_id: uuid.UUID,
    body: ApplicationInput,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    return gs.save(service, principal, cluster_id, body, application_id)


class Confirmation(BaseModel):
    confirm: str


@router.post("/applications/{application_id}/forget")
def forget(
    cluster_id: uuid.UUID,
    application_id: uuid.UUID,
    body: Confirmation,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    _, row = gs.application(service, principal, cluster_id, application_id)
    service.session.refresh(row, with_for_update=True)
    gs.busy(service.session, row)
    if body.confirm != row.name:
        raise ConfirmationRequiredError("Repeat the application name to remove its registration")
    audit.record(
        service.session,
        principal=principal,
        action="gitops.forget",
        resource_type="gitops_application",
        resource_id=row.id,
        details={"resources_retained": True},
    )
    service.session.delete(row)
    service.session.commit()
    return {"removed": True, "resources_retained": True}


@router.post("/applications/{application_id}/review")
def review(
    cluster_id: uuid.UUID,
    application_id: uuid.UUID,
    response: Response,
    body: ReviewInput | None = None,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    response.headers["Cache-Control"] = "no-store"
    return gs.preview(service, principal, cluster_id, application_id, **(body.model_dump() if body else {}))


@router.get("/applications/{application_id}/history")
def history(
    cluster_id: uuid.UUID,
    application_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    gs.application(service, principal, cluster_id, application_id)
    response.headers["Cache-Control"] = "no-store"
    return [
        {
            "id": row.id,
            "commit": row.commit_sha,
            "created_at": row.created_at,
            "expires_at": row.expires_at,
            "generation": row.generation,
            "operation_id": row.operation_id,
            **row.summary,
        }
        for row in service.session.scalars(
            select(GitOpsReview)
            .where(GitOpsReview.application_id == application_id)
            .order_by(GitOpsReview.created_at.desc())
            .limit(20)
        )
    ]


class SyncInput(Confirmation):
    review_id: uuid.UUID


@router.post("/applications/{application_id}/sync", status_code=202)
def sync(
    cluster_id: uuid.UUID,
    application_id: uuid.UUID,
    body: SyncInput,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    return OperationDetail.model_validate(
        gs.submit(service, principal, cluster_id, application_id, body.review_id, body.confirm)
    )


@router.put("/applications/{application_id}/automation")
def configure_automation(
    cluster_id: uuid.UUID,
    application_id: uuid.UUID,
    body: PolicyInput,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    return automation.configure(service, principal, cluster_id, application_id, body)


@router.post("/applications/{application_id}/automation/pause")
def pause_automation(
    cluster_id: uuid.UUID,
    application_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    return automation.pause(service, principal, cluster_id, application_id)


@router.post("/applications/{application_id}/automation/reconcile")
def reconcile_now(
    cluster_id: uuid.UUID,
    application_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    return automation.request_now(service, principal, cluster_id, application_id)

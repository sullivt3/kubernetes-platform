"""Tenant-scoped reusable workflow definitions and reviewed runs."""

import uuid

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.api.auth import get_principal
from app.api.deps import get_kubernetes_service
from app.api.routes.operations import OperationDetail
from app.core.principal import Principal
from app.modules.custom_workflows import service as workflows
from app.modules.custom_workflows.models import CustomWorkflow
from app.modules.kubernetes import reviewed_operations as tasks
from app.modules.kubernetes.custom_resource_models import CustomResourceReview
from app.modules.kubernetes.service import KubernetesService

router = APIRouter(prefix="/clusters/{cluster_id}/custom-workflows", tags=["custom workflows"])


class Confirmation(BaseModel):
    confirm: str


class Run(Confirmation):
    review_id: uuid.UUID


@router.get("")
def inventory(
    cluster_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    service.cluster(principal, cluster_id)
    response.headers["Cache-Control"] = "no-store"
    return [
        workflows.public(row)
        for row in service.session.scalars(
            select(CustomWorkflow)
            .where(CustomWorkflow.cluster_id == cluster_id)
            .order_by(CustomWorkflow.name)
            .limit(100)
        )
    ]


@router.post("", status_code=201)
def create(
    cluster_id: uuid.UUID,
    body: workflows.WorkflowInput,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    return workflows.save(service, principal, cluster_id, body)


@router.put("/{workflow_id}")
def update(
    cluster_id: uuid.UUID,
    workflow_id: uuid.UUID,
    body: workflows.WorkflowInput,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    return workflows.save(service, principal, cluster_id, body, workflow_id)


@router.post("/{workflow_id}/reveal")
def reveal(
    cluster_id: uuid.UUID,
    workflow_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    response.headers["Cache-Control"] = "no-store"
    return workflows.reveal(service, principal, cluster_id, workflow_id)


@router.post("/{workflow_id}/forget")
def forget(
    cluster_id: uuid.UUID,
    workflow_id: uuid.UUID,
    body: Confirmation,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    return workflows.forget(service, principal, cluster_id, workflow_id, body.confirm)


@router.post("/{workflow_id}/review")
def review(
    cluster_id: uuid.UUID,
    workflow_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    response.headers["Cache-Control"] = "no-store"
    return workflows.preview(service, principal, cluster_id, workflow_id)


@router.post("/{workflow_id}/run", status_code=202)
def run(
    cluster_id: uuid.UUID,
    workflow_id: uuid.UUID,
    body: Run,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    workflows.find(service, principal, cluster_id, workflow_id)
    row = service.session.get(CustomResourceReview, body.review_id)
    if not row or row.specification.get("summary", {}).get("workflow_id") != str(workflow_id):
        from app.core.errors import NotFoundError

        raise NotFoundError("Workflow review not found")
    return OperationDetail.model_validate(
        tasks.submit(
            service,
            principal,
            cluster_id,
            body.review_id,
            body.confirm,
            "custom_workflow",
            "cluster.custom_workflow",
            lambda r: len(r.specification["summary"]["steps"]),
        )
    )


@router.get("/{workflow_id}/history")
def history(
    cluster_id: uuid.UUID,
    workflow_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    workflows.find(service, principal, cluster_id, workflow_id)
    response.headers["Cache-Control"] = "no-store"
    rows = service.session.scalars(
        select(CustomResourceReview)
        .where(
            CustomResourceReview.cluster_id == cluster_id,
            CustomResourceReview.specification["category"].as_string() == "custom_workflow",
            CustomResourceReview.specification["summary"]["workflow_id"].as_string() == str(workflow_id),
        )
        .order_by(CustomResourceReview.created_at.desc())
        .limit(20)
    )
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "summary": r.specification["summary"],
            "operation_id": r.specification.get("operation_id"),
        }
        for r in rows
    ]

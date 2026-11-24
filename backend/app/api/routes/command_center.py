"""Author-bound command reviews, execution and private output."""

import uuid

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from app.api.auth import get_principal
from app.api.deps import get_kubernetes_service
from app.api.routes.operations import OperationDetail
from app.core.principal import Principal
from app.modules.command_center import service as commands
from app.modules.kubernetes import reviewed_operations as tasks
from app.modules.kubernetes.service import KubernetesService

router = APIRouter(prefix="/clusters/{cluster_id}/commands", tags=["command center"])


class Run(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: uuid.UUID
    confirm: str


@router.post("/reviews", status_code=201)
def review(
    cluster_id: uuid.UUID,
    body: commands.CommandInput,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    response.headers["Cache-Control"] = "no-store"
    return commands.review(service, principal, cluster_id, body)


@router.post("/run", status_code=202)
def run(
    cluster_id: uuid.UUID,
    body: Run,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    row = commands.find(service, principal, cluster_id, body.review_id)
    return OperationDetail.model_validate(
        tasks.submit(
            service,
            principal,
            cluster_id,
            body.review_id,
            body.confirm,
            commands.CATEGORY,
            "cluster.command" if row.specification["summary"]["read_only"] else "cluster.command.mutate",
            lambda _: 1,
        )
    )


@router.get("/history")
def history(
    cluster_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    response.headers["Cache-Control"] = "no-store"
    return commands.history(service, principal, cluster_id)


@router.get("/{review_id}/output")
def output(
    cluster_id: uuid.UUID,
    review_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
):
    response.headers["Cache-Control"] = "no-store"
    return commands.output(service, principal, cluster_id, review_id)

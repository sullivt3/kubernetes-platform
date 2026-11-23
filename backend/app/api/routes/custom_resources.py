"""Scoped CRD inventory and permission-checked, reviewed custom-resource CRUD."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from fastapi import Path as APIPath
from pydantic import BaseModel, Field

from app.api.auth import get_principal
from app.api.deps import get_kubernetes_service
from app.core.errors import ValidationFailedError
from app.core.principal import Principal
from app.modules.kubernetes.custom_resources import (
    DNS,
    NAME,
    VERSION,
    CustomResources,
    ExecuteInput,
    ReviewInput,
    Target,
)
from app.modules.kubernetes.definition_lifecycle import DefinitionLifecycle, DefinitionReviewInput, DefinitionTarget
from app.modules.kubernetes.service import KubernetesService

router = APIRouter(prefix="/clusters/{cluster_id}/custom-resources", tags=["custom resources"])


def custom_service(kubernetes: KubernetesService = Depends(get_kubernetes_service)):
    return CustomResources(kubernetes)


def no_store(response):
    response.headers["Cache-Control"] = "no-store"


@router.get("/definitions")
def definitions(
    cluster_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: CustomResources = Depends(custom_service),
):
    no_store(response)
    return service.definitions(principal, cluster_id)


@router.get("/definitions/{name}")
def definition(
    cluster_id: uuid.UUID,
    name: Annotated[str, APIPath(pattern=NAME, max_length=253)],
    response: Response,
    version: str = Query(pattern=VERSION),
    namespace: str | None = Query(default=None, pattern=DNS, max_length=63),
    principal: Principal = Depends(get_principal),
    service: CustomResources = Depends(custom_service),
):
    no_store(response)
    return service.definition(principal, cluster_id, name, version, namespace)


@router.get("/definitions/{name}/resources")
def resources(
    cluster_id: uuid.UUID,
    name: Annotated[str, APIPath(pattern=NAME, max_length=253)],
    response: Response,
    version: str = Query(pattern=VERSION),
    namespace: str | None = Query(default=None, pattern=DNS, max_length=63),
    continue_token: str | None = Query(default=None, max_length=8192),
    principal: Principal = Depends(get_principal),
    service: CustomResources = Depends(custom_service),
):
    no_store(response)
    return service.listing(principal, cluster_id, name, version, namespace, continue_token)


@router.get("/resource")
def resource(
    cluster_id: uuid.UUID,
    response: Response,
    definition: str = Query(pattern=NAME, max_length=253),
    version: str = Query(pattern=VERSION),
    name: str = Query(pattern=NAME, max_length=253),
    namespace: str | None = Query(default=None, pattern=DNS, max_length=63),
    expected_uid: str | None = Query(default=None, max_length=128),
    principal: Principal = Depends(get_principal),
    service: CustomResources = Depends(custom_service),
):
    no_store(response)
    return service.detail(
        principal,
        cluster_id,
        Target(definition=definition, version=version, name=name, namespace=namespace, expected_uid=expected_uid),
    )


@router.post("/reveal")
def reveal(
    cluster_id: uuid.UUID,
    body: Target,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: CustomResources = Depends(custom_service),
):
    no_store(response)
    return service.reveal(principal, cluster_id, body)


@router.post("/review")
def review(
    cluster_id: uuid.UUID,
    body: ReviewInput,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: CustomResources = Depends(custom_service),
):
    no_store(response)
    return service.review(principal, cluster_id, body)


@router.post("/execute")
def execute(
    cluster_id: uuid.UUID,
    body: ExecuteInput,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: CustomResources = Depends(custom_service),
):
    no_store(response)
    return service.execute(principal, cluster_id, body)


class SchemaVersion(BaseModel):
    version: str = Field(pattern=VERSION)
    definition_uid: str | None = Field(default=None, max_length=128)


@router.post("/definitions/{name}/reveal")
def reveal_schema(
    cluster_id: uuid.UUID,
    name: Annotated[str, APIPath(pattern=NAME, max_length=253)],
    body: SchemaVersion,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: CustomResources = Depends(custom_service),
):
    no_store(response)
    return service.reveal_schema(principal, cluster_id, name, body.version, body.definition_uid)


def definition_service(kubernetes: KubernetesService = Depends(get_kubernetes_service)):
    return DefinitionLifecycle(kubernetes)


@router.post("/definitions/{name}/manifest/reveal")
def reveal_definition_manifest(
    cluster_id: uuid.UUID,
    name: Annotated[str, APIPath(pattern=NAME, max_length=253)],
    body: DefinitionTarget,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: DefinitionLifecycle = Depends(definition_service),
):
    no_store(response)
    if name != body.name:
        raise ValidationFailedError("Definition path and body names must match")
    return service.reveal_definition(principal, cluster_id, body)


@router.post("/definition-reviews")
def review_definition(
    cluster_id: uuid.UUID,
    body: DefinitionReviewInput,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: DefinitionLifecycle = Depends(definition_service),
):
    no_store(response)
    return service.review_definition(principal, cluster_id, body)


@router.post("/definition-execute")
def execute_definition(
    cluster_id: uuid.UUID,
    body: ExecuteInput,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: DefinitionLifecycle = Depends(definition_service),
):
    no_store(response)
    return service.execute_definition(principal, cluster_id, body)

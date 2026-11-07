"""Catalog of infrastructure providers, Kubernetes distributions and operation types."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.modules.cluster_lifecycle.distributions.registry import DistributionDescriptor, distribution_registry
from app.modules.cluster_lifecycle.versions import kubernetes_versions
from app.modules.providers.registry import ProviderDescriptor, provider_registry
from app.orchestration.runtime import load_workflows
from app.orchestration.workflow import workflow_registry

router = APIRouter(prefix="/catalog", tags=["catalog"])


class OperationTypeDescriptor(BaseModel):
    type: str
    title: str
    description: str
    destructive: bool
    user_startable: bool


@router.get("/providers", response_model=list[ProviderDescriptor])
def list_providers() -> list[ProviderDescriptor]:
    return provider_registry.describe_all()


@router.get("/distributions", response_model=list[DistributionDescriptor])
def list_distributions() -> list[DistributionDescriptor]:
    return distribution_registry.describe_all()


class KubernetesVersions(BaseModel):
    available: bool
    latest: str | None
    versions: list[dict[str, str]]
    minimum_minor: str
    message: str | None


@router.get("/kubernetes-versions", response_model=KubernetesVersions)
def list_kubernetes_versions() -> KubernetesVersions:
    """Currently supported upstream minor versions (from dl.k8s.io, cached for an hour)."""
    catalog = kubernetes_versions()
    return KubernetesVersions(**catalog.__dict__)


@router.get("/operation-types", response_model=list[OperationTypeDescriptor])
def list_operation_types() -> list[OperationTypeDescriptor]:
    load_workflows()
    return [
        OperationTypeDescriptor(
            type=w.type,
            title=w.title,
            description=w.description,
            destructive=w.destructive,
            user_startable=w.user_startable,
        )
        for w in workflow_registry.all()
    ]

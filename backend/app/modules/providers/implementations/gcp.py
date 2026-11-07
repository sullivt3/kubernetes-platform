"""Google Cloud provider: Compute Engine instances and networking."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.modules.credentials.models import CredentialKind
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.types import (
    ImplementationStatus,
    ProviderCapability,
    ProviderCategory,
    ProviderType,
)


class GcpConfig(BaseModel):
    project_id: str = Field(pattern=r"^[a-z][-a-z0-9]{4,28}[a-z0-9]$")
    region: str = Field(min_length=1, description="e.g. us-central1")
    zone: str | None = None
    network: str | None = None


class GcpProvider(InfrastructureProvider):
    provider_type = ProviderType.GCP
    display_name = "Google Cloud"
    description = "Compute Engine instances for self-managed clusters, and the project context for GKE."
    category = ProviderCategory.CLOUD
    capabilities = frozenset(set(ProviderCapability))
    implementation_status = ImplementationStatus.PLANNED
    planned_phase = 5
    config_model = GcpConfig
    credential_kinds = frozenset({CredentialKind.GCP_SERVICE_ACCOUNT})

"""Microsoft Azure provider: virtual machines and networking."""

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


class AzureConfig(BaseModel):
    subscription_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    resource_group: str = Field(min_length=1, max_length=90)
    location: str = Field(min_length=1, description="e.g. eastus")
    virtual_network: str | None = None


class AzureProvider(InfrastructureProvider):
    provider_type = ProviderType.AZURE
    display_name = "Microsoft Azure"
    description = "Azure virtual machines for self-managed clusters, and the subscription context for AKS."
    category = ProviderCategory.CLOUD
    capabilities = frozenset(set(ProviderCapability))
    implementation_status = ImplementationStatus.PLANNED
    planned_phase = 5
    config_model = AzureConfig
    credential_kinds = frozenset({CredentialKind.AZURE_SERVICE_PRINCIPAL})

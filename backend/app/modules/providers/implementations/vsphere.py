"""VMware vSphere provider: VM lifecycle via vCenter."""

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


class VsphereConfig(BaseModel):
    server: str = Field(min_length=1, description="vCenter hostname")
    datacenter: str = Field(min_length=1)
    cluster: str | None = None
    datastore: str | None = None
    network: str | None = None
    template: str | None = Field(default=None, description="VM template to clone")
    verify_tls: bool = True


class VsphereProvider(InfrastructureProvider):
    provider_type = ProviderType.VSPHERE
    display_name = "VMware vSphere"
    description = "Provision and manage virtual machines through VMware vCenter."
    category = ProviderCategory.VIRTUALIZATION
    capabilities = frozenset(set(ProviderCapability))
    implementation_status = ImplementationStatus.PLANNED
    planned_phase = 5
    config_model = VsphereConfig
    credential_kinds = frozenset({CredentialKind.VSPHERE_PASSWORD})

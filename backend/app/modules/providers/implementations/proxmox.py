"""Proxmox VE provider: VM lifecycle via the Proxmox REST API."""

from __future__ import annotations

from pydantic import AnyHttpUrl, BaseModel, Field

from app.modules.credentials.models import CredentialKind
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.types import (
    ImplementationStatus,
    ProviderCapability,
    ProviderCategory,
    ProviderType,
)


class ProxmoxConfig(BaseModel):
    api_url: AnyHttpUrl = Field(description="e.g. https://pve.example.internal:8006")
    node: str = Field(min_length=1, description="Default Proxmox node for new VMs")
    verify_tls: bool = True
    default_storage: str | None = Field(default=None, description="Storage pool for VM disks")
    default_bridge: str = Field(default="vmbr0", description="Network bridge for VM NICs")
    template_vmid: int | None = Field(default=None, ge=100, description="Cloud-init template to clone")


class ProxmoxProvider(InfrastructureProvider):
    provider_type = ProviderType.PROXMOX
    display_name = "Proxmox VE"
    description = "Provision and manage virtual machines on Proxmox Virtual Environment."
    category = ProviderCategory.VIRTUALIZATION
    capabilities = frozenset(set(ProviderCapability))
    implementation_status = ImplementationStatus.PLANNED
    planned_phase = 3
    config_model = ProxmoxConfig
    credential_kinds = frozenset({CredentialKind.PROXMOX_API_TOKEN})

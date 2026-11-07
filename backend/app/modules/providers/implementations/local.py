"""Local provider: machines on the operator workstation (container-based nodes for
Kind/k3d, or local hypervisor VMs)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.types import (
    ImplementationStatus,
    ProviderCapability,
    ProviderCategory,
    ProviderType,
)


class LocalConfig(BaseModel):
    runtime: Literal["docker", "podman"] = Field(
        default="docker", description="Container runtime used to host local cluster nodes"
    )


class LocalProvider(InfrastructureProvider):
    provider_type = ProviderType.LOCAL
    display_name = "Local"
    description = "Local development infrastructure (container-based nodes for Kind / k3d)."
    category = ProviderCategory.LOCAL
    capabilities = frozenset(
        {
            ProviderCapability.VALIDATE_CONNECTION,
            ProviderCapability.DISCOVER,
            ProviderCapability.STATUS,
        }
    )
    implementation_status = ImplementationStatus.PLANNED
    planned_phase = 3
    config_model = LocalConfig
    credential_required = False

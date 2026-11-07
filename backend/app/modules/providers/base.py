"""The infrastructure provider contract.

A provider manages the *lifecycle of machines* on some substrate (SSH-reachable
hosts, Proxmox, AWS...). It does not install software on machines — in-guest work
goes through :mod:`app.modules.infrastructure.host_access` — and it knows nothing
about Kubernetes.

Rules for implementations
-------------------------
* Methods are synchronous and may block; they are only ever called from
  orchestration workflow steps, never from API request handlers.
* Every mutating method must be idempotent: calling ``provision`` twice for the
  same ``MachineSpecRequest.name`` must not create two machines, ``delete`` of a
  machine that is already gone must succeed. Workflows rely on this for resume.
* Declare every supported operation in ``capabilities``. Any method left at the
  base-class default raises :class:`CapabilityNotSupportedError`; a test enforces
  that declared capabilities of AVAILABLE providers are actually overridden.
* Credentials arrive already decrypted via ``credential``; never log them.
"""

from __future__ import annotations

from abc import ABC
from typing import ClassVar

from pydantic import BaseModel

from app.core.errors import CapabilityNotSupportedError
from app.modules.credentials.models import CredentialKind
from app.modules.infrastructure.host_access import HostEndpoint
from app.modules.providers.types import (
    ConnectionCheckResult,
    DiscoveredMachine,
    HardwareSpec,
    ImplementationStatus,
    MachineRef,
    MachineSpecRequest,
    MachineStatus,
    NetworkConfigRequest,
    PowerAction,
    ProviderCapability,
    ProviderCategory,
    ProviderType,
)

# Maps capability -> the method that implements it (used by validation and tests).
CAPABILITY_METHODS: dict[ProviderCapability, str] = {
    ProviderCapability.VALIDATE_CONNECTION: "validate_connection",
    ProviderCapability.DISCOVER: "discover",
    ProviderCapability.PROVISION: "provision",
    ProviderCapability.DELETE: "delete",
    ProviderCapability.STATUS: "get_status",
    ProviderCapability.SPECIFICATIONS: "get_specifications",
    ProviderCapability.POWER: "set_power_state",
    ProviderCapability.NETWORK: "configure_network",
    ProviderCapability.REGISTER_EXISTING: "connection_endpoint",
}


class EmptyConfig(BaseModel):
    pass


class InfrastructureProvider(ABC):
    # ---- Static metadata (shown in the catalog and used by the cluster wizard) ----
    provider_type: ClassVar[ProviderType]
    display_name: ClassVar[str]
    description: ClassVar[str]
    category: ClassVar[ProviderCategory]
    capabilities: ClassVar[frozenset[ProviderCapability]]
    implementation_status: ClassVar[ImplementationStatus]
    planned_phase: ClassVar[int | None] = None
    config_model: ClassVar[type[BaseModel]] = EmptyConfig
    credential_kinds: ClassVar[frozenset[CredentialKind]] = frozenset()
    credential_required: ClassVar[bool] = True

    def __init__(self, config: BaseModel, credential: bytes | None) -> None:
        self.config = config
        self._credential = credential

    # ---- Operations (override the ones listed in ``capabilities``) ---------------

    def _unsupported(self, capability: ProviderCapability) -> CapabilityNotSupportedError:
        return CapabilityNotSupportedError(
            f"Provider '{self.provider_type}' does not support '{capability}'",
            details={"provider": self.provider_type.value, "capability": capability.value},
        )

    def validate_connection(self) -> ConnectionCheckResult:
        raise self._unsupported(ProviderCapability.VALIDATE_CONNECTION)

    def discover(self) -> list[DiscoveredMachine]:
        raise self._unsupported(ProviderCapability.DISCOVER)

    def provision(self, request: MachineSpecRequest) -> DiscoveredMachine:
        raise self._unsupported(ProviderCapability.PROVISION)

    def delete(self, machine: MachineRef) -> None:
        raise self._unsupported(ProviderCapability.DELETE)

    def get_status(self, machine: MachineRef) -> MachineStatus:
        raise self._unsupported(ProviderCapability.STATUS)

    def get_specifications(self, machine: MachineRef) -> HardwareSpec:
        raise self._unsupported(ProviderCapability.SPECIFICATIONS)

    def set_power_state(self, machine: MachineRef, action: PowerAction) -> MachineStatus:
        raise self._unsupported(ProviderCapability.POWER)

    def configure_network(self, machine: MachineRef, request: NetworkConfigRequest) -> None:
        raise self._unsupported(ProviderCapability.NETWORK)

    def connection_endpoint(self, host: str, port: int | None, username: str | None) -> HostEndpoint:
        """How to reach an existing machine registered by address (defaults applied)."""
        raise self._unsupported(ProviderCapability.REGISTER_EXISTING)

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release connections/sessions held by the provider."""

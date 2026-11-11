"""SSH provider: existing physical or virtual Linux servers reached over SSH.

It cannot create, delete or power-cycle machines; it registers servers that already
exist. In-guest work (inspection, OS preparation, Kubernetes installation) goes
through the host-access layer using each machine's own credential.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.modules.credentials.models import CredentialKind
from app.modules.infrastructure.host_access import HostEndpoint
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.types import (
    ImplementationStatus,
    ProviderCapability,
    ProviderCategory,
    ProviderType,
)


class SshConfig(BaseModel):
    default_username: str = Field(default="root", min_length=1, max_length=64)
    default_port: int = Field(default=22, ge=1, le=65535)


class SshProvider(InfrastructureProvider):
    provider_type = ProviderType.SSH
    display_name = "SSH / Linux servers"
    description = (
        "Existing physical or virtual Linux servers (Ubuntu, Debian, RHEL, Rocky, AlmaLinux, Fedora) "
        "registered by address and managed over SSH."
    )
    category = ProviderCategory.PHYSICAL
    capabilities = frozenset({ProviderCapability.REGISTER_EXISTING})
    implementation_status = ImplementationStatus.AVAILABLE
    config_model = SshConfig
    credential_kinds = frozenset({CredentialKind.SSH_PRIVATE_KEY, CredentialKind.SSH_PASSWORD})
    # Credentials are attached per machine, not to the provider connection.
    credential_required = False

    def connection_endpoint(self, host: str, port: int | None, username: str | None) -> HostEndpoint:
        config: SshConfig = self.config  # type: ignore[assignment]
        return HostEndpoint(host=host, port=port or config.default_port, username=username or config.default_username)

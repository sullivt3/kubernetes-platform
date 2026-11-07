"""Concrete provider implementations. Each lives in its own module and is isolated
from the others; the only shared surface is :class:`InfrastructureProvider`.

In Phase 0 every provider is catalogued with its configuration schema and
intended capabilities, and is marked ``PLANNED``. ``ProviderRegistry.create``
refuses to instantiate planned providers, so nothing can pretend to provision.
"""

from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.implementations.aws import AwsProvider
from app.modules.providers.implementations.azure import AzureProvider
from app.modules.providers.implementations.gcp import GcpProvider
from app.modules.providers.implementations.local import LocalProvider
from app.modules.providers.implementations.proxmox import ProxmoxProvider
from app.modules.providers.implementations.ssh import SshProvider
from app.modules.providers.implementations.vsphere import VsphereProvider

ALL_PROVIDERS: list[type[InfrastructureProvider]] = [
    LocalProvider,
    SshProvider,
    ProxmoxProvider,
    VsphereProvider,
    AwsProvider,
    AzureProvider,
    GcpProvider,
]

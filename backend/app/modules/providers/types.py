"""Provider-neutral vocabulary shared by every infrastructure provider."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ProviderType(StrEnum):
    LOCAL = "local"
    SSH = "ssh"
    PROXMOX = "proxmox"
    VSPHERE = "vsphere"
    AWS = "aws"
    AZURE = "azure"
    GCP = "gcp"


class ProviderCategory(StrEnum):
    LOCAL = "local"
    PHYSICAL = "physical"
    VIRTUALIZATION = "virtualization"
    CLOUD = "cloud"


class ProviderCapability(StrEnum):
    VALIDATE_CONNECTION = "validate_connection"
    DISCOVER = "discover"  # enumerate existing machines
    PROVISION = "provision"  # create new machines
    DELETE = "delete"  # destroy machines
    STATUS = "status"  # query lifecycle/power state
    SPECIFICATIONS = "specifications"  # hardware/sizing details
    POWER = "power"  # start/stop/reboot
    NETWORK = "network"  # configure networks/addresses
    REGISTER_EXISTING = "register_existing"  # add machines that already exist (by address)
    LOAD_BALANCER = "load_balancer"  # manage a Kubernetes API load balancer
    MANAGED_KUBERNETES = "managed_kubernetes"  # create/delete provider-managed Kubernetes clusters


class ImplementationStatus(StrEnum):
    AVAILABLE = "available"
    PREVIEW = "preview"
    PLANNED = "planned"  # in the catalog, not implemented — must never pretend to work


class PowerAction(StrEnum):
    START = "start"
    STOP = "stop"
    REBOOT = "reboot"


# ---- Data exchanged with providers ----------------------------------------------


class MachineRef(BaseModel):
    external_id: str


class DiskInfo(BaseModel):
    name: str
    size_gb: float
    type: str | None = None


class HardwareSpec(BaseModel):
    cpu_cores: int | None = None
    memory_mb: int | None = None
    architecture: str | None = None
    disks: list[DiskInfo] = Field(default_factory=list)


class OSInfo(BaseModel):
    family: str | None = None  # debian, rhel, ...
    distribution: str | None = None  # ubuntu, rocky, ...
    version: str | None = None
    kernel: str | None = None


class MachineAddress(BaseModel):
    type: str  # internal | external
    address: str


class Endpoint(BaseModel):
    host: str
    port: int


class DiscoveredMachine(BaseModel):
    external_id: str
    name: str
    addresses: list[MachineAddress] = Field(default_factory=list)
    power_state: str = "unknown"
    hardware: HardwareSpec | None = None
    os: OSInfo | None = None
    provider_metadata: dict[str, str] = Field(default_factory=dict)
    # How the platform reaches the machine's SSH server when that differs from its
    # internal address (e.g. a port published on the workstation for local nodes).
    ssh_endpoint: Endpoint | None = None
    # How the platform reaches a Kubernetes API server on this machine, if it differs
    # from <internal address>:6443 (used to configure cluster connections).
    kube_api_endpoint: Endpoint | None = None
    # Machines the provider can resolve to an SSH-ready guest (not templates, not cloud
    # instances discovered read-only).
    importable: bool = True


class MachineSpecRequest(BaseModel):
    """Provider-neutral request for a new machine. Providers map it to native sizing."""

    name: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
    cpu_cores: int = Field(ge=1, le=512)
    memory_mb: int = Field(ge=512)
    disk_gb: int = Field(ge=10)
    image: str | None = None  # provider-specific template/AMI/image reference
    network: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    provider_options: dict[str, object] = Field(default_factory=dict)
    # Access injected into the new machine (cloud-init / container env). Public key only.
    ssh_username: str = Field(default="platform", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    ssh_public_key: str | None = None


class MachineStatus(BaseModel):
    external_id: str
    exists: bool
    power_state: str
    addresses: list[MachineAddress] = Field(default_factory=list)
    ssh_endpoint: Endpoint | None = None
    kube_api_endpoint: Endpoint | None = None


class NetworkConfigRequest(BaseModel):
    network: str
    addresses: list[str] = Field(default_factory=list)
    options: dict[str, object] = Field(default_factory=dict)


class LoadBalancerRequest(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9-]{1,32}$")
    vpc_id: str
    subnet_ids: list[str] = Field(min_length=1, max_length=16)
    targets: list[str] = Field(min_length=1)
    scheme: str = "internal"


class LoadBalancerResult(BaseModel):
    external_id: str
    endpoint: str


class ManagedClusterRequest(BaseModel):
    name: str
    version: str
    options: dict[str, object] = Field(default_factory=dict)
    node_pools: list[dict[str, object]] = Field(default_factory=list)


class ManagedClusterResult(BaseModel):
    external_id: str
    endpoint: str
    certificate_authority_data: str
    client_certificate_data: str | None = None
    client_key_data: str | None = None


class ConnectionCheckResult(BaseModel):
    ok: bool
    message: str
    details: dict[str, str] = Field(default_factory=dict)

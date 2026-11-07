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


class DiscoveredMachine(BaseModel):
    external_id: str
    name: str
    addresses: list[MachineAddress] = Field(default_factory=list)
    power_state: str = "unknown"
    hardware: HardwareSpec | None = None
    os: OSInfo | None = None
    provider_metadata: dict[str, str] = Field(default_factory=dict)


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


class MachineStatus(BaseModel):
    external_id: str
    exists: bool
    power_state: str
    addresses: list[MachineAddress] = Field(default_factory=list)


class NetworkConfigRequest(BaseModel):
    network: str
    addresses: list[str] = Field(default_factory=list)
    options: dict[str, object] = Field(default_factory=dict)


class ConnectionCheckResult(BaseModel):
    ok: bool
    message: str
    details: dict[str, str] = Field(default_factory=dict)

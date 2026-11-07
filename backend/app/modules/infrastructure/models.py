"""Infrastructure inventory: provider connections and machines.

This module knows NOTHING about Kubernetes. A machine is a compute resource with
an operating system that the platform can reach; which cluster (if any) it serves
is recorded by the cluster_lifecycle module (``ClusterMachine``). Keeping this
boundary lets the same machine inventory feed kubeadm, K3s, RKE2 or nothing at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonDict, JsonList, OrganizationScoped, Timestamps, UUIDPrimaryKey, str_enum
from app.modules.providers.types import ProviderType


class ConnectionStatus(StrEnum):
    UNKNOWN = "unknown"
    CONNECTED = "connected"
    ERROR = "error"


class ProviderConnection(UUIDPrimaryKey, Timestamps, OrganizationScoped, Base):
    """A configured instance of an infrastructure provider (e.g. "Proxmox lab", "AWS us-east-1")."""

    __tablename__ = "provider_connections"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    name: Mapped[str] = mapped_column(String(128))
    provider_type: Mapped[ProviderType] = mapped_column(str_enum(ProviderType))
    # Non-secret configuration, validated against the provider's config schema.
    config: Mapped[JsonDict] = mapped_column(default=dict)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credentials.id", ondelete="RESTRICT"))
    status: Mapped[ConnectionStatus] = mapped_column(
        str_enum(ConnectionStatus), default=ConnectionStatus.UNKNOWN
    )
    status_message: Mapped[str | None] = mapped_column(Text)
    last_validated_at: Mapped[datetime | None]


class MachineLifecycleState(StrEnum):
    REQUESTED = "requested"  # recorded, provisioning not started
    PROVISIONING = "provisioning"
    READY = "ready"  # exists and is reachable
    UNREACHABLE = "unreachable"
    ERROR = "error"
    DEPROVISIONING = "deprovisioning"
    DELETED = "deleted"


class PowerState(StrEnum):
    UNKNOWN = "unknown"
    RUNNING = "running"
    STOPPED = "stopped"
    SUSPENDED = "suspended"


class Machine(UUIDPrimaryKey, Timestamps, OrganizationScoped, Base):
    """A physical server, VM or cloud instance under platform management."""

    __tablename__ = "machines"
    __table_args__ = (UniqueConstraint("provider_connection_id", "external_id"),)

    name: Mapped[str] = mapped_column(String(255))
    provider_connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_connections.id", ondelete="RESTRICT"), index=True
    )
    # Identifier within the provider (VMID, instance ID, hostname for SSH hosts...).
    external_id: Mapped[str] = mapped_column(String(255))
    lifecycle_state: Mapped[MachineLifecycleState] = mapped_column(
        str_enum(MachineLifecycleState), default=MachineLifecycleState.REQUESTED
    )
    power_state: Mapped[PowerState] = mapped_column(str_enum(PowerState), default=PowerState.UNKNOWN)

    # [{"type": "internal"|"external", "address": "10.0.0.5"}]
    addresses: Mapped[JsonList] = mapped_column(default=list)
    ssh_host: Mapped[str | None] = mapped_column(String(255))
    ssh_port: Mapped[int] = mapped_column(default=22)
    ssh_username: Mapped[str | None] = mapped_column(String(64))
    access_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="RESTRICT")
    )

    # Pinned SSH host key (trust-on-first-use, then strictly verified).
    host_key_type: Mapped[str | None] = mapped_column(String(64))
    host_key_data: Mapped[str | None] = mapped_column(Text)
    host_key_fingerprint: Mapped[str | None] = mapped_column(String(128))

    # Observed facts, refreshed by inspection operations.
    os_info: Mapped[JsonDict] = mapped_column(default=dict)
    hardware: Mapped[JsonDict] = mapped_column(default=dict)
    # Prerequisite checks: [{"key", "label", "status": pass|warn|fail, "message"}]
    checks: Mapped[JsonList] = mapped_column(default=list)
    labels: Mapped[JsonDict] = mapped_column(default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime | None]
    inspected_at: Mapped[datetime | None]

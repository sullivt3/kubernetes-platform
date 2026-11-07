"""Cluster registration and the binding between clusters and infrastructure machines.

``Cluster`` records what the platform knows about a cluster it created or
imported: desired spec, observed status and how to reach it. Live Kubernetes
state (nodes, pods, ...) is NOT mirrored here — it is read from the cluster's API
by the kubernetes module, so the database never serves stale cluster data as if
it were live.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonDict, OrganizationScoped, Timestamps, UUIDPrimaryKey, str_enum
from app.modules.cluster_lifecycle.distributions.types import DistributionType


class ClusterOrigin(StrEnum):
    IMPORTED = "imported"  # existing cluster registered via kubeconfig
    PROVISIONED = "provisioned"  # created by this platform


class ClusterLifecycleState(StrEnum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    UPGRADING = "upgrading"
    DELETING = "deleting"
    DELETED = "deleted"
    ERROR = "error"


class Cluster(UUIDPrimaryKey, Timestamps, OrganizationScoped, Base):
    __tablename__ = "clusters"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    name: Mapped[str] = mapped_column(String(63))
    display_name: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    distribution: Mapped[DistributionType] = mapped_column(str_enum(DistributionType))
    origin: Mapped[ClusterOrigin] = mapped_column(str_enum(ClusterOrigin, 16))
    lifecycle_state: Mapped[ClusterLifecycleState] = mapped_column(
        str_enum(ClusterLifecycleState), default=ClusterLifecycleState.PENDING
    )
    kubernetes_version: Mapped[str | None] = mapped_column(String(32))
    api_endpoint: Mapped[str | None] = mapped_column(String(512))

    # Kubeconfig (or managed-service credential) used by the platform to reach the API.
    # Stored encrypted in ``credentials``; never sent to the browser.
    access_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="RESTRICT")
    )
    # Infrastructure the cluster runs on (null for imported clusters of unknown origin).
    provider_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_connections.id", ondelete="RESTRICT")
    )

    spec: Mapped[JsonDict] = mapped_column(default=dict)  # desired configuration (ClusterSpec)
    status_detail: Mapped[JsonDict] = mapped_column(default=dict)  # last observed health summary
    last_contact_at: Mapped[datetime | None]


class NodeRole(StrEnum):
    CONTROL_PLANE = "control_plane"
    WORKER = "worker"


class MachineMembershipState(StrEnum):
    PLANNED = "planned"
    INSTALLING = "installing"
    JOINED = "joined"
    FAILED = "failed"
    REMOVING = "removing"


class ClusterMachine(UUIDPrimaryKey, Timestamps, Base):
    """Assignment of a managed infrastructure machine to a cluster role.

    A machine can belong to at most one cluster (enforced by the unique constraint).
    Nodes of managed services (EKS/AKS/GKE) or imported clusters are not represented
    here; they are read live from the Kubernetes API.
    """

    __tablename__ = "cluster_machines"
    __table_args__ = (UniqueConstraint("machine_id"),)

    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), index=True)
    machine_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("machines.id", ondelete="RESTRICT"))
    role: Mapped[NodeRole] = mapped_column(str_enum(NodeRole, 16))
    node_name: Mapped[str | None] = mapped_column(String(253))
    membership_state: Mapped[MachineMembershipState] = mapped_column(
        str_enum(MachineMembershipState, 16), default=MachineMembershipState.PLANNED
    )

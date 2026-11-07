"""Desired-state specification of a cluster, as produced by the creation wizard.

The spec is distribution-neutral. Distribution-specific settings live in
``options`` and are validated by the chosen distribution's ``options_model``.
"""

from __future__ import annotations

import ipaddress
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.modules.cluster_lifecycle.distributions.types import CniPlugin, ContainerRuntime, DistributionType
from app.modules.providers.types import ProviderType

DNS_LABEL = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"
K8S_VERSION = r"^v?1\.\d{1,2}(\.\d{1,3})?$"
LABEL_KEY = r"^([a-z0-9]([-a-z0-9.]*[a-z0-9])?/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$"


class TaintEffect(StrEnum):
    NO_SCHEDULE = "NoSchedule"
    PREFER_NO_SCHEDULE = "PreferNoSchedule"
    NO_EXECUTE = "NoExecute"


class Taint(BaseModel):
    key: str = Field(pattern=LABEL_KEY)
    value: str | None = Field(default=None, max_length=63)
    effect: TaintEffect


class MachineTemplate(BaseModel):
    """Sizing for machines the platform must provision (when not using existing machines)."""

    cpu_cores: int = Field(ge=1, le=512)
    memory_mb: int = Field(ge=1024)
    disk_gb: int = Field(ge=20)
    image: str | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)


class NodePoolSpec(BaseModel):
    name: str = Field(pattern=DNS_LABEL)
    count: int = Field(ge=0, le=1000)
    existing_machine_ids: list[uuid.UUID] = Field(default_factory=list)
    machine_template: MachineTemplate | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    taints: list[Taint] = Field(default_factory=list)

    @field_validator("labels")
    @classmethod
    def _labels_valid(cls, labels: dict[str, str]) -> dict[str, str]:
        import re

        for key, value in labels.items():
            if not re.match(LABEL_KEY, key):
                raise ValueError(f"Invalid label key '{key}'")
            if len(value) > 63:
                raise ValueError(f"Label value for '{key}' exceeds 63 characters")
        return labels

    @model_validator(mode="after")
    def _machines_cover_count(self) -> NodePoolSpec:
        if len(set(self.existing_machine_ids)) != len(self.existing_machine_ids):
            raise ValueError(f"Node pool '{self.name}' lists the same machine more than once")
        missing = self.count - len(self.existing_machine_ids)
        if missing < 0:
            raise ValueError(f"Node pool '{self.name}' lists more machines than its count")
        if missing > 0 and self.machine_template is None:
            raise ValueError(
                f"Node pool '{self.name}' needs {missing} more machine(s): select existing machines "
                "or provide a machine_template to provision them"
            )
        return self


class LoadBalancerMode(StrEnum):
    NONE = "none"  # single control plane, API served by the node itself
    KUBE_VIP = "kube-vip"  # virtual IP managed inside the cluster
    EXTERNAL = "external"  # user-provided load balancer / DNS name
    PROVIDER = "provider"  # cloud load balancer created by the provider


class ControlPlaneSpec(NodePoolSpec):
    name: str = Field(default="control-plane", pattern=DNS_LABEL)
    count: int = Field(default=1, ge=0, le=7)
    endpoint: str | None = Field(default=None, description="Stable API endpoint (VIP or DNS name)")
    load_balancer: LoadBalancerMode = LoadBalancerMode.NONE

    @model_validator(mode="after")
    def _ha_rules(self) -> ControlPlaneSpec:
        if self.count > 1:
            if self.count % 2 == 0:
                raise ValueError("Highly available control planes need an odd member count (3, 5 or 7)")
            if self.load_balancer is LoadBalancerMode.NONE:
                raise ValueError("Multiple control-plane nodes require a load balancer mode for the API")
            if self.load_balancer in (LoadBalancerMode.KUBE_VIP, LoadBalancerMode.EXTERNAL) and not self.endpoint:
                raise ValueError("A stable API endpoint is required for kube-vip or external load balancing")
        return self


class NetworkSpec(BaseModel):
    pod_cidr: str = "10.244.0.0/16"
    service_cidr: str = "10.96.0.0/12"
    dns_domain: str = Field(default="cluster.local", pattern=r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
    cni: CniPlugin = CniPlugin.CALICO

    @field_validator("pod_cidr", "service_cidr")
    @classmethod
    def _cidr(cls, value: str) -> str:
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError(f"'{value}' is not a valid network CIDR") from exc
        if network.version == 4 and network.prefixlen > 24:
            raise ValueError(f"'{value}' is too small for cluster networking (use /24 or larger)")
        return str(network)

    @model_validator(mode="after")
    def _no_overlap(self) -> NetworkSpec:
        pod = ipaddress.ip_network(self.pod_cidr)
        svc = ipaddress.ip_network(self.service_cidr)
        if pod.version == svc.version and pod.overlaps(svc):
            raise ValueError("Pod CIDR and service CIDR must not overlap")
        return self


class StorageSpec(BaseModel):
    default_storage_class: str | None = Field(default=None, pattern=DNS_LABEL)
    csi_drivers: list[str] = Field(default_factory=list)


class ClusterSpec(BaseModel):
    name: str = Field(pattern=DNS_LABEL)
    distribution: DistributionType
    provider_type: ProviderType
    provider_connection_id: uuid.UUID | None = None
    kubernetes_version: str = Field(pattern=K8S_VERSION)
    container_runtime: ContainerRuntime = ContainerRuntime.CONTAINERD
    network: NetworkSpec = Field(default_factory=NetworkSpec)
    control_plane: ControlPlaneSpec
    worker_pools: list[NodePoolSpec] = Field(default_factory=list)
    storage: StorageSpec = Field(default_factory=StorageSpec)
    options: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["create"] = "create"

    @model_validator(mode="after")
    def _unique_pools_and_machines(self) -> ClusterSpec:
        names = [self.control_plane.name, *(p.name for p in self.worker_pools)]
        if len(names) != len(set(names)):
            raise ValueError("Node pool names must be unique")
        machines = [m for pool in (self.control_plane, *self.worker_pools) for m in pool.existing_machine_ids]
        if len(machines) != len(set(machines)):
            raise ValueError("A machine can only be assigned to one node pool")
        return self

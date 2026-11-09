"""Distribution catalog.

Each distribution declares its supported infrastructure, runtimes, CNIs and
capabilities. Implemented distributions live in their own package (``kubeadm/``)
with their automation alongside; the rest are ``PLANNED`` and their ``plan_*``
methods raise ``NotImplementedYetError``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.modules.cluster_lifecycle.distributions.base import KubernetesDistribution
from app.modules.cluster_lifecycle.distributions.kubeadm import Kubeadm
from app.modules.cluster_lifecycle.distributions.types import (
    CniPlugin,
    DistributionKind,
    DistributionType,
)
from app.modules.cluster_lifecycle.distributions.types import (
    DistributionCapability as Cap,
)
from app.modules.providers.types import ImplementationStatus, ProviderType
from app.orchestration.workflow import StepDefinition


class EksOptions(BaseModel):
    managed_cluster_role_arn: str = Field(pattern=r"^arn:aws:iam::\d{12}:role/.+$")
    managed_node_role_arn: str = Field(pattern=r"^arn:aws:iam::\d{12}:role/.+$")
    managed_subnet_ids: list[str] = Field(min_length=2)
    managed_security_group_ids: list[str] = Field(default_factory=list)
    managed_node_instance_types: list[str] = Field(default_factory=lambda: ["t3.medium"], min_length=1)
    managed_node_disk_size_gb: int = Field(default=20, ge=20, le=2000)
    managed_min_node_count: int = Field(default=1, ge=0, le=1000)
    managed_max_node_count: int = Field(default=10, ge=1, le=1000)
    managed_endpoint_public_access: bool = True
    managed_endpoint_private_access: bool = False

    @model_validator(mode="after")
    def _node_bounds(self) -> EksOptions:
        if self.managed_min_node_count > self.managed_max_node_count:
            raise ValueError("managed_min_node_count cannot exceed managed_max_node_count")
        return self


class AksOptions(BaseModel):
    managed_resource_group: str = Field(min_length=1, max_length=90)
    managed_location: str = Field(min_length=1, max_length=64)
    managed_subnet_id: str = Field(min_length=1)
    managed_node_vm_size: str = Field(default="Standard_D2s_v5", min_length=1, max_length=64)


class GkeOptions(BaseModel):
    managed_location: str = Field(min_length=1, max_length=64)
    managed_network: str = Field(min_length=1)
    managed_subnetwork: str = Field(min_length=1)
    managed_node_machine_type: str = Field(default="e2-standard-2", min_length=1, max_length=64)


_VM_PROVIDERS = frozenset(
    {
        ProviderType.SSH,
        ProviderType.PROXMOX,
        ProviderType.VSPHERE,
        ProviderType.AWS,
        ProviderType.AZURE,
        ProviderType.GCP,
    }
)
_SELF_MANAGED_CAPS = frozenset(
    {Cap.CREATE, Cap.IMPORT, Cap.ADD_NODES, Cap.REMOVE_NODES, Cap.UPGRADE, Cap.DELETE, Cap.HIGH_AVAILABILITY}
)
_MANAGED_CAPS = frozenset({Cap.CREATE, Cap.IMPORT, Cap.ADD_NODES, Cap.REMOVE_NODES, Cap.UPGRADE, Cap.DELETE})
_LOCAL_CAPS = frozenset({Cap.CREATE, Cap.IMPORT, Cap.DELETE})


class K3s(KubernetesDistribution):
    distribution_type = DistributionType.K3S
    display_name = "K3s"
    description = "Lightweight certified Kubernetes distribution, well suited to edge and small clusters."
    kind = DistributionKind.SELF_MANAGED
    capabilities = _SELF_MANAGED_CAPS
    implementation_status = ImplementationStatus.PLANNED
    planned_phase = 5
    supported_providers = _VM_PROVIDERS
    supported_cnis = frozenset({CniPlugin.FLANNEL, CniPlugin.CALICO, CniPlugin.CILIUM})
    default_cni = CniPlugin.FLANNEL


class Rke2(KubernetesDistribution):
    distribution_type = DistributionType.RKE2
    display_name = "RKE2"
    description = "Security-focused Kubernetes distribution with CIS-hardened defaults."
    kind = DistributionKind.SELF_MANAGED
    capabilities = _SELF_MANAGED_CAPS
    implementation_status = ImplementationStatus.PLANNED
    planned_phase = 5
    supported_providers = _VM_PROVIDERS
    supported_cnis = frozenset({CniPlugin.CANAL, CniPlugin.CALICO, CniPlugin.CILIUM})
    default_cni = CniPlugin.CANAL


class Kind(KubernetesDistribution):
    distribution_type = DistributionType.KIND
    display_name = "Kind"
    description = "Kubernetes-in-Docker clusters for local development and testing."
    kind = DistributionKind.LOCAL
    capabilities = _LOCAL_CAPS | {Cap.HIGH_AVAILABILITY}
    implementation_status = ImplementationStatus.PLANNED
    planned_phase = 4
    supported_providers = frozenset({ProviderType.LOCAL})
    supported_cnis = frozenset({CniPlugin.PROVIDER_DEFAULT, CniPlugin.CALICO, CniPlugin.CILIUM})
    default_cni = CniPlugin.PROVIDER_DEFAULT
    supports_existing_machines = False


class K3d(KubernetesDistribution):
    distribution_type = DistributionType.K3D
    display_name = "k3d"
    description = "K3s clusters running in Docker for local development."
    kind = DistributionKind.LOCAL
    capabilities = _LOCAL_CAPS | {Cap.HIGH_AVAILABILITY, Cap.ADD_NODES}
    implementation_status = ImplementationStatus.PLANNED
    planned_phase = 4
    supported_providers = frozenset({ProviderType.LOCAL})
    supported_cnis = frozenset({CniPlugin.PROVIDER_DEFAULT})
    default_cni = CniPlugin.PROVIDER_DEFAULT
    supports_existing_machines = False


class _Managed(KubernetesDistribution):
    kind = DistributionKind.MANAGED
    capabilities = _MANAGED_CAPS
    implementation_status = ImplementationStatus.PLANNED
    planned_phase = 5
    supported_cnis = frozenset({CniPlugin.PROVIDER_DEFAULT, CniPlugin.CILIUM})
    default_cni = CniPlugin.PROVIDER_DEFAULT
    supports_existing_machines = False


class Eks(_Managed):
    distribution_type = DistributionType.EKS
    display_name = "Amazon EKS"
    description = "Managed Kubernetes on AWS."
    supported_providers = frozenset({ProviderType.AWS})
    implementation_status = ImplementationStatus.AVAILABLE
    planned_phase = None
    capabilities = frozenset({Cap.CREATE, Cap.DELETE, Cap.IMPORT})
    options_model = EksOptions

    def plan_create(self, plan):
        from app.modules.cluster_lifecycle.distributions import managed_steps

        return [StepDefinition("managed-create", "Create EKS cluster and node groups", managed_steps.create)]

    def plan_delete(self, plan):
        from app.modules.cluster_lifecycle.distributions import managed_steps
        from app.modules.cluster_lifecycle.distributions.kubeadm.steps import release

        return [
            StepDefinition(
                "managed-delete",
                "Delete EKS cluster and node groups",
                managed_steps.delete,
                max_attempts=3,
                retry_backoff_seconds=30,
            ),
            StepDefinition("release", "Remove cluster registration", release),
        ]


class Aks(_Managed):
    distribution_type = DistributionType.AKS
    display_name = "Azure AKS"
    description = "Managed Kubernetes on Microsoft Azure."
    supported_providers = frozenset({ProviderType.AZURE})
    implementation_status = ImplementationStatus.AVAILABLE
    planned_phase = None
    capabilities = frozenset({Cap.CREATE, Cap.DELETE, Cap.IMPORT})
    options_model = AksOptions

    def plan_create(self, plan):
        from app.modules.cluster_lifecycle.distributions import managed_steps

        return [StepDefinition("managed-create", "Create AKS cluster and node pools", managed_steps.create)]

    def plan_delete(self, plan):
        from app.modules.cluster_lifecycle.distributions import managed_steps
        from app.modules.cluster_lifecycle.distributions.kubeadm.steps import release

        return [
            StepDefinition(
                "managed-delete", "Delete AKS cluster", managed_steps.delete, max_attempts=3, retry_backoff_seconds=30
            ),
            StepDefinition("release", "Remove cluster registration", release),
        ]


class Gke(_Managed):
    distribution_type = DistributionType.GKE
    display_name = "Google GKE"
    description = "Managed Kubernetes on Google Cloud."
    supported_providers = frozenset({ProviderType.GCP})
    implementation_status = ImplementationStatus.AVAILABLE
    planned_phase = None
    capabilities = frozenset({Cap.CREATE, Cap.DELETE, Cap.IMPORT})
    options_model = GkeOptions

    def plan_create(self, plan):
        from app.modules.cluster_lifecycle.distributions import managed_steps

        return [StepDefinition("managed-create", "Create GKE cluster and node pools", managed_steps.create)]

    def plan_delete(self, plan):
        from app.modules.cluster_lifecycle.distributions import managed_steps
        from app.modules.cluster_lifecycle.distributions.kubeadm.steps import release

        return [
            StepDefinition(
                "managed-delete", "Delete GKE cluster", managed_steps.delete, max_attempts=3, retry_backoff_seconds=30
            ),
            StepDefinition("release", "Remove cluster registration", release),
        ]


ALL_DISTRIBUTIONS: list[type[KubernetesDistribution]] = [Kubeadm, K3s, Rke2, Kind, K3d, Eks, Aks, Gke]

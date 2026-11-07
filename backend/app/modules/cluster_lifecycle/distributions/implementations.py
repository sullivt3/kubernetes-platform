"""Distribution catalog.

Each distribution declares its supported infrastructure, runtimes, CNIs and
capabilities. Implemented distributions live in their own package (``kubeadm/``)
with their automation alongside; the rest are ``PLANNED`` and their ``plan_*``
methods raise ``NotImplementedYetError``.
"""

from __future__ import annotations

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


class Aks(_Managed):
    distribution_type = DistributionType.AKS
    display_name = "Azure AKS"
    description = "Managed Kubernetes on Microsoft Azure."
    supported_providers = frozenset({ProviderType.AZURE})


class Gke(_Managed):
    distribution_type = DistributionType.GKE
    display_name = "Google GKE"
    description = "Managed Kubernetes on Google Cloud."
    supported_providers = frozenset({ProviderType.GCP})


ALL_DISTRIBUTIONS: list[type[KubernetesDistribution]] = [Kubeadm, K3s, Rke2, Kind, K3d, Eks, Aks, Gke]

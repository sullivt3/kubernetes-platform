"""Distribution vocabulary."""

from __future__ import annotations

from enum import StrEnum


class DistributionType(StrEnum):
    KUBEADM = "kubeadm"
    K3S = "k3s"
    RKE2 = "rke2"
    KIND = "kind"
    K3D = "k3d"
    EKS = "eks"
    AKS = "aks"
    GKE = "gke"
    # Imported clusters whose distribution could not be identified. Not installable.
    OTHER = "other"


class DistributionKind(StrEnum):
    SELF_MANAGED = "self_managed"  # platform installs and operates the control plane on machines
    MANAGED = "managed"  # cloud provider operates the control plane
    LOCAL = "local"  # disposable development clusters


class DistributionCapability(StrEnum):
    CREATE = "create"
    IMPORT = "import"
    ADD_NODES = "add_nodes"
    REMOVE_NODES = "remove_nodes"
    UPGRADE = "upgrade"
    DELETE = "delete"
    HIGH_AVAILABILITY = "high_availability"


class ContainerRuntime(StrEnum):
    CONTAINERD = "containerd"
    CRI_O = "cri-o"


class CniPlugin(StrEnum):
    CALICO = "calico"
    CILIUM = "cilium"
    FLANNEL = "flannel"
    CANAL = "canal"
    PROVIDER_DEFAULT = "provider-default"  # managed services / local tools choose

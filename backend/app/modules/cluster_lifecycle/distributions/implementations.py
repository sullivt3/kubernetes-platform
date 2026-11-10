"""Distribution catalog.

Each distribution declares its supported infrastructure, runtimes, CNIs and
capabilities. Implemented distributions live in their own package (``kubeadm/``)
with their automation alongside; the rest are ``PLANNED`` and their ``plan_*``
methods raise ``NotImplementedYetError``.
"""

from __future__ import annotations

from typing import ClassVar

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


class RancherOptions(BaseModel):
    release_revision: int = Field(default=1, ge=1, le=100)


class _Rancher(KubernetesDistribution):
    kind = DistributionKind.SELF_MANAGED
    capabilities = _SELF_MANAGED_CAPS
    implementation_status = ImplementationStatus.PREVIEW
    supported_providers = _VM_PROVIDERS
    options_model = RancherOptions

    def validate_spec(self, spec):
        problems = super().validate_spec(spec)
        if spec.kubernetes_version.removeprefix("v").count(".") != 2:
            problems.append("Choose an exact Kubernetes patch version for K3s/RKE2")
        if spec.control_plane.count > 1 and spec.control_plane.count % 2 == 0:
            problems.append("Embedded etcd requires an odd number of control-plane nodes")
        if spec.control_plane.load_balancer.value not in {"none", "external"}:
            problems.append("K3s/RKE2 currently use an existing external API load balancer for HA")
        return problems

    def plan_create(self, plan):
        from app.modules.cluster_lifecycle.distributions import rancher_steps as native
        from app.modules.cluster_lifecycle.distributions.kubeadm import steps

        result = [
            StepDefinition("wait-machines", "Wait for machine provisioning", steps.wait_machines),
            StepDefinition("validate", "Validate machines and connectivity", steps.validate),
            StepDefinition("prepare", "Prepare operating systems and dependencies", native.prepare),
            StepDefinition("bootstrap", f"Bootstrap {self.display_name} control plane", native.bootstrap),
        ]
        if self.distribution_type is DistributionType.K3S and plan.spec.network.cni is CniPlugin.CALICO:
            result.append(StepDefinition("cni", "Install Calico", steps.install_cni))
        result.extend(
            [
                StepDefinition("join", "Join remaining servers and agents", native.join),
                StepDefinition("register", "Store encrypted cluster access", native.register),
            ]
        )
        if self.distribution_type is DistributionType.K3S and plan.spec.network.cni is CniPlugin.CILIUM:
            from app.modules.cluster_lifecycle.distributions import cni_steps

            result.append(StepDefinition("cni", "Install pinned Cilium", cni_steps.install))
        result.append(StepDefinition("ready", "Wait for nodes to become Ready", native.ready))
        result.append(StepDefinition("smoke-test", "Verify scheduling and cluster DNS", steps.smoke_test))
        return result

    def plan_add_nodes(self, plan):
        from app.modules.cluster_lifecycle.distributions import rancher_steps as native
        from app.modules.cluster_lifecycle.distributions.kubeadm import steps

        return [
            StepDefinition("wait-machines", "Wait for machine provisioning", steps.wait_machines),
            StepDefinition("validate", "Validate new machines", steps.validate),
            StepDefinition("prepare", "Prepare operating systems", native.prepare),
            StepDefinition("join", "Join servers and agents", native.join),
            StepDefinition("ready", "Wait for new nodes", native.ready),
            StepDefinition("finalize", "Record joined nodes", steps.finalize_add),
        ]

    def plan_remove_nodes(self, plan):
        from app.modules.cluster_lifecycle.distributions import rancher_steps as native
        from app.modules.cluster_lifecycle.distributions.kubeadm import steps

        result = [
            StepDefinition("drain", "Drain leaving nodes", steps.drain_nodes),
            StepDefinition("node-delete", "Delete node membership", steps.delete_node_objects),
            StepDefinition("uninstall", f"Uninstall {self.display_name}", native.uninstall),
            StepDefinition("finalize", "Release node registration", steps.finalize_remove),
        ]
        if plan.destroy_machines:
            result.append(StepDefinition("destroy-machines", "Destroy provisioned machines", steps.destroy_machines))
        return result

    def plan_upgrade(self, plan, target_version):
        from app.modules.cluster_lifecycle.distributions import rancher_steps as native

        return [StepDefinition("upgrade", f"Upgrade {self.display_name} servers then agents", native.upgrade)]

    def plan_delete(self, plan):
        from app.modules.cluster_lifecycle.distributions import rancher_steps as native
        from app.modules.cluster_lifecycle.distributions.kubeadm import steps

        result = [StepDefinition("uninstall", f"Uninstall {self.display_name}", native.uninstall)]
        if plan.destroy_machines:
            result.append(StepDefinition("destroy-machines", "Destroy provisioned machines", steps.destroy_machines))
        result.append(StepDefinition("release", "Remove cluster registration", steps.release))
        return result


class K3s(_Rancher):
    distribution_type = DistributionType.K3S
    display_name = "K3s"
    description = "Lightweight certified Kubernetes distribution, well suited to edge and small clusters."
    kind = DistributionKind.SELF_MANAGED
    capabilities = _SELF_MANAGED_CAPS
    implementation_status = ImplementationStatus.PREVIEW
    supported_providers = _VM_PROVIDERS
    supported_cnis = frozenset({CniPlugin.FLANNEL, CniPlugin.CALICO, CniPlugin.CILIUM})
    default_cni = CniPlugin.FLANNEL


class Rke2(_Rancher):
    distribution_type = DistributionType.RKE2
    display_name = "RKE2"
    description = "Security-focused Kubernetes distribution with CIS-hardened defaults."
    kind = DistributionKind.SELF_MANAGED
    capabilities = _SELF_MANAGED_CAPS
    implementation_status = ImplementationStatus.PREVIEW
    supported_providers = _VM_PROVIDERS
    supported_cnis = frozenset({CniPlugin.CANAL, CniPlugin.CALICO, CniPlugin.CILIUM})
    default_cni = CniPlugin.CANAL


class Kind(KubernetesDistribution):
    distribution_type = DistributionType.KIND
    display_name = "Kind"
    description = "Kubernetes-in-Docker clusters for local development and testing."
    kind = DistributionKind.LOCAL
    capabilities = _LOCAL_CAPS | {Cap.HIGH_AVAILABILITY}
    implementation_status = ImplementationStatus.PREVIEW
    supported_providers = frozenset({ProviderType.LOCAL})
    supported_cnis = frozenset({CniPlugin.PROVIDER_DEFAULT, CniPlugin.CALICO, CniPlugin.CILIUM})
    default_cni = CniPlugin.PROVIDER_DEFAULT
    supports_existing_machines = False

    def validate_spec(self, spec):
        problems = super().validate_spec(spec)
        if spec.kubernetes_version.removeprefix("v").count(".") != 2:
            problems.append("Local distributions require an exact Kubernetes patch version")
        if spec.control_plane.count > 1 and spec.control_plane.count % 2 == 0:
            problems.append("Highly available local clusters require an odd control-plane count")
        return problems

    def plan_create(self, plan):
        from app.modules.cluster_lifecycle.distributions import cni_steps, local_steps

        result = [
            StepDefinition(
                "local-create", f"Create {self.display_name} cluster and store encrypted access", local_steps.create
            )
        ]
        if plan.spec.network.cni is not CniPlugin.PROVIDER_DEFAULT:
            result.append(StepDefinition("cni", "Install selected pod network", cni_steps.install))
        result.append(StepDefinition("local-ready", "Wait for local nodes", local_steps.ready))
        return result

    def plan_delete(self, plan):
        from app.modules.cluster_lifecycle.distributions import local_steps
        from app.modules.cluster_lifecycle.distributions.kubeadm.steps import release

        return [
            StepDefinition("local-delete", f"Delete {self.display_name} Docker cluster", local_steps.delete),
            StepDefinition("release", "Remove cluster registration", release),
        ]


class K3d(Kind):
    distribution_type = DistributionType.K3D
    display_name = "k3d"
    description = "K3s clusters running in Docker for local development."
    kind = DistributionKind.LOCAL
    capabilities = _LOCAL_CAPS | {Cap.HIGH_AVAILABILITY, Cap.ADD_NODES, Cap.REMOVE_NODES}
    implementation_status = ImplementationStatus.PREVIEW
    supported_providers = frozenset({ProviderType.LOCAL})
    supported_cnis = frozenset({CniPlugin.PROVIDER_DEFAULT})
    default_cni = CniPlugin.PROVIDER_DEFAULT
    supports_existing_machines = False


class _Managed(KubernetesDistribution):
    kind = DistributionKind.MANAGED
    capabilities = _MANAGED_CAPS
    implementation_status = ImplementationStatus.PLANNED
    planned_phase: ClassVar[int | None] = 5
    supported_cnis = frozenset({CniPlugin.PROVIDER_DEFAULT, CniPlugin.CILIUM})
    default_cni = CniPlugin.PROVIDER_DEFAULT
    supports_existing_machines = False

    def validate_spec(self, spec):
        problems = super().validate_spec(spec)
        if spec.network.dns_domain != "cluster.local":
            problems.append("Managed services use the cluster.local DNS domain")
        if spec.distribution is DistributionType.AKS:
            names = [pool.name.replace("-", "")[:12] for pool in spec.worker_pools]
            if len(names) != len(set(names)) or any(not name[0].isalpha() for name in names):
                problems.append(
                    "AKS pool names must start with a letter and remain unique after "
                    "removing hyphens and limiting to 12 characters"
                )
            for pool in spec.worker_pools:
                if pool.machine_template and pool.machine_template.disk_gb < 30:
                    problems.append("AKS OS disks must be at least 30 GB")
        if spec.distribution is DistributionType.GKE and any(len(pool.name) > 40 for pool in spec.worker_pools):
            problems.append("GKE node pool names must be at most 40 characters")
        return problems

    def plan_upgrade(self, plan, target_version):
        from app.modules.cluster_lifecycle.distributions import managed_steps

        return [
            StepDefinition("managed-upgrade", "Upgrade managed control plane and node pools", managed_steps.upgrade)
        ]


class Eks(_Managed):
    distribution_type = DistributionType.EKS
    display_name = "Amazon EKS"
    description = "Managed Kubernetes on AWS."
    supported_providers = frozenset({ProviderType.AWS})
    implementation_status = ImplementationStatus.AVAILABLE
    planned_phase = None
    capabilities = _MANAGED_CAPS
    options_model = EksOptions

    def plan_create(self, plan):
        from app.modules.cluster_lifecycle.distributions import cni_steps, managed_steps

        result = [StepDefinition("managed-create", "Create EKS cluster and node groups", managed_steps.create)]
        if plan.spec.network.cni is CniPlugin.CILIUM:
            result.append(StepDefinition("cni", "Install Cilium with AWS VPC CNI chaining", cni_steps.install))
        return result

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
    capabilities = _MANAGED_CAPS
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
    capabilities = _MANAGED_CAPS
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

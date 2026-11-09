"""kubeadm distribution: upstream Kubernetes on existing Linux machines (containerd)."""

from __future__ import annotations

import ipaddress

from app.modules.cluster_lifecycle.distributions.base import (
    ClusterPlanInput,
    KubernetesDistribution,
)
from app.modules.cluster_lifecycle.distributions.kubeadm import config as cfg
from app.modules.cluster_lifecycle.distributions.kubeadm import steps
from app.modules.cluster_lifecycle.distributions.spec import ClusterSpec, LoadBalancerMode
from app.modules.cluster_lifecycle.distributions.types import (
    CniPlugin,
    ContainerRuntime,
    DistributionCapability,
    DistributionKind,
    DistributionType,
)
from app.modules.providers.types import ImplementationStatus, ProviderType
from app.orchestration.workflow import StepDefinition


class Kubeadm(KubernetesDistribution):
    distribution_type = DistributionType.KUBEADM
    display_name = "kubeadm"
    description = "Upstream Kubernetes bootstrapped with kubeadm and containerd on Linux machines."
    kind = DistributionKind.SELF_MANAGED
    capabilities = frozenset(
        {
            DistributionCapability.CREATE,
            DistributionCapability.DELETE,
            DistributionCapability.HIGH_AVAILABILITY,
            DistributionCapability.ADD_NODES,
            DistributionCapability.REMOVE_NODES,
            DistributionCapability.UPGRADE,
        }
    )
    implementation_status = ImplementationStatus.AVAILABLE
    planned_phase = None
    # Any Linux machine reachable over SSH: registered servers, or machines provisioned
    # through a provider that can create them (local containers, Proxmox VMs and public clouds).
    supported_providers = frozenset(
        {
            ProviderType.SSH,
            ProviderType.LOCAL,
            ProviderType.PROXMOX,
            ProviderType.AWS,
            ProviderType.AZURE,
            ProviderType.GCP,
        }
    )
    supported_runtimes = frozenset({ContainerRuntime.CONTAINERD})
    supported_cnis = frozenset({CniPlugin.CALICO, CniPlugin.FLANNEL})
    default_cni = CniPlugin.CALICO
    supports_existing_machines = True
    options_model = cfg.KubeadmOptions

    def validate_spec(self, spec: ClusterSpec) -> list[str]:
        problems = super().validate_spec(spec)
        try:
            _, minor, _ = cfg.parse_version(spec.kubernetes_version)
            if minor < cfg.MIN_MINOR:
                problems.append(f"Kubernetes 1.{minor} is not supported; use 1.{cfg.MIN_MINOR} or newer")
        except ValueError as exc:
            problems.append(str(exc))
        mode = spec.control_plane.load_balancer
        if mode is LoadBalancerMode.PROVIDER:
            if spec.provider_type is not ProviderType.AWS:
                problems.append("Provider-managed API load balancing is currently supported for AWS only")
            if not spec.provider_connection_id:
                problems.append("Provider-managed API load balancing needs a provider connection")
            options = spec.options
            for key in ("provider_lb_vpc_id",):
                if not isinstance(options.get(key), str) or not options[key].strip():
                    problems.append(f"options.{key} is required for provider-managed API load balancing")
            subnets = options.get("provider_lb_subnet_ids")
            if (
                not isinstance(subnets, list)
                or not subnets
                or any(not isinstance(item, str) or not item for item in subnets)
            ):
                problems.append("options.provider_lb_subnet_ids must contain one or more subnet IDs")
        if mode is LoadBalancerMode.KUBE_VIP:
            host = (spec.control_plane.endpoint or "").split(":", 1)[0]
            try:
                if ipaddress.ip_address(host).version != 4:
                    raise ValueError
            except ValueError:
                problems.append("kube-vip needs an unused IPv4 address on the control-plane subnet as the endpoint")
            if (
                spec.control_plane.endpoint
                and ":" in spec.control_plane.endpoint
                and not spec.control_plane.endpoint.endswith(":6443")
            ):
                problems.append("kube-vip serves the API on port 6443; give the address without another port")
        if spec.control_plane.count > 1:
            if spec.control_plane.count % 2 == 0:
                problems.append("Highly available control planes need an odd member count (3, 5 or 7)")
            if not spec.control_plane.endpoint and mode is not LoadBalancerMode.PROVIDER:
                problems.append("Highly available control planes need a stable API endpoint (load balancer address)")
        return problems

    def plan_create(self, plan: ClusterPlanInput) -> list[StepDefinition]:
        cni = plan.spec.network.cni.value
        first = plan.control_planes[0]
        result: list[StepDefinition] = [
            *self._wait_machines(plan),
            *self._provider_lb(plan, create=True),
            StepDefinition(
                "validate",
                "Validate machines and connectivity",
                steps.validate,
                max_attempts=3,
                retry_backoff_seconds=10,
            ),
            StepDefinition(
                "prepare", "Prepare operating systems", steps.prepare, max_attempts=2, retry_backoff_seconds=15
            ),
            StepDefinition(
                "containerd",
                "Install and configure containerd",
                steps.install_containerd,
                max_attempts=3,
                retry_backoff_seconds=15,
            ),
            StepDefinition(
                "resolve-version",
                "Resolve Kubernetes version",
                steps.resolve_version,
                max_attempts=3,
                retry_backoff_seconds=10,
            ),
            StepDefinition(
                "packages",
                "Install kubeadm, kubelet and kubectl",
                steps.install_packages,
                max_attempts=3,
                retry_backoff_seconds=15,
            ),
            StepDefinition(
                "init",
                f"Initialize control plane on {first.name}",
                steps.init_control_plane,
                max_attempts=2,
                retry_backoff_seconds=15,
            ),
            StepDefinition(
                "cni", f"Install {cni} pod network", steps.install_cni, max_attempts=2, retry_backoff_seconds=15
            ),
        ]
        for node in plan.control_planes[1:]:
            result.append(
                StepDefinition(
                    f"join-cp:{node.machine_id}",
                    f"Join control plane {node.name}",
                    steps.join_control_plane(node),
                    max_attempts=2,
                    retry_backoff_seconds=20,
                )
            )
        if plan.workers:
            result.append(
                StepDefinition(
                    "join-workers",
                    f"Join {len(plan.workers)} worker node(s)",
                    steps.join_workers,
                    max_attempts=2,
                    retry_backoff_seconds=20,
                )
            )
        result += [
            StepDefinition(
                "configure-nodes",
                "Apply node labels and taints",
                steps.configure_nodes,
                max_attempts=2,
                retry_backoff_seconds=10,
            ),
            StepDefinition("wait-ready", "Wait for nodes and cluster DNS", steps.wait_ready),
            StepDefinition(
                "smoke-test",
                "Smoke test: schedule a pod and resolve cluster DNS",
                steps.smoke_test,
                max_attempts=2,
                retry_backoff_seconds=15,
            ),
            StepDefinition(
                "register",
                "Store kubeconfig and register cluster",
                steps.register,
                max_attempts=2,
                retry_backoff_seconds=5,
            ),
        ]
        return result

    def plan_delete(self, plan: ClusterPlanInput) -> list[StepDefinition]:
        result = [
            StepDefinition(
                "reset",
                f"Reset Kubernetes on {len(plan.nodes)} node(s)",
                steps.reset_nodes,
                max_attempts=2,
                retry_backoff_seconds=10,
            ),
            StepDefinition("release", "Release machines and remove the cluster", steps.release),
        ]
        if plan.spec.options.get("provider_lb_external_id"):
            result.insert(
                -1,
                StepDefinition(
                    "provider-load-balancer-delete",
                    "Delete provider load balancer",
                    steps.delete_provider_load_balancer,
                ),
            )
        if plan.destroy_machines:
            result.append(
                StepDefinition(
                    "destroy-machines",
                    "Destroy machines the platform provisioned",
                    steps.destroy_machines,
                    max_attempts=3,
                    retry_backoff_seconds=10,
                )
            )
        return result

    def plan_upgrade(self, plan: ClusterPlanInput, target_version: str) -> list[StepDefinition]:
        if not plan.control_planes or not plan.nodes:
            raise ValueError("A kubeadm upgrade needs registered control-plane and worker nodes")
        result: list[StepDefinition] = []
        for index, node in enumerate(plan.control_planes):
            result.append(
                StepDefinition(
                    f"upgrade-kubeadm:{node.machine_id}",
                    f"Upgrade kubeadm on control plane {node.name}",
                    steps.upgrade_kubeadm(node, target_version, apply=index == 0),
                    max_attempts=2,
                    retry_backoff_seconds=15,
                )
            )
        for node in plan.workers:
            result.append(
                StepDefinition(
                    f"upgrade-kubeadm:{node.machine_id}",
                    f"Upgrade kubeadm on worker {node.name}",
                    steps.upgrade_kubeadm(node, target_version, apply=False),
                    max_attempts=2,
                    retry_backoff_seconds=15,
                )
            )
        for node in plan.nodes:
            result.append(
                StepDefinition(
                    f"upgrade-kubelet:{node.machine_id}",
                    f"Drain, upgrade and uncordon {node.name}",
                    steps.upgrade_node(node, target_version),
                    max_attempts=2,
                    retry_backoff_seconds=20,
                )
            )
        result.append(
            StepDefinition(
                "verify-upgrade",
                "Verify all nodes are Ready at the target version",
                steps.verify_upgrade,
            )
        )
        return result

    # ---- node add / remove -----------------------------------------------------------

    @staticmethod
    def _wait_machines(plan: ClusterPlanInput) -> list[StepDefinition]:
        provisioned = [n for n in plan.nodes if n.provisioned]
        if not provisioned:
            return []
        return [
            StepDefinition(
                "wait-machines",
                f"Wait for {len(provisioned)} machine(s) to be provisioned",
                steps.wait_machines,
            )
        ]

    @staticmethod
    def _provider_lb(plan: ClusterPlanInput, *, create: bool) -> list[StepDefinition]:
        if plan.spec.control_plane.load_balancer is not LoadBalancerMode.PROVIDER:
            return []
        function = steps.create_provider_load_balancer if create else steps.sync_provider_load_balancer
        return [
            StepDefinition(
                "provider-load-balancer",
                "Reconcile provider API load balancer",
                function,
                max_attempts=3,
                retry_backoff_seconds=15,
            )
        ]

    def plan_add_nodes(self, plan: ClusterPlanInput) -> list[StepDefinition]:
        result = [
            *self._wait_machines(plan),
            StepDefinition(
                "validate", "Validate new machines", steps.validate, max_attempts=3, retry_backoff_seconds=10
            ),
            StepDefinition(
                "prepare", "Prepare operating systems", steps.prepare, max_attempts=2, retry_backoff_seconds=15
            ),
            StepDefinition(
                "containerd",
                "Install and configure containerd",
                steps.install_containerd,
                max_attempts=3,
                retry_backoff_seconds=15,
            ),
            StepDefinition("resolve-version", "Match the cluster's Kubernetes version", steps.cluster_version),
            StepDefinition(
                "packages",
                "Install kubeadm, kubelet and kubectl",
                steps.install_packages,
                max_attempts=3,
                retry_backoff_seconds=15,
            ),
            StepDefinition(
                "init",
                "Read join information from the cluster",
                steps.join_info,
                max_attempts=3,
                retry_backoff_seconds=10,
            ),
        ]
        result += self._provider_lb(plan, create=True)
        for node in plan.control_planes:
            result.append(
                StepDefinition(
                    f"join-cp:{node.machine_id}",
                    f"Join control plane {node.name}",
                    steps.join_control_plane(node),
                    max_attempts=2,
                    retry_backoff_seconds=20,
                )
            )
        if plan.workers:
            result.append(
                StepDefinition(
                    "join-workers",
                    f"Join {len(plan.workers)} worker node(s)",
                    steps.join_workers,
                    max_attempts=2,
                    retry_backoff_seconds=20,
                )
            )
        return [
            *result,
            StepDefinition(
                "configure-nodes",
                "Apply node labels and taints",
                steps.configure_nodes,
                max_attempts=2,
                retry_backoff_seconds=10,
            ),
            StepDefinition("wait-ready", "Wait for the new nodes", steps.wait_ready),
            StepDefinition("finalize", "Record the new nodes", steps.finalize_add),
        ]

    def plan_remove_nodes(self, plan: ClusterPlanInput) -> list[StepDefinition]:
        result = [
            *self._provider_lb(plan, create=False),
            StepDefinition(
                "drain", "Drain workloads off the node(s)", steps.drain_nodes, max_attempts=2, retry_backoff_seconds=15
            ),
            StepDefinition(
                "reset",
                "Reset Kubernetes on the node(s)",
                steps.reset_removed,
                max_attempts=2,
                retry_backoff_seconds=10,
            ),
        ]
        if plan.control_planes:
            result.append(
                StepDefinition(
                    "etcd",
                    "Remove control planes from etcd",
                    steps.remove_etcd_members,
                    max_attempts=3,
                    retry_backoff_seconds=10,
                )
            )
        result += [
            StepDefinition(
                "delete-nodes",
                "Delete the node objects",
                steps.delete_node_objects,
                max_attempts=3,
                retry_backoff_seconds=10,
            ),
            StepDefinition("release", "Release the machines", steps.finalize_remove),
        ]
        if plan.destroy_machines:
            result.append(
                StepDefinition(
                    "destroy-machines",
                    "Destroy machines the platform provisioned",
                    steps.destroy_machines,
                    max_attempts=3,
                    retry_backoff_seconds=10,
                )
            )
        return result

"""kubeadm distribution: upstream Kubernetes on existing Linux machines (containerd)."""

from __future__ import annotations

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
        }
    )
    implementation_status = ImplementationStatus.AVAILABLE
    planned_phase = None
    # Machines must already exist: today that means SSH-registered servers. VM/cloud
    # providers become usable here as soon as they can provision machines.
    supported_providers = frozenset({ProviderType.SSH})
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
        if spec.control_plane.load_balancer not in (LoadBalancerMode.NONE, LoadBalancerMode.EXTERNAL):
            problems.append(
                f"Load balancer mode '{spec.control_plane.load_balancer}' is not available for kubeadm yet; "
                "use 'external' with an existing load balancer or DNS name for HA"
            )
        if spec.control_plane.count > 1 and not spec.control_plane.endpoint:
            problems.append(
                "Highly available control planes need a stable API endpoint (load balancer address)"
            )
        for pool in (spec.control_plane, *spec.worker_pools):
            if pool.count > len(pool.existing_machine_ids):
                problems.append(
                    f"Node pool '{pool.name}' asks the platform to provision machines, but no available "
                    "infrastructure provider can create machines yet. Select registered machines instead."
                )
        return problems

    def plan_create(self, plan: ClusterPlanInput) -> list[StepDefinition]:
        cni = plan.spec.network.cni.value
        first = plan.control_planes[0]
        result: list[StepDefinition] = [
            StepDefinition(
                "validate",
                "Validate machines and connectivity",
                steps.validate,
                max_attempts=3,
                retry_backoff_seconds=10,
            ),
            StepDefinition(
                "prepare",
                "Prepare operating systems",
                steps.prepare,
                max_attempts=2,
                retry_backoff_seconds=15,
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
                "cni",
                f"Install {cni} pod network",
                steps.install_cni,
                max_attempts=2,
                retry_backoff_seconds=15,
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
                "register",
                "Store kubeconfig and register cluster",
                steps.register,
                max_attempts=2,
                retry_backoff_seconds=5,
            ),
        ]
        return result

    def plan_delete(self, plan: ClusterPlanInput) -> list[StepDefinition]:
        return [
            StepDefinition(
                "reset",
                f"Reset Kubernetes on {len(plan.nodes)} node(s)",
                steps.reset_nodes,
                max_attempts=2,
                retry_backoff_seconds=10,
            ),
            StepDefinition("release", "Release machines and remove the cluster", steps.release),
        ]

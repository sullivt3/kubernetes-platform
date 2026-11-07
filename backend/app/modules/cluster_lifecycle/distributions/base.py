"""The Kubernetes distribution contract.

A distribution knows how to turn a :class:`ClusterSpec` into an ordered workflow
*plan* (a list of :class:`StepDefinition`). It never executes anything itself and
never talks to an infrastructure provider directly: the plan's steps obtain
machines through the provider abstraction and run in-guest commands through the
host-access abstraction. This keeps "where machines come from" (providers) and
"how Kubernetes is installed" (distributions) independent.
"""

from __future__ import annotations

import uuid
from abc import ABC
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, ValidationError

from app.core.errors import NotImplementedYetError
from app.modules.cluster_lifecycle.distributions.spec import ClusterSpec
from app.modules.cluster_lifecycle.distributions.types import (
    CniPlugin,
    ContainerRuntime,
    DistributionCapability,
    DistributionKind,
    DistributionType,
)
from app.modules.providers.types import ImplementationStatus, ProviderType
from app.orchestration.workflow import StepDefinition


class EmptyOptions(BaseModel):
    pass


class PlannedNode(BaseModel):
    """A machine resolved for a cluster plan. Names are captured so plans stay deterministic."""

    machine_id: uuid.UUID
    name: str
    role: Literal["control_plane", "worker"]
    pool: str
    # Created by the platform for this cluster (waits for provisioning; destroyable with the cluster).
    provisioned: bool = False


class ClusterPlanInput(BaseModel):
    """Everything a distribution needs to plan an operation on a cluster."""

    cluster_id: uuid.UUID
    cluster_name: str
    spec: ClusterSpec
    nodes: list[PlannedNode]
    force: bool = Field(default=False, description="Delete/remove: continue past unreachable nodes")
    # Node add/remove: an existing, joined control plane used to issue join tokens and run kubectl.
    anchor: PlannedNode | None = None
    # Delete/remove: also destroy platform-provisioned machines at their provider.
    destroy_machines: bool = False

    @property
    def control_planes(self) -> list[PlannedNode]:
        return [n for n in self.nodes if n.role == "control_plane"]

    @property
    def workers(self) -> list[PlannedNode]:
        return [n for n in self.nodes if n.role == "worker"]

    @property
    def lead(self) -> PlannedNode:
        """The control plane that drives the operation: the anchor, or the first new control plane."""
        if self.anchor is not None:
            return self.anchor
        return self.control_planes[0]


class KubernetesDistribution(ABC):
    distribution_type: ClassVar[DistributionType]
    display_name: ClassVar[str]
    description: ClassVar[str]
    kind: ClassVar[DistributionKind]
    capabilities: ClassVar[frozenset[DistributionCapability]]
    implementation_status: ClassVar[ImplementationStatus]
    planned_phase: ClassVar[int | None] = None
    supported_providers: ClassVar[frozenset[ProviderType]]
    supported_runtimes: ClassVar[frozenset[ContainerRuntime]] = frozenset({ContainerRuntime.CONTAINERD})
    supported_cnis: ClassVar[frozenset[CniPlugin]] = frozenset({CniPlugin.CALICO, CniPlugin.CILIUM, CniPlugin.FLANNEL})
    default_cni: ClassVar[CniPlugin] = CniPlugin.CALICO
    supports_existing_machines: ClassVar[bool] = True
    options_model: ClassVar[type[BaseModel]] = EmptyOptions

    # ---- Validation (implemented generically; subclasses may extend) -------------

    def validate_spec(self, spec: ClusterSpec) -> list[str]:
        """Return human-readable problems; an empty list means the spec is acceptable."""
        problems: list[str] = []
        if spec.distribution is not self.distribution_type:
            problems.append(f"Spec targets '{spec.distribution}', not '{self.distribution_type}'")
        if spec.provider_type not in self.supported_providers:
            supported = ", ".join(sorted(p.value for p in self.supported_providers))
            problems.append(
                f"{self.display_name} cannot run on '{spec.provider_type}' infrastructure (supported: {supported})"
            )
        if spec.container_runtime not in self.supported_runtimes:
            problems.append(f"{self.display_name} does not support the '{spec.container_runtime}' runtime")
        if spec.network.cni not in self.supported_cnis:
            supported = ", ".join(sorted(c.value for c in self.supported_cnis))
            problems.append(f"CNI '{spec.network.cni}' is not supported by {self.display_name} ({supported})")
        if spec.control_plane.count > 1 and DistributionCapability.HIGH_AVAILABILITY not in self.capabilities:
            problems.append(f"{self.display_name} does not support multiple control-plane nodes")
        if self.kind is DistributionKind.MANAGED and spec.control_plane.count != 0:
            problems.append("Managed services operate the control plane; set control_plane.count to 0")
        if self.kind is not DistributionKind.MANAGED and spec.control_plane.count < 1:
            problems.append("At least one control-plane node is required")
        if not self.supports_existing_machines and any(
            pool.existing_machine_ids for pool in (spec.control_plane, *spec.worker_pools)
        ):
            problems.append(f"{self.display_name} provisions its own nodes; existing machines cannot be used")
        try:
            self.options_model.model_validate(spec.options)
        except ValidationError as exc:
            problems.extend(
                f"options.{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors(include_url=False)
            )
        return problems

    # ---- Planning (implemented per distribution in later phases) ------------------

    def _not_ready(self, action: str) -> NotImplementedYetError:
        phase = f" (planned for Phase {self.planned_phase})" if self.planned_phase else ""
        return NotImplementedYetError(f"{self.display_name} {action} is not implemented yet{phase}")

    def plan_create(self, plan: ClusterPlanInput) -> list[StepDefinition]:
        raise self._not_ready("cluster creation")

    def plan_add_nodes(self, plan: ClusterPlanInput) -> list[StepDefinition]:
        """``plan.nodes`` are the new nodes; ``plan.anchor`` is an existing control plane."""
        raise self._not_ready("node addition")

    def plan_remove_nodes(self, plan: ClusterPlanInput) -> list[StepDefinition]:
        """``plan.nodes`` are the nodes to remove; ``plan.anchor`` is a control plane that stays."""
        raise self._not_ready("node removal")

    def plan_upgrade(self, plan: ClusterPlanInput, target_version: str) -> list[StepDefinition]:
        raise self._not_ready("upgrade")

    def plan_delete(self, plan: ClusterPlanInput) -> list[StepDefinition]:
        raise self._not_ready("cluster deletion")

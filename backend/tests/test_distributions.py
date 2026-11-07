from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.errors import NotImplementedYetError
from app.modules.cluster_lifecycle.distributions.base import ClusterPlanInput
from app.modules.cluster_lifecycle.distributions.registry import distribution_registry
from app.modules.cluster_lifecycle.distributions.spec import ClusterSpec
from app.modules.cluster_lifecycle.distributions.types import DistributionKind, DistributionType


def spec(**overrides: Any) -> ClusterSpec:
    data: dict[str, Any] = {
        "name": "prod-1",
        "distribution": "kubeadm",
        "provider_type": "ssh",
        "kubernetes_version": "1.31.2",
        "control_plane": {"count": 1, "existing_machine_ids": [str(uuid.uuid4())]},
        "worker_pools": [
            {"name": "general", "count": 2, "existing_machine_ids": [str(uuid.uuid4()), str(uuid.uuid4())]}
        ],
    }
    data.update(overrides)
    return ClusterSpec.model_validate(data)


def test_every_distribution_registered_with_metadata() -> None:
    # OTHER marks imported clusters of unknown origin; it is never installable.
    expected = set(DistributionType) - {DistributionType.OTHER}
    assert {d.distribution_type for d in distribution_registry.all()} == expected
    for d in distribution_registry.all():
        descriptor = distribution_registry.describe(d)
        assert descriptor.supported_providers
        assert descriptor.default_cni in descriptor.supported_cnis


def test_valid_kubeadm_spec_has_no_problems() -> None:
    assert distribution_registry.validate(spec()) == []


def test_containerd_is_default_runtime() -> None:
    assert spec().container_runtime.value == "containerd"


class TestSpecValidation:
    def test_overlapping_cidrs_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not overlap"):
            spec(network={"pod_cidr": "10.0.0.0/8", "service_cidr": "10.96.0.0/12"})

    def test_invalid_cidr_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not a valid network"):
            spec(network={"pod_cidr": "10.244.0.1/16"})

    def test_even_ha_control_plane_rejected_at_creation(self) -> None:
        # The spec model allows even counts (node add/remove passes through them); creating a
        # cluster with one is rejected by the distribution.
        from app.modules.cluster_lifecycle.distributions.kubeadm import Kubeadm

        template = {"cpu_cores": 2, "memory_mb": 4096, "disk_gb": 40}
        even = spec(
            control_plane={"count": 2, "load_balancer": "external", "endpoint": "api.x", "machine_template": template}
        )
        assert any("odd member count" in p for p in Kubeadm().validate_spec(even))

    def test_ha_requires_load_balancer(self) -> None:
        template = {"cpu_cores": 2, "memory_mb": 4096, "disk_gb": 40}
        with pytest.raises(ValidationError, match="load balancer"):
            spec(control_plane={"count": 3, "machine_template": template})

    def test_kube_vip_requires_endpoint(self) -> None:
        template = {"cpu_cores": 2, "memory_mb": 4096, "disk_gb": 40}
        with pytest.raises(ValidationError, match="stable API endpoint"):
            spec(control_plane={"count": 3, "load_balancer": "kube-vip", "machine_template": template})

    def test_pool_without_enough_machines_or_template_rejected(self) -> None:
        with pytest.raises(ValidationError, match="needs 2 more machine"):
            spec(worker_pools=[{"name": "w", "count": 2}])

    def test_machine_in_two_pools_rejected(self) -> None:
        machine = str(uuid.uuid4())
        with pytest.raises(ValidationError, match="one node pool"):
            spec(
                control_plane={"count": 1, "existing_machine_ids": [machine]},
                worker_pools=[{"name": "w", "count": 1, "existing_machine_ids": [machine]}],
            )

    def test_invalid_label_rejected(self) -> None:
        template = {"cpu_cores": 2, "memory_mb": 4096, "disk_gb": 40}
        with pytest.raises(ValidationError, match="Invalid label key"):
            spec(worker_pools=[{"name": "w", "count": 1, "machine_template": template, "labels": {"bad key": "x"}}])


class TestCompatibility:
    def test_managed_service_on_wrong_provider(self) -> None:
        problems = distribution_registry.validate(
            spec(distribution="eks", provider_type="proxmox", control_plane={"count": 0})
        )
        assert any("cannot run on 'proxmox'" in p for p in problems)

    def test_managed_service_requires_zero_control_plane_nodes(self) -> None:
        problems = distribution_registry.validate(spec(distribution="eks", provider_type="aws"))
        assert any("operate the control plane" in p for p in problems)

    def test_local_distribution_rejects_existing_machines(self) -> None:
        problems = distribution_registry.validate(
            spec(distribution="kind", provider_type="local", network={"cni": "provider-default"})
        )
        assert any("existing machines" in p for p in problems)

    def test_unsupported_cni(self) -> None:
        problems = distribution_registry.validate(spec(distribution="rke2", network={"cni": "flannel"}))
        assert any("CNI 'flannel'" in p for p in problems)

    def test_self_managed_needs_a_control_plane(self) -> None:
        problems = distribution_registry.validate(spec(control_plane={"count": 0}))
        assert any("At least one control-plane" in p for p in problems)


PLANNED = [d for d in distribution_registry.all() if d.implementation_status.value == "planned"]


@pytest.mark.parametrize("d", PLANNED, ids=lambda d: d.distribution_type.value)
def test_planned_distributions_refuse_to_plan(d: Any) -> None:
    kind_spec = spec(
        distribution=d.distribution_type.value,
        control_plane={"count": 0} if d.kind is DistributionKind.MANAGED else spec().control_plane.model_dump(),
    )
    plan = ClusterPlanInput(cluster_id=uuid.uuid4(), cluster_name="x", spec=kind_spec, nodes=[])
    with pytest.raises(NotImplementedYetError):
        d.plan_create(plan)

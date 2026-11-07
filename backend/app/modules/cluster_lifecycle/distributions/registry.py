"""Registry of Kubernetes distributions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.core.errors import ValidationFailedError
from app.modules.cluster_lifecycle.distributions.base import KubernetesDistribution
from app.modules.cluster_lifecycle.distributions.spec import ClusterSpec
from app.modules.cluster_lifecycle.distributions.types import DistributionType
from app.modules.providers.types import ImplementationStatus


class DistributionDescriptor(BaseModel):
    type: DistributionType
    display_name: str
    description: str
    kind: str
    implementation_status: ImplementationStatus
    planned_phase: int | None
    capabilities: list[str]
    supported_providers: list[str]
    supported_runtimes: list[str]
    supported_cnis: list[str]
    default_cni: str
    supports_existing_machines: bool
    options_schema: dict[str, Any]


class DistributionRegistry:
    def __init__(self) -> None:
        self._items: dict[DistributionType, KubernetesDistribution] = {}

    def register(self, cls: type[KubernetesDistribution]) -> None:
        if cls.distribution_type in self._items:
            raise ValueError(f"Distribution '{cls.distribution_type}' is already registered")
        self._items[cls.distribution_type] = cls()

    def get(self, distribution_type: DistributionType) -> KubernetesDistribution:
        try:
            return self._items[distribution_type]
        except KeyError:
            raise ValidationFailedError(f"Unknown distribution '{distribution_type}'") from None

    def all(self) -> list[KubernetesDistribution]:
        return list(self._items.values())

    def validate(self, spec: ClusterSpec) -> list[str]:
        return self.get(spec.distribution).validate_spec(spec)

    @staticmethod
    def describe(d: KubernetesDistribution) -> DistributionDescriptor:
        return DistributionDescriptor(
            type=d.distribution_type,
            display_name=d.display_name,
            description=d.description,
            kind=d.kind.value,
            implementation_status=d.implementation_status,
            planned_phase=d.planned_phase,
            capabilities=sorted(c.value for c in d.capabilities),
            supported_providers=sorted(p.value for p in d.supported_providers),
            supported_runtimes=sorted(r.value for r in d.supported_runtimes),
            supported_cnis=sorted(c.value for c in d.supported_cnis),
            default_cni=d.default_cni.value,
            supports_existing_machines=d.supports_existing_machines,
            options_schema=d.options_model.model_json_schema(),
        )

    def is_registered(self, distribution_type: DistributionType) -> bool:
        return distribution_type in self._items

    def describe_all(self) -> list[DistributionDescriptor]:
        return [self.describe(d) for d in self._items.values()]


def build_default_registry() -> DistributionRegistry:
    from app.modules.cluster_lifecycle.distributions.implementations import ALL_DISTRIBUTIONS

    registry = DistributionRegistry()
    for cls in ALL_DISTRIBUTIONS:
        registry.register(cls)
    return registry


distribution_registry = build_default_registry()

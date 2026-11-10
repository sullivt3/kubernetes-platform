from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.core.errors import CapabilityNotSupportedError, ValidationFailedError
from app.modules.providers.base import CAPABILITY_METHODS, InfrastructureProvider
from app.modules.providers.registry import ProviderRegistry, provider_registry
from app.modules.providers.types import (
    ConnectionCheckResult,
    ImplementationStatus,
    MachineRef,
    ProviderCapability,
    ProviderCategory,
    ProviderType,
)


def capability_contract_violations(cls: type[InfrastructureProvider]) -> list[str]:
    """Declared capabilities must be backed by an overridden method."""
    return [
        f"{cls.__name__} declares '{cap}' but does not override {method}()"
        for cap, method in CAPABILITY_METHODS.items()
        if cap in cls.capabilities and getattr(cls, method) is getattr(InfrastructureProvider, method)
    ]


def test_every_provider_type_is_registered() -> None:
    assert {c.provider_type for c in provider_registry.all()} == set(ProviderType)


def test_every_capability_maps_to_a_method() -> None:
    assert set(CAPABILITY_METHODS) == set(ProviderCapability)


@pytest.mark.parametrize("cls", provider_registry.all(), ids=lambda c: c.provider_type.value)
def test_provider_metadata_is_complete(cls: type[InfrastructureProvider]) -> None:
    descriptor = ProviderRegistry.describe(cls)
    assert descriptor.display_name and descriptor.description
    assert descriptor.config_schema["type"] == "object"
    if cls.credential_required:
        assert cls.credential_kinds, "providers requiring credentials must declare accepted kinds"
    if cls.implementation_status is ImplementationStatus.PLANNED:
        assert cls.planned_phase is not None
    else:
        assert capability_contract_violations(cls) == []


def test_vsphere_requires_a_username_and_is_instantiable_without_network_access() -> None:
    with pytest.raises(ValidationFailedError):
        provider_registry.create(ProviderType.VSPHERE, {"server": "vc", "datacenter": "dc1"}, credential=b"pw")
    provider = provider_registry.create(
        ProviderType.VSPHERE, {"server": "vc", "datacenter": "dc1", "username": "ops"}, credential=b"pw"
    )
    assert provider.implementation_status is ImplementationStatus.PREVIEW


def test_config_validation_reports_field_errors() -> None:
    with pytest.raises(ValidationFailedError) as exc_info:
        provider_registry.validate_config(ProviderType.AWS, {"region": "not a region"})
    assert exc_info.value.details["errors"]


def test_valid_config_is_parsed() -> None:
    config = provider_registry.validate_config(
        ProviderType.AWS, {"access_key_id": "AKIAIOSFODNN7EXAMPLE", "region": "us-east-1"}
    )
    assert config.region == "us-east-1"  # type: ignore[attr-defined]


# ---- The contract itself, exercised with a minimal in-test provider ------------------


class _FakeConfig(BaseModel):
    endpoint: str = "fake://"


class _FakeProvider(InfrastructureProvider):
    provider_type = ProviderType.LOCAL
    display_name = "Fake"
    description = "Test double"
    category = ProviderCategory.LOCAL
    capabilities = frozenset({ProviderCapability.VALIDATE_CONNECTION})
    implementation_status = ImplementationStatus.AVAILABLE
    config_model = _FakeConfig
    credential_required = False

    def validate_connection(self) -> ConnectionCheckResult:
        return ConnectionCheckResult(ok=True, message="reachable")


def test_available_provider_lifecycle_and_unsupported_capabilities() -> None:
    registry = ProviderRegistry()
    registry.register(_FakeProvider)
    provider = registry.create(ProviderType.LOCAL, {}, credential=None)
    assert provider.validate_connection().ok
    with pytest.raises(CapabilityNotSupportedError):
        provider.delete(MachineRef(external_id="x"))
    assert capability_contract_violations(_FakeProvider) == []


def test_contract_checker_detects_missing_override() -> None:
    class Broken(_FakeProvider):
        capabilities = frozenset({ProviderCapability.PROVISION})

    assert capability_contract_violations(Broken)


def test_duplicate_registration_rejected() -> None:
    registry = ProviderRegistry()
    registry.register(_FakeProvider)
    with pytest.raises(ValueError):
        registry.register(_FakeProvider)

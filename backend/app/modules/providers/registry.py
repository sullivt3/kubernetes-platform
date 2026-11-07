"""Registry of infrastructure provider implementations.

The rest of the application looks providers up here by ``ProviderType``; nothing
outside ``app.modules.providers.implementations`` imports a concrete provider.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.errors import NotImplementedYetError, ValidationFailedError
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.types import ImplementationStatus, ProviderType


class ProviderDescriptor(BaseModel):
    type: ProviderType
    display_name: str
    description: str
    category: str
    implementation_status: ImplementationStatus
    planned_phase: int | None
    capabilities: list[str]
    credential_kinds: list[str]
    credential_required: bool
    config_schema: dict[str, Any]


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[ProviderType, type[InfrastructureProvider]] = {}

    def register(self, provider_cls: type[InfrastructureProvider]) -> type[InfrastructureProvider]:
        if provider_cls.provider_type in self._providers:
            raise ValueError(f"Provider '{provider_cls.provider_type}' is already registered")
        self._providers[provider_cls.provider_type] = provider_cls
        return provider_cls

    def get_class(self, provider_type: ProviderType) -> type[InfrastructureProvider]:
        try:
            return self._providers[provider_type]
        except KeyError:
            raise ValidationFailedError(f"Unknown provider type '{provider_type}'") from None

    def all(self) -> list[type[InfrastructureProvider]]:
        return list(self._providers.values())

    def validate_config(self, provider_type: ProviderType, config: dict[str, Any]) -> BaseModel:
        cls = self.get_class(provider_type)
        try:
            return cls.config_model.model_validate(config)
        except ValidationError as exc:
            raise ValidationFailedError(
                f"Invalid configuration for provider '{provider_type}'",
                details={"errors": exc.errors(include_url=False, include_context=False)},
            ) from exc

    def create(
        self, provider_type: ProviderType, config: dict[str, Any], credential: bytes | None
    ) -> InfrastructureProvider:
        cls = self.get_class(provider_type)
        if cls.implementation_status is ImplementationStatus.PLANNED:
            raise NotImplementedYetError(
                f"The {cls.display_name} provider is not implemented yet"
                + (f" (planned for Phase {cls.planned_phase})" if cls.planned_phase else ""),
                details={"provider": provider_type.value},
            )
        if cls.credential_required and credential is None:
            raise ValidationFailedError(f"Provider '{provider_type}' requires a credential")
        return cls(self.validate_config(provider_type, config), credential)

    @staticmethod
    def describe(cls: type[InfrastructureProvider]) -> ProviderDescriptor:
        return ProviderDescriptor(
            type=cls.provider_type,
            display_name=cls.display_name,
            description=cls.description,
            category=cls.category.value,
            implementation_status=cls.implementation_status,
            planned_phase=cls.planned_phase,
            capabilities=sorted(c.value for c in cls.capabilities),
            credential_kinds=sorted(k.value for k in cls.credential_kinds),
            credential_required=cls.credential_required,
            config_schema=cls.config_model.model_json_schema(),
        )

    def describe_all(self) -> list[ProviderDescriptor]:
        return [self.describe(c) for c in self._providers.values()]


def build_default_registry() -> ProviderRegistry:
    from app.modules.providers.implementations import ALL_PROVIDERS

    registry = ProviderRegistry()
    for provider_cls in ALL_PROVIDERS:
        registry.register(provider_cls)
    return registry


provider_registry = build_default_registry()

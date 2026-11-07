"""Amazon Web Services provider: EC2 instances and networking."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.modules.credentials.models import CredentialKind
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.types import (
    ImplementationStatus,
    ProviderCapability,
    ProviderCategory,
    ProviderType,
)


class AwsConfig(BaseModel):
    region: str = Field(pattern=r"^[a-z]{2}(-gov)?-[a-z]+-\d$", description="e.g. us-east-1")
    vpc_id: str | None = Field(default=None, pattern=r"^vpc-[0-9a-f]+$")
    subnet_ids: list[str] = Field(default_factory=list)
    assume_role_arn: str | None = None


class AwsProvider(InfrastructureProvider):
    provider_type = ProviderType.AWS
    display_name = "Amazon Web Services"
    description = "EC2 instances for self-managed clusters, and the account context for EKS."
    category = ProviderCategory.CLOUD
    capabilities = frozenset(set(ProviderCapability))
    implementation_status = ImplementationStatus.PLANNED
    planned_phase = 5
    config_model = AwsConfig
    credential_kinds = frozenset({CredentialKind.AWS_ACCESS_KEY})

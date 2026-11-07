"""Encrypted credential storage."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonDict, OrganizationScoped, Timestamps, UUIDPrimaryKey, str_enum


class CredentialKind(StrEnum):
    SSH_PRIVATE_KEY = "ssh_private_key"
    SSH_PASSWORD = "ssh_password"
    KUBECONFIG = "kubeconfig"
    PROXMOX_API_TOKEN = "proxmox_api_token"
    VSPHERE_PASSWORD = "vsphere_password"
    AWS_ACCESS_KEY = "aws_access_key"
    AZURE_SERVICE_PRINCIPAL = "azure_service_principal"
    GCP_SERVICE_ACCOUNT = "gcp_service_account"
    GENERIC_TOKEN = "generic_token"


class Credential(UUIDPrimaryKey, Timestamps, OrganizationScoped, Base):
    """A secret (SSH key, kubeconfig, cloud key...).

    ``ciphertext`` is produced by the configured SecretStore and is NEVER returned
    by the API. ``public_metadata`` holds only non-secret descriptive values
    (for example an SSH username or AWS access key ID) supplied explicitly as
    non-secret by the user.
    """

    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[CredentialKind] = mapped_column(str_enum(CredentialKind, 48))
    description: Mapped[str | None] = mapped_column(Text)
    public_metadata: Mapped[JsonDict] = mapped_column(default=dict)

    secret_backend: Mapped[str] = mapped_column(String(32))
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    rotated_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]

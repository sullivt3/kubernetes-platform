"""Reviewed Helm plans; values remain encrypted and never enter operation parameters."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, LargeBinary, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonDict, OrganizationScoped, Timestamps, UUIDPrimaryKey


class HelmPlan(UUIDPrimaryKey, OrganizationScoped, Timestamps, Base):
    __tablename__ = "helm_plans"
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[str] = mapped_column(String(255))
    specification: Mapped[JsonDict]
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime]
    approved_at: Mapped[datetime | None]
    operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operations.id", ondelete="SET NULL"))


class HelmRepositoryCredential(UUIDPrimaryKey, OrganizationScoped, Timestamps, Base):
    __tablename__ = "helm_repository_credentials"
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), index=True)
    repository_url: Mapped[str] = mapped_column(Text)
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    revoked: Mapped[bool] = mapped_column(default=False, server_default=false())

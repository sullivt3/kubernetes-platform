"""Tenant-scoped applications and encrypted immutable revision reviews."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonDict, OrganizationScoped, Timestamps, UUIDPrimaryKey


class GitOpsApplication(UUIDPrimaryKey, OrganizationScoped, Timestamps, Base):
    __tablename__ = "gitops_applications"
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(63))
    configuration: Mapped[JsonDict]
    generation: Mapped[int] = mapped_column(default=1)
    key_id: Mapped[str | None] = mapped_column(String(64))
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    observation: Mapped[JsonDict] = mapped_column(default=dict)
    inventory: Mapped[JsonDict] = mapped_column(default=dict)
    automation: Mapped[JsonDict] = mapped_column(default=dict, server_default="{}")
    next_reconcile_at: Mapped[datetime | None] = mapped_column(index=True)


class GitOpsReview(UUIDPrimaryKey, OrganizationScoped, Timestamps, Base):
    __tablename__ = "gitops_reviews"
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gitops_applications.id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[int]
    created_by: Mapped[str] = mapped_column(String(255))
    commit_sha: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    summary: Mapped[JsonDict]
    expires_at: Mapped[datetime]
    approved_at: Mapped[datetime | None]
    operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operations.id", ondelete="SET NULL"))

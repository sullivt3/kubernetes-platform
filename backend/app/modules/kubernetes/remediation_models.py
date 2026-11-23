"""Durable observations and administrator approvals for bounded remediation."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonDict, OrganizationScoped, Timestamps, UUIDPrimaryKey


class RemediationRequest(UUIDPrimaryKey, Timestamps, OrganizationScoped, Base):
    __tablename__ = "remediation_requests"
    __table_args__ = (UniqueConstraint("cluster_id", "pod_uid"),)

    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), index=True)
    namespace: Mapped[str] = mapped_column(String(63))
    pod_name: Mapped[str] = mapped_column(String(253))
    pod_uid: Mapped[str] = mapped_column(String(128))
    snapshot: Mapped[JsonDict]
    reason: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="observing")
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    expires_at: Mapped[datetime | None]
    approved_at: Mapped[datetime | None]
    approved_by: Mapped[str | None] = mapped_column(String(255))
    operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operations.id", ondelete="SET NULL"))

"""Encrypted, identity-bound reviews for dynamic custom resource mutations."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonDict, OrganizationScoped, Timestamps, UUIDPrimaryKey


class CustomResourceReview(UUIDPrimaryKey, OrganizationScoped, Timestamps, Base):
    __tablename__ = "custom_resource_reviews"
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[str] = mapped_column(String(255))
    specification: Mapped[JsonDict]
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime]
    result: Mapped[JsonDict | None]

"""Tenancy and identity: organizations, projects and users.

Organization/project isolation is part of the schema from day one: every
tenant-owned table carries ``organization_id`` (and optionally ``project_id``)
via :class:`app.db.base.OrganizationScoped`. Memberships and fine-grained
application RBAC arrive in Phase 7.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class Organization(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "organizations"

    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(255))


class Project(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("organization_id", "slug"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    slug: Mapped[str] = mapped_column(String(63))
    name: Mapped[str] = mapped_column(String(255))


class User(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "users"

    # Stable identity from the identity provider ("iss|sub" for OIDC, "dev|local" in dev mode).
    subject: Mapped[str] = mapped_column(String(512), unique=True)
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    last_seen_at: Mapped[datetime | None]

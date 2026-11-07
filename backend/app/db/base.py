"""Declarative base, shared column mixins and type conventions."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import DateTime, Enum, ForeignKey, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# Deterministic constraint names make Alembic migrations reviewable and portable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JsonDict = dict[str, Any]
JsonList = list[Any]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: DateTime(timezone=True),
        JsonDict: JSONB,
        JsonList: JSONB,
    }


def str_enum(enum_cls: type[StrEnum], length: int = 32) -> Enum:
    """Store enums as plain strings without a DB-level enum type or CHECK constraint.

    New providers, distributions and states are added frequently; keeping enum
    validation in the application avoids a schema migration for each new value.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class OrganizationScoped:
    """Every tenant-owned resource belongs to an organization and optionally a project."""

    @declared_attr
    def organization_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), index=True)

    @declared_attr
    def project_id(cls) -> Mapped[uuid.UUID | None]:
        return mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), index=True)

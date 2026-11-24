"""Session leases, namespace assignments and opt-in encrypted transcript chunks."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonDict, OrganizationScoped, Timestamps, UUIDPrimaryKey


class CommandSession(UUIDPrimaryKey, OrganizationScoped, Timestamps, Base):
    __tablename__ = "command_sessions"
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime]
    heartbeat_at: Mapped[datetime | None]
    operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("operations.id", ondelete="SET NULL"))
    specification: Mapped[JsonDict]


class CommandNamespaceGrant(UUIDPrimaryKey, OrganizationScoped, Timestamps, Base):
    __tablename__ = "command_namespace_grants"
    __table_args__ = (UniqueConstraint("cluster_id", "user_id", "namespace"),)
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    namespace: Mapped[str] = mapped_column(String(63))
    namespace_uid: Mapped[str] = mapped_column(String(255))


class CommandTranscriptChunk(Base):
    __tablename__ = "command_transcript_chunks"
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("command_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(primary_key=True)
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime] = mapped_column(index=True)


def require_closed_sessions(session, cluster_id):
    from sqlalchemy import select

    from app.core.errors import InvalidStateError

    if session.scalar(
        select(CommandSession.id)
        .where(CommandSession.cluster_id == cluster_id, CommandSession.status != "closed")
        .limit(1)
    ):
        raise InvalidStateError(
            "End interactive sessions and finish cleanup before changing connectivity or removing the cluster"
        )

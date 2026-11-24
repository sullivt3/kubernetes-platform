"""Session leases and cleanup metadata; never store tokens or terminal transcripts."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
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

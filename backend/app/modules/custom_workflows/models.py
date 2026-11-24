import uuid

from sqlalchemy import ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, OrganizationScoped, Timestamps, UUIDPrimaryKey


class CustomWorkflow(UUIDPrimaryKey, OrganizationScoped, Timestamps, Base):
    __tablename__ = "custom_workflows"
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(63))
    generation: Mapped[int] = mapped_column(default=1)
    step_count: Mapped[int]
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)

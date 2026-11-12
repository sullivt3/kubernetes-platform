"""Persist observed machine connectivity separately from installation state."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("machines", sa.Column("connectivity_status", sa.String(16), nullable=False, server_default="unknown"))
    op.add_column("machines", sa.Column("connectivity_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("machines", sa.Column("connectivity_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("machines", "connectivity_error")
    op.drop_column("machines", "connectivity_checked_at")
    op.drop_column("machines", "connectivity_status")

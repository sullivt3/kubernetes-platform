"""Store each cluster's optional Loki Service connection."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clusters", sa.Column("loki_connection", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("clusters", "loki_connection")

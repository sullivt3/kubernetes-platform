"""Store each cluster's optional Prometheus Service connection."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clusters", sa.Column("prometheus_connection", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("clusters", "prometheus_connection")

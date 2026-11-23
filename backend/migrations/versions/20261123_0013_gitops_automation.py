"""Opt-in GitOps reconciliation policy and persisted scheduling."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "gitops_applications",
        sa.Column("automation", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column("gitops_applications", sa.Column("next_reconcile_at", sa.DateTime(timezone=True)))
    op.create_index("ix_gitops_applications_next_reconcile_at", "gitops_applications", ["next_reconcile_at"])


def downgrade() -> None:
    op.drop_index("ix_gitops_applications_next_reconcile_at", table_name="gitops_applications")
    op.drop_column("gitops_applications", "next_reconcile_at")
    op.drop_column("gitops_applications", "automation")

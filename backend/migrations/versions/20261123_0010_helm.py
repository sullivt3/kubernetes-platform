"""Persist chart repositories and encrypted, reviewed release plans."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clusters", sa.Column("helm_repositories", postgresql.JSONB(), nullable=True))
    op.create_table(
        "helm_plans",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="RESTRICT")),
        sa.Column("cluster_id", sa.UUID(), sa.ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("specification", postgresql.JSONB(), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("operation_id", sa.UUID(), sa.ForeignKey("operations.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for column in ("organization_id", "project_id", "cluster_id"):
        op.create_index(f"ix_helm_plans_{column}", "helm_plans", [column])


def downgrade() -> None:
    op.drop_table("helm_plans")
    op.drop_column("clusters", "helm_repositories")

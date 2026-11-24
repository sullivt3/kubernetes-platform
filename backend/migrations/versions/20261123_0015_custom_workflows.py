"""Encrypted reusable custom workflow definitions."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "custom_workflows",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="RESTRICT")),
        sa.Column("cluster_id", sa.UUID(), sa.ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("step_count", sa.Integer(), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for column in ("organization_id", "project_id", "cluster_id"):
        op.create_index("ix_custom_workflows_" + column, "custom_workflows", [column])


def downgrade():
    op.drop_table("custom_workflows")

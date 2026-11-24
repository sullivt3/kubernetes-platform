"""Explicit user/namespace assignments for read-only interactive access."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "command_namespace_grants",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="RESTRICT")),
        sa.Column("cluster_id", sa.UUID(), sa.ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("namespace", sa.String(63), nullable=False),
        sa.Column("namespace_uid", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("cluster_id", "user_id", "namespace"),
    )
    for column in ("organization_id", "project_id", "cluster_id"):
        op.create_index("ix_command_namespace_grants_" + column, "command_namespace_grants", [column])


def downgrade():
    op.drop_table("command_namespace_grants")

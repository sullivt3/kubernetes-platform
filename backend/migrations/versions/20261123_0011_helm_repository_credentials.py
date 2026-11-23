"""Encrypted, revocable credentials bound to each cluster and repository URL."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "helm_repository_credentials",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="RESTRICT")),
        sa.Column("cluster_id", sa.UUID(), sa.ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("repository_url", sa.Text(), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("revoked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for column in ("cluster_id", "organization_id", "project_id"):
        op.create_index(f"ix_helm_repository_credentials_{column}", "helm_repository_credentials", [column])


def downgrade() -> None:
    op.drop_table("helm_repository_credentials")

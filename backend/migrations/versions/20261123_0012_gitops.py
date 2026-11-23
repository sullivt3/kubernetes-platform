"""GitOps applications and encrypted commit reviews."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def common():
    return [
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="RESTRICT")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade():
    op.create_table(
        "gitops_applications",
        *common(),
        sa.Column("cluster_id", sa.UUID(), sa.ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("key_id", sa.String(64)),
        sa.Column("ciphertext", sa.LargeBinary()),
        sa.Column("observation", postgresql.JSONB(), nullable=False),
        sa.Column("inventory", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "gitops_reviews",
        *common(),
        sa.Column(
            "application_id", sa.UUID(), sa.ForeignKey("gitops_applications.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("operation_id", sa.UUID(), sa.ForeignKey("operations.id", ondelete="SET NULL")),
    )
    for table, target in [("gitops_applications", "cluster_id"), ("gitops_reviews", "application_id")]:
        for column in ("organization_id", "project_id", target):
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    op.drop_table("gitops_reviews")
    op.drop_table("gitops_applications")

"""Persist opt-in remediation policies, observations and approvals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clusters", sa.Column("remediation_policy", postgresql.JSONB(), nullable=True))
    op.create_table(
        "remediation_requests",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organization_id", sa.UUID(), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="RESTRICT")),
        sa.Column("cluster_id", sa.UUID(), sa.ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("namespace", sa.String(63), nullable=False),
        sa.Column("pod_name", sa.String(253), nullable=False),
        sa.Column("pod_uid", sa.String(128), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("approved_by", sa.String(255)),
        sa.Column("operation_id", sa.UUID(), sa.ForeignKey("operations.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("cluster_id", "pod_uid"),
    )
    for column in ("organization_id", "project_id", "cluster_id"):
        op.create_index(f"ix_remediation_requests_{column}", "remediation_requests", [column])


def downgrade() -> None:
    op.drop_table("remediation_requests")
    op.drop_column("clusters", "remediation_policy")

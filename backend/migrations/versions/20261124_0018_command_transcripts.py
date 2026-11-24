"""Opt-in encrypted terminal output with bounded retention."""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "command_transcript_chunks",
        sa.Column("session_id", sa.UUID(), sa.ForeignKey("command_sessions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_command_transcript_chunks_expires_at", "command_transcript_chunks", ["expires_at"])


def downgrade():
    op.drop_table("command_transcript_chunks")

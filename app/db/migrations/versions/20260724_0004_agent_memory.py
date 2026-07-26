"""add agent memory

Revision ID: 20260724_0004
Revises: 20260724_0003
Create Date: 2026-07-24 00:00:04
"""

from alembic import op
import sqlalchemy as sa


revision = "20260724_0004"
down_revision = "20260724_0003"
branch_labels = None
depends_on = None


memory_scope = sa.Enum("task", "repository", name="memoryscope")
memory_kind = sa.Enum(
    "localization",
    "failure",
    "solution",
    "review",
    name="memorykind",
)


def upgrade() -> None:
    op.create_table(
        "agent_memories",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "repository_id",
            sa.Integer(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(length=36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("scope", memory_scope, nullable=False),
        sa.Column("kind", memory_kind, nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_commit", sa.Text(), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_agent_memories_repository_id",
        "agent_memories",
        ["repository_id"],
    )
    op.create_index("ix_agent_memories_task_id", "agent_memories", ["task_id"])
    op.create_index("ix_agent_memories_scope", "agent_memories", ["scope"])
    op.create_index("ix_agent_memories_kind", "agent_memories", ["kind"])
    op.create_index(
        "ix_agent_memories_fingerprint",
        "agent_memories",
        ["fingerprint"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_memories_fingerprint", table_name="agent_memories")
    op.drop_index("ix_agent_memories_kind", table_name="agent_memories")
    op.drop_index("ix_agent_memories_scope", table_name="agent_memories")
    op.drop_index("ix_agent_memories_task_id", table_name="agent_memories")
    op.drop_index("ix_agent_memories_repository_id", table_name="agent_memories")
    op.drop_table("agent_memories")
    memory_kind.drop(op.get_bind(), checkfirst=False)
    memory_scope.drop(op.get_bind(), checkfirst=False)

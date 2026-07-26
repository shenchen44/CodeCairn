"""add generic agent phase artifact type

Revision ID: 20260724_0003
Revises: 20260409_0002
Create Date: 2026-07-24 00:00:03
"""

from alembic import op


revision = "20260724_0003"
down_revision = "20260409_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE taskartifacttype ADD VALUE IF NOT EXISTS 'agent_phase'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely while rows may reference them.
    pass

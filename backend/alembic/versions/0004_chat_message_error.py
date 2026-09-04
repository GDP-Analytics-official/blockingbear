"""Persist user-facing chat turn failures.

Provider failures used to live only in the transient SSE stream. After a
refresh the user could see the submitted prompt with no response and no
explanation. The error belongs to the assistant-side turn marker and is not
part of the OpenRouter message history.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("chat_messages") as batch:
        batch.add_column(sa.Column("error", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("chat_messages") as batch:
        batch.drop_column("error")

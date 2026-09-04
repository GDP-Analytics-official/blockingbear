"""First-access tutorial flag on the user profile.

The frontend shows a short guided tour to users entering the app for the
first time. Whether it was completed (or skipped) lives on the profile, like
the interface language, so it follows the person and not the browser.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-27
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("tour_done", sa.Boolean(), nullable=False,
                                   server_default="0"))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("tour_done")

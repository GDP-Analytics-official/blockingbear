# -*- coding: utf-8 -*-
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

NB: ogni revisione deve girare su ENTRAMBI i dialetti (SQLite e Postgres).
Su SQLite le ALTER TABLE vanno fatte in batch mode (op.batch_alter_table),
che env.py predispone gia' con render_as_batch."""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade():
    ${upgrades if upgrades else "pass"}


def downgrade():
    ${downgrades if downgrades else "pass"}

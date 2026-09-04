"""chat_messages.tool_call_id: VARCHAR(64) -> TEXT.

L'id lo genera il MODELLO e va rispedito a OpenRouter identico: non si può
troncare (spezzerebbe l'aggancio coi tool_calls del messaggio assistant).
Su SQLite le lunghezze sono decorative e nessuno se n'era mai accorto; su
Postgres un id oltre i 64 caratteri faceva fallire la scrittura del turno.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    # batch: su SQLite il cambio di tipo richiede il rebuild atomico della
    # tabella (una tantum); sugli altri dialetti è una ALTER COLUMN normale
    with op.batch_alter_table("chat_messages") as batch:
        batch.alter_column("tool_call_id", existing_type=sa.String(64),
                           type_=sa.Text(), existing_nullable=True)


def downgrade():
    with op.batch_alter_table("chat_messages") as batch:
        batch.alter_column("tool_call_id", existing_type=sa.Text(),
                           type_=sa.String(64), existing_nullable=True)

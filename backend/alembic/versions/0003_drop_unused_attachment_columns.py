"""attachments: via due colonne che nessuno scrive.

`anonymization_requested` e `job_id` appartenevano al flusso in cui ogni
allegato veniva anonimizzato da un job proprio, all'upload. Gli allegati delle
chat si anonimizzano nel TURNO (chat_anonymization.anonymize_turn), tutti
insieme e sotto un solo registro, quindi non c'è né una richiesta da
registrare né un job da agganciare al singolo file.

Le altre colonne della stessa tabella restano: original_path, protected_path,
display_filename, model_filename, model_briefing_json,
anonymization_report_json e mapping_version sono il cuore del flusso attuale.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    # batch: su SQLite togliere una colonna vuol dire ricostruire la tabella
    # (una tantum); sugli altri dialetti è una DROP COLUMN normale
    with op.batch_alter_table("attachments") as batch:
        batch.drop_column("anonymization_requested")
        batch.drop_column("job_id")


def downgrade():
    with op.batch_alter_table("attachments") as batch:
        batch.add_column(sa.Column("anonymization_requested", sa.Integer(),
                                   nullable=False, server_default="0"))
        batch.add_column(sa.Column("job_id", sa.String(32), nullable=True))

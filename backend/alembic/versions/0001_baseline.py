"""Baseline: fotografia dello schema al 22 agosto 2026 + ADOZIONE dei
blockingbear.db esistenti.

Questa revisione è il punto zero e fa due mestieri, per ogni tabella:

  - se la tabella NON esiste (database vuoto: primo avvio SQLite, o Postgres
    appena creato) la crea con lo schema corrente;
  - se ESISTE (un blockingbear.db creato prima di Alembic, quando lo schema lo
    faceva init_db con create_all + micro-migrazioni artigianali) applica gli
    stessi delta idempotenti che init_db applicava a mano: colonne aggiunte
    nel tempo, rebuild della tabella alias con la UNIQUE sbagliata, indici.
    È il ramo di adozione: porta QUALUNQUE blockingbear.db storico allo stesso
    identico stato della creazione da zero, senza perdita di dati.

Così tutti i database passano dallo stesso `upgrade head` e le revisioni
successive (0002, ...) partono da uno stato unico e noto. Il ramo di adozione
può toccare solo SQLite: un database Postgres con tabelle nostre ma senza
versione Alembic non è uno stato legittimo e si rifiuta con errore chiaro.

NOTA sullo schema: conversation_entities.conv_id NON ha la ForeignKey verso
conversations — è uno SCOPE id che per i progetti contiene un project_id
(vedi il modello in app/db.py). Nei vecchi blockingbear.db la FK esiste nel DDL
ma SQLite non l'ha mai applicata (PRAGMA foreign_keys spento): è innocua e
si lascia dov'è.

Revision ID: 0001
Revises:
Create Date: 2026-08-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# le tabelle di BlockingBear (per riconoscere un DB già abitato)
_TABLES = ("users", "settings", "jobs", "projects", "project_files",
           "conversations", "chat_messages", "conversation_entities",
           "conversation_entity_aliases", "attachments")


def _cols(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def _add_missing(bind, insp, table, deltas):
    """Il cuore dell'adozione: le ALTER TABLE che init_db faceva a mano,
    identiche (stessi DDL, stessi default). Idempotente per colonna."""
    have = _cols(insp, table)
    for name, ddl in deltas:
        if name not in have:
            bind.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {t for t in insp.get_table_names() if t in _TABLES}
    if existing and bind.dialect.name != "sqlite":
        raise RuntimeError(
            "Il database ha già tabelle di BlockingBear ma nessuna versione "
            "Alembic: solo i blockingbear.db SQLite storici si adottano così. "
            "Su Postgres partire da un database vuoto.")

    # --- users ---------------------------------------------------------------
    if "users" not in existing:
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("username", sa.String(64), nullable=False, unique=True),
            sa.Column("password_hash", sa.String(256), nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("openrouter_key", sa.Text()),
            sa.Column("openrouter_key_hash", sa.String(128)),
            sa.Column("chat_model_rule", sa.Text()),
            sa.Column("anon_terms_json", sa.Text()),
            sa.Column("must_change_password", sa.Boolean(), nullable=False,
                      server_default="0"),
            sa.Column("lang", sa.String(8)),
        )
    else:
        _add_missing(bind, insp, "users", (
            ("openrouter_key", "TEXT"),
            ("openrouter_key_hash", "VARCHAR(128)"),
            ("chat_model_rule", "TEXT"),
            ("anon_terms_json", "TEXT"),
            ("lang", "VARCHAR(8)"),
            ("must_change_password", "BOOLEAN NOT NULL DEFAULT 0")))

    # tabella di una versione precedente dell'app, senza più un modello
    op.execute("DROP TABLE IF EXISTS documents")

    # --- settings ------------------------------------------------------------
    if "settings" not in existing:
        op.create_table(
            "settings",
            sa.Column("key", sa.String(64), primary_key=True),
            sa.Column("value", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )

    # --- jobs ----------------------------------------------------------------
    if "jobs" not in existing:
        op.create_table(
            "jobs",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"),
                      nullable=False),
            sa.Column("filename", sa.String(256), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("error", sa.Text()),
            sa.Column("doc_id", sa.String(32)),
            sa.Column("project_id", sa.String(32)),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
        )
    else:
        _add_missing(bind, insp, "jobs", (
            ("kind", "VARCHAR(16) NOT NULL DEFAULT 'project_upload'"),
            ("project_id", "VARCHAR(32)")))

    # --- projects ------------------------------------------------------------
    if "projects" not in existing:
        op.create_table(
            "projects",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"),
                      nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("anonymized", sa.Integer(), nullable=False),
            sa.Column("anon_options_json", sa.Text(), nullable=False),
            sa.Column("mapping_version", sa.Integer(), nullable=False),
            sa.Column("skip_unconfirmed_warning", sa.Integer(),
                      nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
    else:
        _add_missing(bind, insp, "projects", (
            ("skip_unconfirmed_warning", "INTEGER NOT NULL DEFAULT 0"),))

    # --- project_files -------------------------------------------------------
    if "project_files" not in existing:
        op.create_table(
            "project_files",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("project_id", sa.String(32),
                      sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("filename", sa.String(256), nullable=False),
            sa.Column("model_filename", sa.String(256)),
            sa.Column("mime", sa.String(128)),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("n_pages", sa.Integer(), nullable=False),
            sa.Column("rev", sa.Integer(), nullable=False),
            sa.Column("confirmed", sa.Integer(), nullable=False),
            sa.Column("mapping_version", sa.Integer(), nullable=False),
            sa.Column("stale_version", sa.Integer(), nullable=False),
            sa.Column("stale_json", sa.Text(), nullable=False),
            sa.Column("n_images", sa.Integer(), nullable=False),
            sa.Column("briefing_json", sa.Text()),
            sa.Column("model_briefing_json", sa.Text()),
            sa.Column("report_json", sa.Text(), nullable=False),
            sa.Column("by_label_json", sa.Text(), nullable=False),
            sa.Column("original_boxes_json", sa.Text(), nullable=False),
            sa.Column("anonymized_boxes_json", sa.Text(), nullable=False),
            sa.Column("page_sizes_json", sa.Text(), nullable=False),
            sa.Column("sealed_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )
    else:
        # -1 = "mai controllato": i file già caricati vengono verificati
        # alla prima apertura del progetto, non dati per allineati
        _add_missing(bind, insp, "project_files", (
            ("stale_version", "INTEGER NOT NULL DEFAULT -1"),
            ("stale_json", "TEXT NOT NULL DEFAULT '[]'")))

    # --- conversations -------------------------------------------------------
    if "conversations" not in existing:
        op.create_table(
            "conversations",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"),
                      nullable=False),
            sa.Column("project_id", sa.String(32)),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("model", sa.String(128), nullable=False),
            sa.Column("options_json", sa.Text(), nullable=False),
            sa.Column("anonymized", sa.Integer(), nullable=False),
            sa.Column("anon_options_json", sa.Text(), nullable=False),
            sa.Column("mapping_version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
    else:
        _add_missing(bind, insp, "conversations", (
            ("mapping_version", "INTEGER NOT NULL DEFAULT 0"),
            ("anonymized", "INTEGER NOT NULL DEFAULT 0"),
            ("anon_options_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("project_id", "VARCHAR(32)")))

    # --- chat_messages -------------------------------------------------------
    if "chat_messages" not in existing:
        op.create_table(
            "chat_messages",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("conv_id", sa.String(32),
                      sa.ForeignKey("conversations.id"), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("content", sa.Text()),
            sa.Column("display_content", sa.Text()),
            sa.Column("model_content", sa.Text()),
            sa.Column("anonymized", sa.Integer()),
            sa.Column("mapping_version", sa.Integer(), nullable=False),
            sa.Column("tool_calls_json", sa.Text()),
            sa.Column("tool_call_id", sa.String(64)),
            sa.Column("reasoning_json", sa.Text()),
            sa.Column("model", sa.String(128)),
            sa.Column("finish_reason", sa.String(32)),
            sa.Column("usage_json", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )
    else:
        _add_missing(bind, insp, "chat_messages", (
            ("display_content", "TEXT"),
            ("model_content", "TEXT"),
            ("anonymized", "INTEGER"),
            ("mapping_version", "INTEGER NOT NULL DEFAULT 0")))

    # --- conversation_entities (conv_id = SCOPE id, MAI ForeignKey) ----------
    if "conversation_entities" not in existing:
        op.create_table(
            "conversation_entities",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("conv_id", sa.String(32), nullable=False),
            sa.Column("placeholder", sa.String(96), nullable=False),
            sa.Column("label", sa.String(64), nullable=False),
            sa.Column("canonical_value", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("mapping_version", sa.Integer(), nullable=False),
            sa.Column("merged_into", sa.String(32)),
            sa.Column("merge_checked", sa.Integer(), nullable=False),
            sa.Column("excluded", sa.Integer(), nullable=False),
            sa.UniqueConstraint("conv_id", "placeholder",
                                name="uq_conv_entity_placeholder"),
        )
    else:
        _add_missing(bind, insp, "conversation_entities", (
            ("merged_into", "VARCHAR(32)"),
            ("merge_checked", "INTEGER NOT NULL DEFAULT 0"),
            ("excluded", "INTEGER NOT NULL DEFAULT 0")))

    # --- conversation_entity_aliases -----------------------------------------
    if "conversation_entity_aliases" not in existing:
        op.create_table(
            "conversation_entity_aliases",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("entity_id", sa.String(32),
                      sa.ForeignKey("conversation_entities.id"),
                      nullable=False),
            sa.Column("original_surface", sa.Text(), nullable=False),
            sa.Column("normalized_key", sa.Text(), nullable=False),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("confidence", sa.String(16), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("mapping_version", sa.Integer(), nullable=False),
            sa.UniqueConstraint("entity_id", "original_surface",
                                name="uq_entity_alias_surface"),
        )
    else:
        # Una build preliminare usava UNIQUE(entity_id, normalized_key), che
        # impediva di conservare due superfici ("Azienda X Srl" / maiuscolo)
        # con la stessa normalizzazione. SQLite richiede il rebuild atomico.
        bad_alias_unique = False
        for idx in bind.exec_driver_sql(
                "PRAGMA index_list(conversation_entity_aliases)"):
            if not idx[2]:
                continue
            cols = [row[2] for row in bind.exec_driver_sql(
                f"PRAGMA index_info('{idx[1]}')")]
            if cols == ["entity_id", "normalized_key"]:
                bad_alias_unique = True
        if bad_alias_unique:
            bind.exec_driver_sql(
                "ALTER TABLE conversation_entity_aliases RENAME TO "
                "conversation_entity_aliases_legacy")
            bind.exec_driver_sql("""
                CREATE TABLE conversation_entity_aliases (
                    id VARCHAR(32) NOT NULL PRIMARY KEY,
                    entity_id VARCHAR(32) NOT NULL,
                    original_surface TEXT NOT NULL,
                    normalized_key TEXT NOT NULL,
                    source VARCHAR(32) NOT NULL DEFAULT 'detector',
                    confidence VARCHAR(16) NOT NULL DEFAULT 'exact',
                    created_at DATETIME,
                    CONSTRAINT uq_entity_alias_surface
                        UNIQUE (entity_id, original_surface),
                    FOREIGN KEY(entity_id) REFERENCES conversation_entities (id)
                )
            """)
            bind.exec_driver_sql("""
                INSERT OR IGNORE INTO conversation_entity_aliases
                    (id, entity_id, original_surface, normalized_key, source,
                     confidence, created_at)
                SELECT id, entity_id, original_surface, normalized_key, source,
                       confidence, created_at
                FROM conversation_entity_aliases_legacy
            """)
            bind.exec_driver_sql(
                "DROP TABLE conversation_entity_aliases_legacy")
        # Dopo l'eventuale rebuild, altrimenti la colonna verrebbe persa.
        # Alias preesistenti a versione 0 = "noti da sempre": è la lettura
        # giusta per un registro creato prima di questa colonna.
        insp_after = sa.inspect(bind)
        _add_missing(bind, insp_after, "conversation_entity_aliases", (
            ("mapping_version", "INTEGER NOT NULL DEFAULT 0"),))

    # --- attachments ---------------------------------------------------------
    if "attachments" not in existing:
        op.create_table(
            "attachments",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("conv_id", sa.String(32),
                      sa.ForeignKey("conversations.id"), nullable=False),
            sa.Column("direction", sa.String(8), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("filename", sa.String(256), nullable=False),
            sa.Column("mime", sa.String(128)),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("message_id", sa.String(32)),
            sa.Column("briefing_json", sa.Text()),
            sa.Column("anonymization_status", sa.String(16), nullable=False),
            sa.Column("anonymization_requested", sa.Integer(),
                      nullable=False),
            sa.Column("display_filename", sa.String(256)),
            sa.Column("model_filename", sa.String(256)),
            sa.Column("original_path", sa.Text()),
            sa.Column("protected_path", sa.Text()),
            sa.Column("model_briefing_json", sa.Text()),
            sa.Column("anonymization_report_json", sa.Text()),
            sa.Column("mapping_version", sa.Integer(), nullable=False),
            sa.Column("job_id", sa.String(32)),
            sa.Column("n_images", sa.Integer(), nullable=False),
            sa.Column("sealed_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )
    else:
        _add_missing(bind, insp, "attachments", (
            ("briefing_json", "TEXT"),
            ("anonymization_status", "VARCHAR(16) NOT NULL DEFAULT 'raw'"),
            ("anonymization_requested", "INTEGER NOT NULL DEFAULT 0"),
            ("display_filename", "VARCHAR(256)"),
            ("model_filename", "VARCHAR(256)"),
            ("original_path", "TEXT"),
            ("protected_path", "TEXT"),
            ("model_briefing_json", "TEXT"),
            ("anonymization_report_json", "TEXT"),
            ("mapping_version", "INTEGER NOT NULL DEFAULT 0"),
            ("job_id", "VARCHAR(32)"),
            ("n_images", "INTEGER NOT NULL DEFAULT 0"),
            ("sealed_json", "TEXT NOT NULL DEFAULT '[]'")))

    # --- indici (nomi canonici, identici sui due motori) ---------------------
    # IF NOT EXISTS copre i due rami: in adozione alcuni indici ci sono già,
    # nella creazione da zero nessuno.
    for ddl in (
            "CREATE INDEX IF NOT EXISTS ix_conv_entities_conv_id "
            "ON conversation_entities (conv_id)",
            "CREATE INDEX IF NOT EXISTS ix_conv_aliases_entity_id "
            "ON conversation_entity_aliases (entity_id)",
            "CREATE INDEX IF NOT EXISTS ix_conv_aliases_normalized "
            "ON conversation_entity_aliases (normalized_key)",
            "CREATE INDEX IF NOT EXISTS ix_conversations_project_id "
            "ON conversations (project_id)",
            "CREATE INDEX IF NOT EXISTS ix_project_files_project_id "
            "ON project_files (project_id)"):
        op.execute(ddl)


def downgrade():
    # la baseline non si annulla: "prima di Alembic" non è uno stato in cui
    # tornare, e un drop di massa butterebbe via i dati
    raise RuntimeError("La migrazione baseline non supporta il downgrade.")

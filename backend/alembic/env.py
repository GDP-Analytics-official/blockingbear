"""Ambiente Alembic: stessa sorgente di verità dell'app.

L'URL viene da app.config.DATABASE_URL (backend/.env o ambiente); quando le
migrazioni girano dentro init_db (app/db_migrate.py) la connessione arriva
già aperta in config.attributes["connection"], così si usano le stesse
impostazioni dell'engine dell'app (PRAGMA SQLite, pool Postgres)."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.config import DATABASE_URL
from app.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# metadata dei modelli: serve ad `alembic revision --autogenerate` per
# calcolare i diff rispetto al DB (le revisioni vanno comunque riviste a mano)
target_metadata = Base.metadata


def _configure_and_run(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite non sa fare quasi nessuna ALTER TABLE: le revisioni future
        # generate in batch mode ricostruiscono la tabella in modo atomico
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline():
    """Modalità --sql: emette gli statement senza connettersi."""
    context.configure(url=DATABASE_URL, target_metadata=target_metadata,
                      literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connection = config.attributes.get("connection")
    if connection is not None:          # dall'app: engine di init_db
        _configure_and_run(connection)
    else:                               # dalla CLI: engine usa e getta
        engine = create_engine(DATABASE_URL)
        try:
            with engine.connect() as conn:
                _configure_and_run(conn)
                conn.commit()
        finally:
            engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

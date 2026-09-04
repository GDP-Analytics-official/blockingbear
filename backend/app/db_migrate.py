"""Applica le migrazioni Alembic (backend/alembic/) all'avvio, da init_db.

Scelta esplicita: le migrazioni girano ALL'AVVIO del backend, non con un
comando separato — l'app è un'installazione locale zero-config e "lancia e
funziona" vale anche per gli aggiornamenti di schema. Il comando `alembic`
resta comunque usabile a mano dalla cartella backend/ (env.py legge lo stesso
DATABASE_URL dell'app), per ispezione o per generare nuove revisioni.

L'adozione dei database esistenti NON sta qui: è dentro la migrazione
baseline (alembic/versions/0001_baseline.py), che crea le tabelle mancanti e
porta a pari quelle preesistenti. Così un blockingbear.db creato prima di
Alembic, uno appena creato e un Postgres vuoto passano tutti dallo stesso
`upgrade head`, senza stamp o rami speciali."""

from pathlib import Path

from alembic import command
from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parents[1]        # backend/


def _alembic_config():
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    # percorso assoluto: il backend può girare con cwd diversa da backend/
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


def run_migrations(engine):
    """Porta lo schema a head sull'engine GIÀ creato da init_db: così le
    migrazioni girano con le stesse impostazioni di connessione dell'app
    (PRAGMA WAL e timeout su SQLite, pool su Postgres) e non aprono un
    secondo engine per conto loro."""
    cfg = _alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")

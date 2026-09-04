"""Percorsi e configurazione di AVVIO del server (tutto locale, niente cloud).

Qui vivono SOLO le scelte di installazione, consumate una volta alla partenza
del processo (worker e modello da creare, percorsi della macchina, CORS): si
impostano in backend/.env (copiare .env.example e modificare) o come variabili
d'ambiente vere (l'ambiente vince sul file) e cambiarle richiede il riavvio.

I parametri di ESERCIZIO (limiti di upload, coda, timeout, sessioni...) NON
stanno qui: sono nel DB, regolabili a caldo dalla pagina Impostazioni del
pannello admin (vedi settings_store.py).

NIENTE default nascosti nel codice: le variabili di tuning sono OBBLIGATORIE
e se ne manca una il server si ferma all'avvio con un messaggio chiaro.
Fanno eccezione solo quelle la cui assenza significa "rilevamento automatico"
(BLOCKINGBEAR_CPU_THREADS, BLOCKINGBEAR_DATA_DIR, PII_MODEL_DIR, BLOCKINGBEAR_SOFFICE_PATH).
"""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]          # backend/


def _load_dotenv():
    """Carica backend/.env nell'ambiente, SENZA sovrascrivere le variabili
    già impostate (così un override momentaneo da shell vince sul file).
    Formato: righe CHIAVE=valore, commenti con #, apici opzionali sul valore."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_dotenv()


def _require(key, allow_empty=False):
    """Variabile OBBLIGATORIA: deve stare in backend/.env o nell'ambiente.
    Meglio fermarsi subito con un messaggio chiaro che partire con un valore
    implicito diverso da quello che l'installatore credeva di avere."""
    value = os.environ.get(key)
    if value is None or (not value.strip() and not allow_empty):
        raise RuntimeError(
            f"Configurazione mancante: {key} non è impostata. Aggiungila a "
            "backend/.env (l'elenco completo commentato è in .env.example).")
    return value.strip()


def _require_int(key, minimum=1):
    value = _require(key)
    try:
        return max(minimum, int(value))
    except ValueError:
        raise RuntimeError(f"Configurazione non valida: {key}={value!r} "
                           "non è un numero intero.")


# Cartella del modello PII. Un percorso RELATIVO e' relativo a BASE_DIR
# (backend/), NON alla cwd del processo. Nel deployment Compose il checkpoint
# viene montato sotto /app/models e l'immagine imposta questo percorso. I
# percorsi ASSOLUTI passano invariati: BASE_DIR / "/opt/x" e' "/opt/x".
_MODEL_ENV = os.environ.get("PII_MODEL_DIR", "").strip()
MODEL_DIR = ((BASE_DIR / _MODEL_ENV) if _MODEL_ENV
             else BASE_DIR / "models" / "rizzo-pii-0.3B")
DATA_DIR = Path(os.environ.get("BLOCKINGBEAR_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "blockingbear.db"

# --- Database ----------------------------------------------------------------
# Backend del database. Il deployment Compose supportato imposta sempre
# DATABASE_URL verso il proprio servizio PostgreSQL. Il ripiego SQLite resta
# soltanto per test e sviluppo del codice, non come procedura di installazione.
# Vale per il SOLO database: i byte dei file restano comunque su disco in
# BLOCKINGBEAR_DATA_DIR. Le differenze tra i due motori vivono solo in db.py
# (creazione engine) e nelle migrazioni Alembic (backend/alembic/).
DATABASE_URL = ((os.environ.get("DATABASE_URL") or "").strip()
                or f"sqlite:///{DB_PATH}")

# --- Coda di elaborazione (vedi jobs.py) ---------------------------------
# Letti UNA volta all'avvio: per cambiarli si riavvia il backend.
_CORES = os.cpu_count() or 1
# Tetto TOTALE di core per l'inferenza. Il pool intra-op di torch è globale
# al processo, quindi il tetto vale qualunque sia N_WORKERS. Assente/vuota =
# rilevamento automatico (core-1): un core resta sempre libero per API/preview
# anche sotto carico pieno.
CPU_THREADS = max(1, int(os.environ.get("BLOCKINGBEAR_CPU_THREADS") or (_CORES - 1)))
# Quante inferenze in parallelo dentro quel budget. Ogni worker carica una
# PROPRIA istanza del modello (RAM x N): alzarlo conviene solo con molti
# documenti piccoli o hardware abbondante.
N_WORKERS = _require_int("BLOCKINGBEAR_N_WORKERS")

# --- Integrazione ----------------------------------------------------------
# Percorso esplicito di soffice (LibreOffice): assente/vuoto = ricerca
# automatica nel PATH e nelle posizioni standard (vedi engine/convert.py).
SOFFICE_PATH = os.environ.get("BLOCKINGBEAR_SOFFICE_PATH", "").strip() or None
# Origin CORS ammessi (separati da virgola). Servono SOLO in sviluppo, dove il
# frontend gira su Vite e chiama l'API cross-origin; in produzione il frontend
# è servito dal backend stesso e la riga VUOTA (obbligatoria comunque, scelta
# esplicita) disattiva il CORS.
CORS_ORIGINS = [o.strip() for o in
                _require("BLOCKINGBEAR_CORS_ORIGINS", allow_empty=True).split(",")
                if o.strip()]

# --- Sandbox code interpreter ----------------------------------------------
# Tutte OPZIONALI (pattern SOFFICE_PATH): assente/vuota = default indicato.
# La sandbox è una feature con degradazione elegante: senza runtime Docker/
# Podman la chat funziona comunque, solo senza esecuzione di codice.
# auto = rileva docker poi podman; off = disattivata anche se il runtime c'è.
SANDBOX_ENGINE = (os.environ.get("BLOCKINGBEAR_SANDBOX_ENGINE") or "auto").strip().lower()
SANDBOX_IMAGE = (os.environ.get("BLOCKINGBEAR_SANDBOX_IMAGE") or "blockingbear-sandbox:1").strip()
# container vergini pre-avviati (warm pool): ognuno fermo pesa ~50-100 MB RAM
SANDBOX_POOL = max(0, int(os.environ.get("BLOCKINGBEAR_SANDBOX_POOL") or 2))
# container ASSEGNATI a conversazioni, in contemporanea (oltre: eviction LRU)
SANDBOX_MAX = max(1, int(os.environ.get("BLOCKINGBEAR_SANDBOX_MAX") or 4))
# minuti dall'ULTIMA esecuzione prima che il reaper spenga un container
# assegnato (i container in pool non scadono, solo riciclo periodico)
SANDBOX_IDLE_MIN = max(1, int(os.environ.get("BLOCKINGBEAR_SANDBOX_IDLE_MIN") or 60))
SANDBOX_MEM = (os.environ.get("BLOCKINGBEAR_SANDBOX_MEM") or "2g").strip()
# secondi per singola esecuzione di codice (il kernel interrompe e sopravvive)
SANDBOX_EXEC_TIMEOUT = max(5, int(os.environ.get("BLOCKINGBEAR_SANDBOX_EXEC_TIMEOUT") or 120))
# iterazioni massime del loop agentico per messaggio utente (usato da chat.py)
SANDBOX_MAX_ITER = max(1, int(os.environ.get("BLOCKINGBEAR_SANDBOX_MAX_ITER") or 12))

# --- Browser camofox per la ricerca web -------------------------------------
# Tutte OPZIONALI, stessa filosofia della sandbox: la ricerca web è una
# feature con degradazione elegante — senza runtime container (e senza URL
# esterno) i tool web semplicemente non vengono dichiarati al modello.
# URL di un'istanza camofox-browser già in esecuzione: se impostato, il
# backend NON gestisce il ciclo di vita del container (per esempio un camofox
# condiviso gestito esternamente). Formato: http://host:9377
CAMOFOX_URL = (os.environ.get("BLOCKINGBEAR_CAMOFOX_URL") or "").strip() or None
# Chiave API dell'istanza esterna (solo con BLOCKINGBEAR_CAMOFOX_URL; per il
# container gestito la chiave si genera al volo a ogni avvio).
CAMOFOX_API_KEY = (os.environ.get("BLOCKINGBEAR_CAMOFOX_API_KEY") or "").strip() or None
CAMOFOX_IMAGE = (os.environ.get("BLOCKINGBEAR_CAMOFOX_IMAGE")
                 or "ghcr.io/redf0x1/camofox-browser").strip()
# porta HOST (bind su 127.0.0.1) del container gestito
CAMOFOX_PORT = max(1, int(os.environ.get("BLOCKINGBEAR_CAMOFOX_PORT") or 9377))
CAMOFOX_MEM = (os.environ.get("BLOCKINGBEAR_CAMOFOX_MEM") or "2g").strip()
# tetto di caratteri di una pagina letta con read_page (oltre: troncata con
# nota, stesso stile del kernel sandbox). È l'unica leva nuova sui token.
WEB_PAGE_MAX_CHARS = max(1000, int(os.environ.get("BLOCKINGBEAR_WEB_PAGE_MAX_CHARS")
                                   or 25000))
# Motore di ricerca: template con {q} al posto della query (URL-encoded).
# Default DuckDuckGo versione HTML: privacy dichiarata, niente JS, estrazione
# robusta.
WEB_SEARCH_URL = ((os.environ.get("BLOCKINGBEAR_WEB_SEARCH_URL") or "").strip()
                  or "https://html.duckduckgo.com/html/?q={q}")

# Il segreto JWT vive su disco accanto al DB: sopravvive ai riavvii senza
# dover configurare niente (installazione locale in azienda).
_SECRET_FILE = DATA_DIR / "secret.key"


def jwt_secret():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not _SECRET_FILE.exists():
        _SECRET_FILE.write_text(secrets.token_hex(32), encoding="ascii")
    return _SECRET_FILE.read_text(encoding="ascii").strip()


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

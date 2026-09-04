"""Log diagnostici del server: un solo posto dove si decide dove finiscono.

Il codice del server NON stampa con print(): usa un logger ottenuto da qui.
La differenza che conta in esercizio è che i messaggi si possono spegnere o
dirottare senza toccare il codice — e su un prodotto che tratta documenti
riservati non è un dettaglio: le righe diagnostiche citano nomi di file
dell'utente, e in produzione si può scendere a WARNING.

Perché serve questo modulo e non basta logging.getLogger(): uvicorn configura
SOLO i propri logger ("uvicorn", "uvicorn.error", "uvicorn.access") e lascia
il root senza handler. Un logger applicativo senza handler propaga al root e
finisce nel "lastResort" di Python, che scarta tutto sotto WARNING: gli info
sparirebbero in silenzio. Qui l'handler lo attacchiamo noi, una volta sola.

Verbosità: BLOCKINGBEAR_LOG_LEVEL (DEBUG/INFO/WARNING/ERROR), default INFO.
È opzionale — a differenza delle variabili di app/config.py la sua assenza
non ferma l'avvio — e un valore scritto male vale INFO invece di rompere il
server per un log.

Uso:
    from .logging_setup import get_logger    # .. nei sottopacchetti
    log = get_logger("blockingbear.jobs")
    log.info("coda avviata")
    log.exception("turno interrotto")       # in un except: allega il traceback
"""

import logging
import os
import sys
import threading

ROOT_NAME = "blockingbear"                  # tutti i logger dell'app stanno sotto
DEFAULT_LEVEL = "INFO"
FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"

_lock = threading.Lock()
_configured = False


def _level():
    """Livello richiesto dall'ambiente. Un nome sconosciuto NON è un errore
    fatale: si torna al default, un log storto non deve impedire l'avvio."""
    raw = os.environ.get("BLOCKINGBEAR_LOG_LEVEL") or DEFAULT_LEVEL
    name = raw.strip().upper()
    return getattr(logging, name, None) if name in (
        "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else logging.INFO


def _configure():
    """Attacca l'handler al logger "blockingbear". Idempotente: chiamarla due
    volte non raddoppia le righe."""
    global _configured
    with _lock:
        if _configured:
            return
        log = logging.getLogger(ROOT_NAME)
        log.setLevel(_level())
        if not log.handlers:
            # stderr come uvicorn: i messaggi diagnostici restano separati
            # da uno stdout che qualcuno potrebbe voler redirigere
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter(FORMAT, DATE_FORMAT))
            log.addHandler(handler)
        # niente risalita al root: se un domani qualcuno chiama basicConfig()
        # (o lo fa una libreria) le righe non escono due volte
        log.propagate = False
        _configured = True


def get_logger(name):
    """Logger applicativo pronto all'uso. `name` va sotto "blockingbear."
    (es. "blockingbear.sandbox"), così il nome nella riga dice già il modulo."""
    _configure()
    if name != ROOT_NAME and not name.startswith(ROOT_NAME + "."):
        name = f"{ROOT_NAME}.{name}"
    return logging.getLogger(name)

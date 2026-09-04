"""Byte in attesa tra la sonda pre-upload e l'upload vero (progetti).

/api/projects/probe riceve già TUTTO il file per contare le immagini e capire
se un PDF ha un layer di testo: rispedirlo all'upload significherebbe
trasferire due volte lo stesso documento (su un PDF da 40 MB, il doppio
dell'attesa, la prima metà pure invisibile). Qui i byte già ricevuti restano
da parte con un BIGLIETTO: l'upload cita il biglietto invece di rimandare il
file.

I byte stanno su DISCO (data/tmp/probes), non in RAM: una sonda abbandonata —
popup OCR chiuso, tab chiusa — non deve tenere occupati 40 MB fino alla
scadenza. L'indice invece è in memoria, quindi la cartella si svuota
all'avvio: qualunque cosa ci si trovi è orfana di un processo morto.

Il biglietto è legato al suo proprietario e vale UNA volta sola: `take` lo
consuma. Chi lo cita dopo la scadenza (o dopo aver già caricato) riceve None
e il chiamante ripiega sull'invio del file.
"""

import threading
import time
import uuid
from pathlib import Path

from .config import DATA_DIR

PROBE_TTL = 30 * 60        # secondi di vita di un biglietto non consumato

_DIR = DATA_DIR / "tmp" / "probes"
_probes = {}               # probe_id -> {"owner_id","filename","mime","ts"}
_lock = threading.Lock()


def _path(probe_id):
    return _DIR / f"{probe_id}.bin"


def _reset_dir():
    """All'avvio la cartella si svuota: l'indice vive in RAM, quindi nessuno
    dei file rimasti da un processo precedente è più reclamabile."""
    _DIR.mkdir(parents=True, exist_ok=True)
    for p in _DIR.glob("*.bin"):
        try:
            p.unlink()
        except OSError:
            pass


def _sweep():
    """Scaduti fuori (chiamata a ogni put/take: nessun thread dedicato)."""
    now = time.time()
    for pid in [pid for pid, e in _probes.items() if now - e["ts"] > PROBE_TTL]:
        _probes.pop(pid, None)
        try:
            _path(pid).unlink()
        except OSError:
            pass


def put(owner_id, filename, mime, data):
    """Mette da parte i byte già ricevuti e torna il biglietto."""
    probe_id = uuid.uuid4().hex
    _DIR.mkdir(parents=True, exist_ok=True)
    _path(probe_id).write_bytes(data)
    with _lock:
        _sweep()
        _probes[probe_id] = {"owner_id": owner_id, "filename": filename,
                             "mime": mime, "ts": time.time()}
    return probe_id


def take(probe_id, owner_id):
    """Consuma il biglietto: {"data","filename","mime"} oppure None se è
    sconosciuto, scaduto, già usato o di un altro utente."""
    with _lock:
        _sweep()
        entry = _probes.get(probe_id)
        if entry is None or entry["owner_id"] != owner_id:
            return None
        _probes.pop(probe_id, None)
    path = _path(probe_id)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return {"data": data, "filename": entry["filename"], "mime": entry["mime"]}


def drop(probe_id, owner_id):
    """Butta via un biglietto mai usato (popup OCR annullato): i byte non
    devono restare su disco fino alla scadenza. True se c'era."""
    return take(probe_id, owner_id) is not None


def drop_owner(owner_id):
    """Tutti i biglietti di un utente in una volta (serve alla cancellazione
    dell'utente, app/purge.py): i byte in attesa non devono sopravvivere al
    proprietario aspettando la scadenza. Torna quanti ne ha buttati."""
    with _lock:
        pids = [pid for pid, e in _probes.items() if e["owner_id"] == owner_id]
        for pid in pids:
            _probes.pop(pid, None)
    for pid in pids:
        try:
            _path(pid).unlink()
        except OSError:
            pass
    return len(pids)


_reset_dir()

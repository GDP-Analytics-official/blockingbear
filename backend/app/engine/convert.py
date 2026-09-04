"""Conversione documenti -> PDF con LibreOffice headless.

Serve SOLO per l'anteprima a video dei formati non-PDF (docx, ...): il file che
l'utente scarica è l'originale redatto nel suo formato, non questa conversione.
Per questo un'imperfezione di resa è accettabile; un backend bloccato no —
quindi processo separato, timeout e un POOL di profili (soffice non sopporta
istanze concorrenti sullo stesso profilo, ma due istanze su due profili
convivono benissimo: è così che l'anteprima converte i suoi due lati in
parallelo, vedi chat_staging._pdf_pair).

Ogni profilo è PERSISTENTE (in data/): il bootstrap del profilo domina il
tempo di conversione (~20s contro ~4s a profilo caldo su macchine datate), e
ricrearlo a ogni chiamata rendeva lente upload e modifiche della mappa. Ogni
slot del pool è ESCLUSIVO (mai due soffice sullo stesso profilo), il .lock
stantio viene rimosso prima di ogni run e su errore il profilo si rigenera da
zero (retry una volta).
"""

import os
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from .. import settings_store
from ..config import DATA_DIR, SOFFICE_PATH

# BLOCKINGBEAR_SOFFICE_PATH (se impostata) vince sulla ricerca automatica
_SOFFICE_CANDIDATES = ([SOFFICE_PATH] if SOFFICE_PATH else []) + [
    "soffice",                                                   # nel PATH
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
]
# base del pool: lo slot 0 usa _PROFILE_DIR così com'è, gli altri aggiungono
# un suffisso numerico (lo_profile2, ...). I test che isolano il profilo
# riassegnando _PROFILE_DIR isolano automaticamente tutto il pool: i percorsi
# si derivano a ogni conversione, non all'import.
_PROFILE_DIR = DATA_DIR / "lo_profile"
_SLOTS = 2                       # soffice concorrenti (un profilo ciascuno)
_free = list(range(_SLOTS))      # slot liberi; accesso solo sotto _cond
_cond = threading.Condition()


def _profile(slot):
    return _PROFILE_DIR if slot == 0 else \
        _PROFILE_DIR.with_name(f"{_PROFILE_DIR.name}{slot + 1}")


@contextmanager
def _slot():
    """Un profilo in USO ESCLUSIVO, o attesa che se ne liberi uno. Si prende
    sempre lo slot LIBERO più basso: le conversioni una-alla-volta restano
    tutte sul profilo base, che resta caldo, e i profili successivi si
    scaldano solo quando c'è vera concorrenza."""
    with _cond:
        while not _free:
            _cond.wait()
        n = min(_free)
        _free.remove(n)
    try:
        yield _profile(n)
    finally:
        with _cond:
            _free.append(n)
            _cond.notify()


class ConvertError(RuntimeError):
    """LibreOffice assente o conversione fallita."""


def soffice_path():
    """Percorso di soffice, o None se LibreOffice non è installato."""
    for c in _SOFFICE_CANDIDATES:
        p = shutil.which(c) or (c if os.path.isfile(c) else None)
        if p:
            return p
    return None


def available():
    return soffice_path() is not None


def _convert(data, suffix, target, timeout=None):
    """Bytes di un documento -> bytes convertiti nel formato `target`.

    Un soffice per SLOT del pool, ognuno sul suo profilo PERSISTENTE: il
    bootstrap del profilo è di gran lunga la parte più costosa, riusarlo
    cambia l'ordine di grandezza. Su errore il profilo viene rigenerato e si
    riprova una volta.
    """
    # il timeout è un parametro del pannello admin: letto a ogni conversione
    timeout = timeout or settings_store.current("lo_timeout_s")
    exe = soffice_path()
    if exe is None:
        raise ConvertError(
            "LibreOffice non trovato: serve per i documenti Word. "
            "Installalo (winget install TheDocumentFoundation.LibreOffice) e riavvia il server.")
    with _slot() as profile, \
            tempfile.TemporaryDirectory(prefix="blockingbear_lo_") as tmp:
        tmp = Path(tmp)
        src = tmp / f"documento{suffix}"
        src.write_bytes(data)
        out = src.with_suffix(f".{target}")
        last_err = ""
        for attempt in (1, 2):
            # lo slot è esclusivo: un .lock presente in QUESTO profilo è per
            # forza stantio (istanza precedente crashata) e bloccherebbe il
            # run headless
            (profile / ".lock").unlink(missing_ok=True)
            try:
                proc = subprocess.run(
                    [exe, "--headless", "--norestore",
                     f"-env:UserInstallation={profile.as_uri()}",
                     "--convert-to", target, "--outdir", str(tmp), str(src)],
                    capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise ConvertError(f"Conversione in {target} scaduta (timeout LibreOffice).")
            if proc.returncode == 0 and out.exists():
                return out.read_bytes()
            last_err = (proc.stderr or proc.stdout or b"").decode(errors="replace")[-400:]
            if attempt == 1:
                # profilo forse corrotto: si butta e si riprova da zero
                shutil.rmtree(profile, ignore_errors=True)
        raise ConvertError(f"Conversione in {target} fallita: {last_err or 'errore sconosciuto'}")


def to_pdf(data, suffix=".docx", timeout=None):
    """Anteprima a video dei formati non-PDF."""
    return _convert(data, suffix, "pdf", timeout)


def to_docx(data, suffix=".doc", timeout=None):
    """Vecchi .doc binari (e .odt di Writer) -> .docx, per farli passare dalla
    pipeline docx. Il binario Word 97-2003 non è riscrivibile in modo
    affidabile e la pipeline è OOXML: si converte all'ingresso e l'utente
    riceve un .docx anonimizzato."""
    return _convert(data, suffix, "docx", timeout)


def to_pptx(data, suffix=".ppt", timeout=None):
    """Vecchi .ppt binari (e .odp di Impress) -> .pptx, stessa storia del .doc."""
    return _convert(data, suffix, "pptx", timeout)


def to_xlsx(data, suffix=".xls", timeout=None):
    """Vecchi .xls binari (e .ods di Calc) -> .xlsx; usato anche per gli .xlsm
    (le macro VBA non sopravvivono: possono contenere PII e riscriverle non è
    affidabile)."""
    return _convert(data, suffix, "xlsx", timeout)

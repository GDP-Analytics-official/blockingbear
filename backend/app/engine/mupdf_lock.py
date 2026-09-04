"""
Serializzazione globale di MuPDF (PyMuPDF/fitz): la libreria NON è thread-safe.

Due thread dentro MuPDF nello stesso momento (es. due GET di pagine PNG nel
threadpool di FastAPI, o un render di preview mentre un worker redige) portano
il processo a un crash NATIVO senza traceback (visto dal vivo: exception
0xc00000ff con due thread in fz_new_display_list_from_page, uno nella callback
d'errore — i PDF malformati che spammano "MuPDF error" lo rendono quasi
deterministico).

Regola: OGNI funzione che apre/usa un documento fitz si decora con
@mupdf_serialized. Il lock è rientrante (le primitive si annidano:
redact_pdf -> _verify_residuals) e va tenuto SOLO per il lavoro MuPDF —
mai attorno ad analisi ML, OCR o conversioni LibreOffice, che possono durare
minuti e devono restare parallele.
"""

import functools
import threading

MUPDF_LOCK = threading.RLock()


def mupdf_serialized(fn):
    """Un solo thread alla volta dentro la funzione (e quindi dentro MuPDF)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with MUPDF_LOCK:
            return fn(*args, **kwargs)
    return wrapper

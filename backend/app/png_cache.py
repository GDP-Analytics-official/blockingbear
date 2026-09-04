"""Cache in RAM dei PNG di pagina renderizzati (anteprime di file di progetto
e dell'anteprima pre-invio della chat). LRU unica per tutte le fonti: la
chiave è (key, source, n) dove `key` è scelta dal chiamante in modo da non
collidere tra contesti (vedi _png_key in project_files e chat_staging)."""

import threading
from collections import OrderedDict

MAX_PNG_CACHE = 200   # pagine renderizzate tenute in cache (tutte le fonti)

_png_cache = OrderedDict()          # (key, source, n) -> {"png","width","height"}
_lock = threading.Lock()


def drop_pngs(key):
    """Invalida le pagine renderizzate di `key` (dopo una ri-redazione)."""
    with _lock:
        for k in [k for k in _png_cache if k[0] == key]:
            _png_cache.pop(k, None)


def cache_png(key, source, n, entry):
    with _lock:
        _png_cache[(key, source, n)] = entry
        _png_cache.move_to_end((key, source, n))
        while len(_png_cache) > MAX_PNG_CACHE:
            _png_cache.popitem(last=False)


def get_png(key, source, n):
    with _lock:
        e = _png_cache.get((key, source, n))
        if e is not None:
            _png_cache.move_to_end((key, source, n))
        return e

"""Catalogo modelli OpenRouter: fetch, cache in RAM, capability per la UI.

Regola guida (la stessa dei tag PII, [[niente-traduzioni-a-mano]]): NIENTE
liste hardcoded — tutto derivato dai metadati dell'API, il catalogo cambia
ogni settimana. Con la chiave configurata si usa /models/user (già filtrato
dalle impostazioni privacy dell'account: evita di mostrare modelli che poi
verrebbero rifiutati con 503); senza chiave, /models pubblico. Solo il
pubblico porta `benchmarks` (i punteggi indipendenti su cui si basa la
regola guidata del modello predefinito): con la chiave si fanno entrambe le
chiamate (~70 ms la seconda) e si agganciano per id.

La cache è in RAM con TTL: ~650 KB di JSON per 400 modelli non si richiedono
a ogni apertura della pagina, e un riavvio ricarica al primo uso.
"""

import re
import threading
import time

from . import client as or_client

_TTL = 3600                     # 1h; refresh forzabile con ?refresh=1 (admin)
_DESCRIPTION_LIMIT = 400        # in lista basta l'attacco della descrizione

_lock = threading.Lock()
_cache = {"models": None, "at": 0.0, "source": None}

# varianti dello stesso modello (":free", ":nitro"...): raggruppate sotto il
# modello base nel selettore, non 400 voci piatte
_VARIANT_RE = re.compile(r"^(?P<base>[^:]+):(?P<variant>[a-z0-9-]+)$")


_BENCHMARK_KEYS = (("intelligence_index", "intelligence"),
                   ("coding_index", "coding"), ("agentic_index", "agentic"))


def _benchmarks(entry):
    """I tre indici (0-100) di Artificial Analysis che l'API pubblica espone
    sotto `benchmarks`, o None se il modello non è stato valutato. Sono la
    base numerica della regola guidata (model_rules): «quanto è bravo» a
    ragionare, a programmare, a usare strumenti. Il resto del blocco
    (design_arena: classifiche di sviluppo UI) non serve qui."""
    aa = (entry.get("benchmarks") or {}).get("artificial_analysis") or {}
    out = {}
    for key, name in _BENCHMARK_KEYS:
        try:
            out[name] = None if aa.get(key) is None else float(aa[key])
        except (TypeError, ValueError):
            out[name] = None
    return out if any(v is not None for v in out.values()) else None


def _normalize(entry):
    """Riduce un elemento di /models ai campi che pilotano la UI."""
    mid = entry.get("id") or ""
    m = _VARIANT_RE.match(mid)
    arch = entry.get("architecture") or {}
    pricing = entry.get("pricing") or {}
    supported = entry.get("supported_parameters") or []
    desc = (entry.get("description") or "").strip()
    if len(desc) > _DESCRIPTION_LIMIT:
        desc = desc[:_DESCRIPTION_LIMIT].rsplit(" ", 1)[0] + "…"
    return {
        "id": mid,
        "variant_of": m.group("base") if m else None,
        "name": entry.get("name") or mid,
        "description": desc,
        # data di pubblicazione (unix): serve alla regola "ultimo modello di
        # <azienda>" del modello predefinito (openrouter/model_rules.py)
        "created": entry.get("created"),
        "context_length": entry.get("context_length"),
        "max_completion_tokens": (entry.get("top_provider") or {})
                                 .get("max_completion_tokens"),
        "pricing": {k: pricing.get(k) for k in
                    ("prompt", "completion", "web_search", "image")
                    if pricing.get(k) is not None},
        "input_modalities": arch.get("input_modalities") or ["text"],
        "output_modalities": arch.get("output_modalities") or ["text"],
        # il selettore deve privilegiare i modelli con "tools": senza, il
        # code interpreter non funziona, e sono una minoranza del catalogo
        "tools": "tools" in supported,
        "supported_parameters": supported,
        # blocco reasoning così com'è: la UI deriva switch/selettore effort
        # da mandatory/supported_efforts/default_*
        "reasoning": entry.get("reasoning"),
        "moderated": bool((entry.get("top_provider") or {}).get("is_moderated")),
        "expiration_date": entry.get("expiration_date"),
        "benchmarks": _benchmarks(entry),
    }


async def _merge_benchmarks(models, base_url=None):
    """/models/user non porta `benchmarks`: si prendono dal catalogo pubblico
    e si agganciano per id (una variante di routing eredita quelli della
    base, che è lo stesso modello). Se il pubblico non risponde i modelli
    restano senza valutazione: la regola guidata non si risolve — «nessun
    default», come per ogni regola che non si risolve — ma il catalogo c'è."""
    try:
        data = await or_client.get_json("/models", base_url=base_url)
    except or_client.OpenRouterError:
        return
    by_id = {e.get("id"): _benchmarks(e) for e in data.get("data") or []}
    for m in models:
        if m["benchmarks"] is None:
            m["benchmarks"] = (by_id.get(m["id"])
                               or by_id.get(m.get("variant_of") or ""))


async def _zdr_providers(base_url=None, api_key=None):
    """{model_id: [provider, ...]} degli endpoint con Zero Data Retention.

    NESSUN campo di /models dice se un modello ha provider ZDR: senza questa
    lista l'unico modo di scoprirlo è vedersi rifiutare la richiesta con un
    503. La fonte è GET /endpoints/zdr, che la documentazione dichiara
    esplicitamente come elenco programmatico, aggiornato da OpenRouter quando
    un provider cambia politica (guides/features/zdr).

    Ritorna None se la lista non è ottenibile (o arriva vuota, che a questo
    punto significa "l'API è cambiata", non "niente è conforme"): meglio "non
    lo so" — e nessun avviso — che un avviso sbagliato in un verso o nell'altro.
    """
    try:
        data = await or_client.get_json("/endpoints/zdr", api_key=api_key,
                                        base_url=base_url)
    except or_client.OpenRouterError:
        return None
    out = {}
    for e in data.get("data") or ():
        mid, name = e.get("model_id"), e.get("provider_name")
        if mid and name:
            out.setdefault(mid, set()).add(name)
    return {k: sorted(v) for k, v in out.items()} or None


def _zdr_for(zdr, model_id):
    """I provider ZDR di un modello. Le varianti di routing (:free, :nitro,
    :batch...) girano sugli stessi endpoint del modello base: se l'id esatto
    non è nell'elenco si guarda la base."""
    if model_id in zdr:
        return zdr[model_id]
    m = _VARIANT_RE.match(model_id)
    return zdr.get(m.group("base"), []) if m else []


async def get_models(force=False, base_url=None, api_key=None):
    """Il catalogo normalizzato (lista) + la fonte usata. Cache a TTL.

    `api_key` è la chiave di inferenza di CHI chiede (chiavi per-utente):
    /models/user riflette le impostazioni privacy dell'ACCOUNT, uguali per
    tutte le chiavi, quindi la cache resta una sola. Senza chiave (utente
    ancora senza la sua) si ripiega sul catalogo pubblico."""
    with _lock:
        fresh = (_cache["models"] is not None
                 and time.monotonic() - _cache["at"] < _TTL)
        if fresh and not force:
            return {"models": _cache["models"], "source": _cache["source"]}

    source = "user" if api_key else "public"
    path = "/models/user" if api_key else "/models"
    try:
        data = await or_client.get_json(path, api_key=api_key,
                                        base_url=base_url)
    except or_client.OpenRouterError:
        if not api_key:
            raise
        # una chiave revocata non deve spegnere il selettore: fallback
        data = await or_client.get_json("/models", base_url=base_url)
        source = "public"

    models = [_normalize(e) for e in data.get("data") or []]
    # se un giorno /models/user portasse i benchmark, la seconda chiamata
    # diventa inutile da sola
    if source == "user" and not any(m["benchmarks"] for m in models):
        await _merge_benchmarks(models, base_url=base_url)
    # zdr_providers: [] = nessun provider conforme (la richiesta verrebbe
    # rifiutata con le regole privacy attive), None = non verificabile
    zdr = await _zdr_providers(base_url=base_url, api_key=api_key)
    for m in models:
        m["zdr_providers"] = None if zdr is None else _zdr_for(zdr, m["id"])
    with _lock:
        _cache.update(models=models, at=time.monotonic(), source=source)
    return {"models": models, "source": source}


async def key_info(api_key, base_url=None):
    """GET /key: valida la chiave e riporta i crediti. Solleva
    OpenRouterError se la chiave non è valida."""
    data = await or_client.get_json("/key", api_key=api_key,
                                    base_url=base_url)
    info = data.get("data") or {}
    return {
        "label": info.get("label"),
        "limit": info.get("limit"),
        "limit_remaining": info.get("limit_remaining"),
        "usage": info.get("usage"),
        "usage_monthly": info.get("usage_monthly"),
        "is_free_tier": info.get("is_free_tier"),
        # OpenRouter may accept management credentials on /key even though
        # they cannot perform inference. Keep both current and historical
        # flags so a management key can be told apart from an inference key.
        "is_management_key": bool(info.get("is_management_key")),
        "is_provisioning_key": bool(info.get("is_provisioning_key")),
    }


def invalidate():
    """Dopo il cambio della chiave: /models/user dipende dall'account."""
    with _lock:
        _cache.update(models=None, at=0.0, source=None)


def find(models, model_id):
    """La voce del catalogo di un modello, o None (alias/modello ritirato)."""
    return next((m for m in models if m["id"] == model_id), None)


# --- Opzioni del turno ------------------------------------------------------
# I controlli della UI (ModelOptions.jsx) nascono dai metadati del modello e
# qui si ri-validano contro gli STESSI metadati: il browser propone, il server
# decide. Intervalli dalla documentazione OpenRouter (§Parameters).
_RANGES = {
    "temperature": (0.0, 2.0), "top_p": (0.0, 1.0), "min_p": (0.0, 1.0),
    "top_a": (0.0, 1.0), "frequency_penalty": (-2.0, 2.0),
    "presence_penalty": (-2.0, 2.0), "repetition_penalty": (0.1, 2.0),
    "top_k": (0, 1000), "max_tokens": (1, None), "seed": (0, 2 ** 31 - 1),
}
_INTEGERS = {"top_k", "max_tokens", "seed"}


def _clamp(key, value, cap=None):
    """Il valore nel suo intervallo, o None se non è un numero."""
    lo, hi = _RANGES[key]
    try:
        v = int(value) if key in _INTEGERS else float(value)
    except (TypeError, ValueError):
        return None
    if cap is not None:
        hi = cap if hi is None else min(hi, cap)
    if hi is not None:
        v = min(v, int(hi) if key in _INTEGERS else hi)
    return max(v, lo)


def sanitize_options(options, entry):
    """(reasoning, params) da mandare a OpenRouter, filtrati sui metadati.

    `options` è il JSON salvato sulla conversazione:
        {"reasoning": {"enabled", "effort", "max_tokens", "exclude"},
         "params":    {"temperature", "top_p", "max_tokens", ...}}
    `entry` è la voce di catalogo del modello (None = modello sconosciuto:
    non si può validare niente, si passa il minimo indispensabile).

    Un parametro non supportato viene TOLTO invece di essere inviato e
    ignorato: così quello che il pannello mostra e quello che il modello
    riceve restano la stessa cosa."""
    options = options or {}
    supported = set((entry or {}).get("supported_parameters") or ())
    meta = (entry or {}).get("reasoning") or {}
    raw = options.get("reasoning") or {}

    reasoning = None
    knows_reasoning = (entry is None or bool(meta)
                       or "reasoning" in supported)
    if knows_reasoning:
        mandatory = bool(meta.get("mandatory"))
        enabled = raw.get("enabled")
        if enabled is None:
            enabled = mandatory or bool(meta.get("default_enabled"))
        if enabled or mandatory:
            reasoning = {"enabled": True}
            efforts = meta.get("supported_efforts") or ()
            effort = raw.get("effort")
            if effort and (effort in efforts or (entry is None and not efforts)):
                reasoning["effort"] = effort
            elif raw.get("max_tokens") and (meta.get("supports_max_tokens")
                                            or entry is None):
                v = _clamp("max_tokens", raw["max_tokens"])
                if v:
                    reasoning["max_tokens"] = v
            if raw.get("exclude"):
                # il ragionamento resta attivo ma non torna nella risposta
                reasoning["exclude"] = True
        elif meta.get("default_enabled"):
            # spegnerlo va CHIESTO: senza il flag il modello ragionerebbe
            reasoning = {"enabled": False}

    cap = ((entry or {}).get("max_completion_tokens")
           or (entry or {}).get("context_length"))
    params = {}
    for key, value in (options.get("params") or {}).items():
        if key not in _RANGES:
            continue
        if entry is not None and key not in supported:
            continue
        v = _clamp(key, value, cap if key == "max_tokens" else None)
        if v is not None:
            params[key] = v
    return reasoning, params

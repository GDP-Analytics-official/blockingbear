"""Client HTTP verso OpenRouter: management key, richieste, errori.

L'unico segreto OpenRouter che l'installazione possiede in proprio è la
MANAGEMENT KEY: si imposta dall'interfaccia (wizard del primo avvio, poi la
pagina API keys dell'admin) e vive in data/openrouter_management.key, stesso
pattern di secret.key: sopravvive ai riavvii, non entra nel DB né nei backup
del database, non viene MAI restituita al browser (solo la versione
mascherata). Non fa inferenza: serve a creare la chiave di inferenza
personale di ogni utente (openrouter/provisioning.py) e a leggere le
analytics.

Lo streaming usa il protocollo SSE di OpenRouter con le sue particolarità
documentate dal provider (snapshot 2026-08):
  - righe di commento ": OPENROUTER PROCESSING" (keep-alive) da saltare PRIMA
    del json.loads, o il parser esplode;
  - sentinella "data: [DONE]" a fine stream;
  - errori PRIMA del primo token = risposta JSON normale con status != 200;
    errori A METÀ stream = HTTP 200 + chunk con campo top-level "error" e
    finish_reason "error" (gestiti dal chiamante, qui si consegnano e basta).
"""

import json
import os

import httpx

from ..config import DATA_DIR

BASE_URL = (os.environ.get("BLOCKINGBEAR_OPENROUTER_BASE_URL")
            or "https://openrouter.ai/api/v1").rstrip("/")
# Attribuzione dell'app nelle classifiche e nelle analytics OpenRouter.
_HEADERS_EXTRA = {
    "HTTP-Referer": "https://blockingbear.com",
    "X-OpenRouter-Title": "BlockingBear",
}

_MGMT_KEY_FILE = DATA_DIR / "openrouter_management.key"


class OpenRouterError(RuntimeError):
    """Errore restituito da OpenRouter prima dell'inizio dello streaming."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


# --- Management key (storage; gli endpoint arrivano con le route) ------------

def get_management_key():
    """La management key salvata, o None se l'installazione non l'ha ancora."""
    try:
        key = _MGMT_KEY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return key or None


def set_management_key(key):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _MGMT_KEY_FILE.write_text(key.strip(), encoding="utf-8")
    try:
        _MGMT_KEY_FILE.chmod(0o600)
    except OSError:
        pass                    # filesystem senza permessi POSIX (bind Windows)


def delete_management_key():
    _MGMT_KEY_FILE.unlink(missing_ok=True)


def mask_key(key):
    """Versione mostrabile in UI di una chiave: mai quella intera verso il
    browser."""
    if not key:
        return None
    return key[:11] + "…" + key[-4:] if len(key) > 18 else "…" + key[-4:]


# --- Streaming -----------------------------------------------------------------

def _error_from_response(status, body_bytes):
    try:
        err = json.loads(body_bytes)["error"]
        return OpenRouterError(err.get("message") or "Errore OpenRouter",
                               status=status, code=err.get("code"))
    except (ValueError, KeyError, TypeError):
        text = body_bytes.decode("utf-8", "replace")[:300]
        return OpenRouterError(f"Errore OpenRouter (HTTP {status}): {text}",
                               status=status)


async def request_json(method, path, api_key=None, json_body=None,
                       base_url=None, timeout=30.0):
    """Richiesta JSON verso OpenRouter (qualsiasi verbo): catalogo, crediti,
    provisioning delle chiavi, analytics. Ritorna il JSON della risposta;
    solleva OpenRouterError su status non 2xx."""
    url = (base_url or BASE_URL) + path
    headers = dict(_HEADERS_EXTRA)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.request(method, url, headers=headers,
                                        json=json_body)
        except httpx.HTTPError as e:
            raise OpenRouterError(f"OpenRouter non raggiungibile: {e}")
    if resp.status_code // 100 != 2:
        raise _error_from_response(resp.status_code, resp.content)
    try:
        return resp.json()
    except ValueError:
        return {}                   # 204 o corpo vuoto (DELETE)


async def get_json(path, api_key=None, base_url=None, timeout=30.0):
    """GET verso OpenRouter (catalogo modelli, crediti, validazione chiave).
    Ritorna il JSON; solleva OpenRouterError su status != 200."""
    return await request_json("GET", path, api_key=api_key,
                              base_url=base_url, timeout=timeout)


def make_client(read_timeout=180.0):
    """Un AsyncClient da riusare per tutte le richieste di un turno (il loop
    agentico ne fa fino a MAX_ITER+1: tenere viva la connessione TLS conta)."""
    return httpx.AsyncClient(timeout=httpx.Timeout(
        connect=15.0, read=read_timeout, write=30.0, pool=15.0))


async def stream_chat(payload, api_key, client, base_url=None):
    """POST /chat/completions con stream=true: genera i chunk già parsati
    (dict). Solleva OpenRouterError sugli errori pre-stream; i chunk d'errore
    a metà stream passano al chiamante come arrivano. Chiudere il generatore
    (aclose) interrompe la richiesta: sui provider che lo supportano ferma
    anche la generazione e la fatturazione."""
    url = (base_url or BASE_URL) + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", **_HEADERS_EXTRA}
    async with client.stream("POST", url, json=payload,
                             headers=headers) as resp:
        if resp.status_code != 200:
            raise _error_from_response(resp.status_code, await resp.aread())
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue            # keep-alive ": OPENROUTER PROCESSING"
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except ValueError:
                continue            # frammento non JSON: si ignora per spec

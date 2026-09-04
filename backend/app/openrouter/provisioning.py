"""Chiavi OpenRouter per-utente, via Management API (snapshot docs 2026-08).

Il modello è "una chiave di inferenza per ogni utente dell'app", nominata
blockingbear-<username>: così la spesa di ciascuno è separata alla fonte
(le analytics di OpenRouter espongono la dimensione api_key_id, che è il
NOME della chiave) e ogni utente può avere il suo tetto di spesa con reset
periodico. Le chiavi si creano/gestiscono con la MANAGEMENT KEY
(or_client.get_management_key): è l'unica chiave che l'amministratore
genera a mano (una volta, da openrouter.ai → Settings → Management Keys) e
incolla nel wizard del primo avvio; tutte le chiavi di inferenza — admin
incluso — nascono da qui. Nessuna chiave condivisa: un utente senza chiave
personale non può parlare con OpenRouter.

ATTENZIONE al vincolo centrale dell'API: il testo in chiaro di una chiave
viaggia SOLO nella risposta della POST di creazione. Va salvato subito sul
record utente (users.openrouter_key); se si perde, l'unica strada è revocare
e ricreare (rotate_key).

Il `limit` di una chiave è un tetto di CONSUMO sul saldo crediti condiviso
dell'account, non un'allocazione: la somma dei limiti può superare il saldo,
per questo la dashboard mostra anche i crediti globali (credits())."""

from ..logging_setup import get_logger
from . import client as or_client

log = get_logger("blockingbear.provisioning")

KEY_PREFIX = "blockingbear-"
LIMIT_RESETS = ("daily", "weekly", "monthly")

_PAGE = 100                 # dimensione pagina documentata di GET /keys


def key_name(username):
    return KEY_PREFIX + username


def configured():
    return or_client.get_management_key() is not None


def _mkey():
    key = or_client.get_management_key()
    if key is None:
        raise or_client.OpenRouterError(
            "Management key OpenRouter non configurata: impostarla dalla "
            "pagina API keys.")
    return key


async def verify_management_key(key):
    """Chiede a OpenRouter cos'è `key` e accetta SOLO una management key.
    Solleva OpenRouterError se OpenRouter la rifiuta o se è una normale
    chiave di inferenza (che non potrebbe creare le chiavi degli utenti)."""
    data = await or_client.get_json("/key", api_key=key)
    info = data.get("data") or {}
    if not (info.get("is_management_key") or info.get("is_provisioning_key")):
        raise or_client.OpenRouterError(
            "Questa è una normale chiave di inferenza, non una Management "
            "API Key.", status=422, code="key_not_management")
    return {"label": info.get("label")}


# --- CRUD chiavi (Bearer = management key) -----------------------------------

async def create_key(name, limit=None, limit_reset=None):
    """Crea una chiave di inferenza. Ritorna (chiave_in_chiaro, metadati).
    limit in USD (None = senza tetto); limit_reset in LIMIT_RESETS o None."""
    body = {"name": name}
    if limit is not None:
        body["limit"] = float(limit)
    if limit_reset:
        if limit_reset not in LIMIT_RESETS:
            raise ValueError(f"limit_reset non valido: {limit_reset!r}")
        body["limit_reset"] = limit_reset
    data = await or_client.request_json("POST", "/keys", api_key=_mkey(),
                                        json_body=body)
    # la chiave in chiaro esiste SOLO qui: il chiamante la salva sull'utente
    return data.get("key"), (data.get("data") or {})


async def get_key(key_hash):
    data = await or_client.request_json("GET", f"/keys/{key_hash}",
                                        api_key=_mkey())
    return data.get("data") or {}


async def update_key(key_hash, **fields):
    """PATCH dei soli campi passati: name, disabled, limit, limit_reset,
    include_byok_in_limit. limit=None RIMUOVE il tetto (il campo viaggia)."""
    data = await or_client.request_json("PATCH", f"/keys/{key_hash}",
                                        api_key=_mkey(), json_body=fields)
    return data.get("data") or {}


async def delete_key(key_hash):
    await or_client.request_json("DELETE", f"/keys/{key_hash}",
                                 api_key=_mkey())


async def list_keys(include_disabled=True):
    """Tutte le chiavi dell'account (paginazione a offset)."""
    out, offset = [], 0
    while True:
        path = f"/keys?offset={offset}"
        if include_disabled:
            path += "&include_disabled=true"
        data = await or_client.request_json("GET", path, api_key=_mkey())
        page = data.get("data") or []
        out.extend(page)
        if len(page) < _PAGE:
            return out
        offset += len(page)


# --- Saldo e analytics (Bearer = management key) ------------------------------

async def credits():
    """Saldo GLOBALE dell'account: {'total_credits','total_usage'}. È il
    complemento necessario ai limiti per-chiave, che non riservano crediti."""
    data = await or_client.request_json("GET", "/credits", api_key=_mkey())
    return data.get("data") or {}


async def analytics_query(payload):
    """POST /analytics/query così com'è (la validazione dei campi sta nella
    route admin che la espone). Ritorna {'data': [...], 'metadata': {...}}."""
    data = await or_client.request_json("POST", "/analytics/query",
                                        api_key=_mkey(), json_body=payload,
                                        timeout=60.0)
    return data.get("data") or {}


async def analytics_meta():
    """Metriche/dimensioni/granularità disponibili (per non hardcodare
    liste che OpenRouter può ampliare — stessa regola del catalogo)."""
    data = await or_client.request_json("GET", "/analytics/meta",
                                        api_key=_mkey())
    return data.get("data") or data


# --- Lifecycle sulla tabella users --------------------------------------------

def key_for(user):
    """La chiave di inferenza con cui QUESTO utente parla a OpenRouter: la
    sua personale, o None se non è ancora stata creata (OpenRouter giù al
    momento della creazione: si riprova al primo messaggio)."""
    return user.openrouter_key


async def ensure_user_key(session, user, limit=None, limit_reset=None):
    """Garantisce che l'utente abbia la sua chiave; la crea se manca.
    Ritorna True se l'ha creata ora. Solleva OpenRouterError se OpenRouter
    rifiuta o non è raggiungibile (il chiamante decide se è fatale)."""
    if user.openrouter_key:
        return False
    key, data = await create_key(key_name(user.username),
                                 limit=limit, limit_reset=limit_reset)
    if not key:
        raise or_client.OpenRouterError(
            "OpenRouter non ha restituito la chiave creata (risposta "
            "inattesa dell'API).")
    user.openrouter_key = key
    user.openrouter_key_hash = data.get("hash")
    session.commit()
    log.info("Creata chiave OpenRouter %r per l'utente %s",
             data.get("name"), user.username)
    return True


async def rotate_user_key(session, user, limit=None, limit_reset=None):
    """Revoca (se esiste) e ricrea la chiave dell'utente: è il rimedio a
    una chiave compromessa o a un record rimasto senza testo in chiaro."""
    if user.openrouter_key_hash:
        try:
            await delete_key(user.openrouter_key_hash)
        except or_client.OpenRouterError as e:
            # già cancellata dalla UI di OpenRouter? Si prosegue: lo scopo
            # è avere una chiave nuova funzionante.
            log.warning("Revoca della vecchia chiave di %s fallita: %s",
                        user.username, e)
    user.openrouter_key = None
    user.openrouter_key_hash = None
    session.commit()
    await ensure_user_key(session, user, limit=limit, limit_reset=limit_reset)


async def drop_user_key(user):
    """Best-effort alla cancellazione dell'utente: la chiave non deve
    sopravvivere al suo proprietario. Ritorna l'eventuale errore (str)."""
    if not user.openrouter_key_hash:
        return None
    try:
        await delete_key(user.openrouter_key_hash)
        return None
    except or_client.OpenRouterError as e:
        log.warning("Chiave OpenRouter di %s non revocata: %s",
                    user.username, e)
        return str(e)


async def bootstrap(session_factory):
    """All'avvio: ogni utente senza chiave ne riceve una (utenti creati
    mentre OpenRouter non rispondeva). Best-effort: se OpenRouter è giù il server parte comunque e la chiave
    arriva al primo messaggio (ensure_user_key in send_message)."""
    if not configured():
        return
    from ..db import User          # import locale: niente ciclo db<->openrouter
    with session_factory() as session:
        users = session.query(User).filter(
            User.openrouter_key.is_(None)).all()
        for user in users:
            try:
                await ensure_user_key(session, user)
            except (or_client.OpenRouterError, ValueError) as e:
                log.warning("Bootstrap chiave per %s rimandato: %s",
                            user.username, e)

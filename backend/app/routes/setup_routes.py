"""Installazione guidata (wizard del primo avvio) e management key OpenRouter.

La configurazione è COMPLETA quando il wizard è arrivato in fondo (flag
`setup_completed` nella tabella settings, alzato da POST /api/setup/finish):
fino ad allora la pagina di accesso mostra il wizard e le route pubbliche qui
sotto accettano di impostare la management key, creare il primo admin e
salvare le scelte di anonimizzazione e chat. Il wizard NON autentica: alla
fine si passa dal normale login. A configurazione completa le route pubbliche
rispondono 409 e la management key si cambia solo da admin autenticato (PUT
/api/settings/management-key, pagina API keys).

Finché il wizard non è concluso, chiunque raggiunga la pagina di accesso può
completarlo e diventare amministratore: è documentato in SECURITY.md e il
rimedio è concludere il wizard subito dopo il primo avvio.

La management key non fa inferenza: serve a creare la chiave di inferenza
personale di ogni utente. Per questo il primo admin nasce SOLO se OpenRouter
accetta di creargli la chiave: un admin senza chiave non potrebbe usare la
chat, e l'errore va mostrato subito, nel wizard, non scoperto dopo.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import purge, settings_store
from ..auth import hash_password, require_admin
from ..db import Setting, User, get_session
from ..errors import ApiError
from ..logging_setup import get_logger
from ..openrouter import catalog, model_rules, provisioning
from ..openrouter import client as or_client

log = get_logger("blockingbear.setup")

router = APIRouter(tags=["setup"])

ADMIN_USERNAME = "admin"


class KeyIn(BaseModel):
    key: str


class AdminIn(BaseModel):
    password: str = Field(min_length=8)
    # lingua scelta allo step 0 del wizard: finisce sul profilo dell'admin,
    # così il popup della lingua non compare al primo ingresso
    lang: str | None = None
    # wizard ripreso con l'admin già creato: elimina quell'account (chiave
    # OpenRouter compresa) e lo ricrea con la nuova password
    replace: bool = False


class FinishIn(BaseModel):
    excluded_tags: list[str]
    custom_terms: list[dict]
    chat_anonymization_policy: str = "optional"
    chat_allow_non_zdr: bool = False
    # regola del modello predefinito dell'installazione (model_rules); None =
    # nessuno, lo sceglie ogni utente
    default_model_rule: dict | None = None
    # modelli visibili agli utenti non admin: {"enabled": bool, "models":
    # [id, ...]} (settings_store.model_access); None = tutti
    model_access: dict | None = None


class ResolveIn(BaseModel):
    rule: dict | None = None
    # la deroga ZDR scelta allo step precedente: non è ancora salvata, la
    # regola guidata deve tenerne conto già nell'anteprima
    chat_allow_non_zdr: bool = False


_COMPLETED_KEY = "setup_completed"


def setup_needed(session):
    return session.get(Setting, _COMPLETED_KEY) is None


def _admin_exists(session):
    return session.query(User).filter_by(username=ADMIN_USERNAME).first()


def _require_setup(session):
    if not setup_needed(session):
        raise ApiError(409, "setup_done",
                       "Configurazione già completata: accedi con le tue "
                       "credenziali.")


async def _validate_key(key):
    key = key.strip()
    if not key:
        raise ApiError(422, "key_empty", "Chiave vuota.")
    try:
        await provisioning.verify_management_key(key)
    except or_client.OpenRouterError as e:
        if e.code == "key_not_management":
            raise ApiError(422, "key_not_management", str(e))
        raise ApiError(422, "key_refused",
                       f"Chiave rifiutata da OpenRouter: {e}", error=str(e))
    return key


# --- Route pubbliche: valgono solo finché il wizard non è concluso -----------

@router.get("/api/setup/status")
def setup_status(session: Session = Depends(get_session)):
    """Se la configurazione va ancora completata e a che punto è: management
    key e admin possono esistere già da un tentativo interrotto (F5 a metà
    wizard). Pubblico: la pagina di accesso lo chiede prima del login."""
    return {"needed": setup_needed(session),
            "management_key": provisioning.configured(),
            "admin": _admin_exists(session) is not None}


@router.post("/api/setup/management-key")
async def setup_management_key(body: KeyIn,
                               session: Session = Depends(get_session)):
    """Step del wizard: valida con OpenRouter e salva la management key.
    Rifiuta una normale chiave di inferenza (422 `key_not_management`)."""
    _require_setup(session)
    key = await _validate_key(body.key)
    or_client.set_management_key(key)
    catalog.invalidate()
    return {"ok": True, "management_masked": or_client.mask_key(key)}


@router.post("/api/setup/admin", status_code=201)
async def setup_admin(body: AdminIn, session: Session = Depends(get_session)):
    """Step del wizard: crea l'utente `admin` con la password scelta e, con
    la management key, la sua chiave di inferenza. Se OpenRouter non crea la
    chiave l'admin NON viene creato (502 `openrouter_refused`) e il wizard
    resta su questo passo. Non autentica: il login si fa dopo, dalla pagina
    di accesso. Se l'admin esiste già (wizard ripreso): 409 `admin_exists`,
    oppure con `replace` lo si elimina e ricrea."""
    _require_setup(session)
    if not provisioning.configured():
        raise ApiError(409, "setup_key_missing",
                       "Prima imposta la management key OpenRouter.")
    existing = _admin_exists(session)
    if existing is not None:
        if not body.replace:
            raise ApiError(409, "admin_exists",
                           "L'amministratore esiste già: prosegui col passo "
                           "successivo.")
        # la chiave non deve sopravvivere all'account: best-effort, come la
        # cancellazione di un utente dal pannello
        await provisioning.drop_user_key(existing)
        purge.purge_user(session, existing)
        log.info("Amministratore ricreato dal wizard di configurazione")
    user = User(username=ADMIN_USERNAME,
                password_hash=hash_password(body.password), role="admin")
    if body.lang:
        try:
            settings_store.set_user_lang(session, user, body.lang)
        except ValueError:
            pass                    # lingua sconosciuta: si chiede dopo
    session.add(user)
    session.commit()
    try:
        await provisioning.ensure_user_key(session, user)
    except (or_client.OpenRouterError, ValueError) as e:
        session.delete(user)
        session.commit()
        raise ApiError(502, "openrouter_refused",
                       f"OpenRouter ha rifiutato l'operazione: {e}",
                       error=str(e))
    log.info("Creato l'amministratore %s dal wizard di configurazione",
             user.username)
    return {"username": user.username, "role": user.role,
            "lang": settings_store.user_lang(user)}


@router.get("/api/setup/models")
async def setup_models(session: Session = Depends(get_session)):
    """Il catalogo dei modelli per lo step del modello predefinito. Il wizard
    non è autenticato: si usa la chiave dell'admin appena creato, quindi
    risponde solo finché il wizard è aperto e l'admin esiste."""
    _require_setup(session)
    admin = _admin_exists(session)
    if admin is None:
        raise ApiError(409, "setup_incomplete",
                       "Prima completa management key e amministratore.")
    try:
        return await catalog.get_models(api_key=provisioning.key_for(admin))
    except or_client.OpenRouterError as e:
        raise ApiError(502, "catalog_unavailable",
                       f"Catalogo OpenRouter non disponibile: {e}",
                       error=str(e))


@router.post("/api/setup/resolve-model")
async def setup_resolve_model(body: ResolveIn,
                              session: Session = Depends(get_session)):
    """Anteprima della regola del modello predefinito durante il wizard: a
    quale modello porta ADESSO (stessa funzione di /api/settings/default-model/
    resolve, ma con la chiave dell'admin appena creato, il wizard non è
    autenticato)."""
    _require_setup(session)
    admin = _admin_exists(session)
    if admin is None:
        raise ApiError(409, "setup_incomplete",
                       "Prima completa management key e amministratore.")
    try:
        rule = model_rules.clean_rule(body.rule)
    except ValueError as e:
        raise ApiError(422, "invalid_rule", str(e))
    try:
        cat = await catalog.get_models(api_key=provisioning.key_for(admin))
    except or_client.OpenRouterError as e:
        raise ApiError(502, "catalog_unavailable",
                       f"Catalogo OpenRouter non disponibile: {e}",
                       error=str(e))
    return model_rules.explain(rule, cat["models"],
                               allow_non_zdr=body.chat_allow_non_zdr)


@router.post("/api/setup/finish")
def setup_finish(body: FinishIn, session: Session = Depends(get_session)):
    """Ultimo step del wizard: categorie escluse, termini globali, obbligo di
    anonimizzazione della chat e deroga ZDR, poi il flag di configurazione
    completata. Da qui in avanti il wizard non compare più e queste scelte si
    cambiano da Impostazioni. Richiede che management key e admin esistano."""
    _require_setup(session)
    if not provisioning.configured() or _admin_exists(session) is None:
        raise ApiError(409, "setup_incomplete",
                       "Prima completa management key e amministratore.")
    try:
        settings_store.set_anon_defaults(session, body.excluded_tags,
                                         body.custom_terms,
                                         body.chat_anonymization_policy)
        settings_store.set_values(
            session, {"chat_allow_non_zdr": int(body.chat_allow_non_zdr)})
        settings_store.set_default_model_rule(session, body.default_model_rule)
        access = body.model_access or {}
        if not isinstance(access, dict):
            raise ValueError("Elenco dei modelli visibili non valido.")
        settings_store.set_model_access(session, access.get("enabled"),
                                        access.get("models") or [])
    except ValueError as e:
        raise ApiError(422, None, str(e))
    session.add(Setting(key=_COMPLETED_KEY, value="1"))
    session.commit()
    log.info("Configurazione completata dal wizard")
    return {"ok": True}


# --- Admin autenticato: sostituzione della management key ------------------

@router.put("/api/settings/management-key")
async def put_management_key(body: KeyIn,
                             _admin: User = Depends(require_admin)):
    """Sostituisce la management key (pagina API keys). Le chiavi di
    inferenza già create restano valide: appartengono all'account OpenRouter,
    non alla management key che le ha generate."""
    key = await _validate_key(body.key)
    or_client.set_management_key(key)
    catalog.invalidate()
    return {"ok": True, "management_masked": or_client.mask_key(key)}

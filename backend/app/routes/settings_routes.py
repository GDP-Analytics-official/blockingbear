"""Parametri di esercizio (solo admin): la pagina impostazioni del frontend.

GET ritorna registro + valori correnti (chiave, valore, default, minimo,
sezione e testi): il frontend renderizza quello che arriva, così un nuovo
parametro aggiunto a settings_store.REGISTRY compare da solo nella pagina.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import model_access, settings_store
from ..auth import current_user, require_admin
from ..db import User, get_session
from ..errors import ApiError, from_internal
from ..openrouter import catalog, model_rules, provisioning
from ..openrouter import client as or_client

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsIn(BaseModel):
    values: dict[str, int]


class ModelRuleIn(BaseModel):
    # None (o {"kind": "default"}) = nessuna scelta: per la regola personale
    # significa «segui quella dell'installazione»
    rule: dict | None = None


class ModelAccessIn(BaseModel):
    enabled: bool = False
    models: list[str] = []


class AnonDefaultsIn(BaseModel):
    excluded_tags: list[str]
    custom_terms: list[dict]        # [{"text","tag"}]: valida settings_store
    chat_anonymization_policy: str | None = None


class MyTermsIn(BaseModel):
    custom_terms: list[dict]        # [{"text","tag"}]: valida settings_store


class LanguageIn(BaseModel):
    lang: str                       # "it" | "en": valida settings_store


class TourIn(BaseModel):
    done: bool = True


@router.get("")
def get_settings(_admin: User = Depends(require_admin),
                 session: Session = Depends(get_session)):
    """Registro completo dei parametri di esercizio con i valori correnti.

    Ogni voce porta chiave, valore, default, minimo, sezione e testi: il
    frontend disegna la pagina da questo JSON, senza conoscere i parametri.
    Solo admin."""
    return settings_store.describe(session)


@router.put("")
def put_settings(body: SettingsIn, _admin: User = Depends(require_admin),
                 session: Session = Depends(get_session)):
    """Scrive uno o più parametri di esercizio e restituisce il registro
    aggiornato.

    Si mandano solo le chiavi da cambiare. Una chiave sconosciuta o un valore
    sotto il minimo fanno fallire l'intera PUT, senza scriverne nessuna. I
    valori valgono a caldo, senza riavviare il backend. Solo admin."""
    try:
        settings_store.set_values(session, body.values)
    except ValueError as e:
        raise from_internal(e)
    return settings_store.describe(session)


# --- Modello predefinito della chat ----------------------------------------
# Un GET solo per entrambi i livelli: la pagina impostazioni mostra sempre e
# comunque tutti e due (la propria scelta e quella che si eredita restando su
# «default»), quindi separarli vorrebbe dire due chiamate per un JSON di due
# righe. Le PUT invece sono distinte: la madre è dell'admin, la personale è
# di chiunque — admin compreso, che ha anche lui la sua.

async def _model_payload(user, session):
    """I due livelli + il modello a cui la regola si risolve ADESSO. Il
    risolto arriva dal server e non lo ricalcola il browser: la regola è una
    sola cosa, e chi la mostra deve dire quello che poi succede davvero.
    Catalogo irraggiungibile = risolto vuoto, non un errore: la pagina delle
    impostazioni resta utilizzabile.

    `allow_non_zdr` è la deroga dell'installazione, letta anche da chi non è
    admin (le impostazioni di esercizio sono riservate): il selettore del
    modello fisso nasconde i modelli senza provider Zero Data Retention
    quando la deroga è spenta, perché un default così verrebbe rifiutato."""
    out = {"rule": settings_store.default_model_rule(session),
           "personal": model_rules.load(user.chat_model_rule), "resolved": "",
           "allow_non_zdr": _allow_non_zdr(session)}
    try:
        cat = await catalog.get_models(api_key=provisioning.key_for(user))
    except or_client.OpenRouterError:
        return out
    rule = model_rules.effective(user, out["rule"])
    out["resolved"] = model_rules.resolve(
        rule, _rule_catalog(user, session, rule, out["rule"], cat["models"]),
        allow_non_zdr=_allow_non_zdr(session))
    return out


def _rule_catalog(user, session, rule, mother, models):
    """Su quale catalogo si risolve una regola: la PERSONALE solo fra i
    modelli che l'utente può scegliere (white list, model_access.py), la
    madre sul catalogo intero — stessa precedenza di chat_routes._default_model,
    così l'anteprima dice quello che poi succede alla creazione."""
    if rule is mother:
        return models
    return model_access.visible_models(user, session, models)


def _allow_non_zdr(session):
    """La deroga ZDR dell'installazione: la regola guidata non deve scegliere
    un modello che poi verrebbe rifiutato dalle regole privacy."""
    return bool(settings_store.get_int(session, "chat_allow_non_zdr"))


@router.post("/default-model/resolve")
async def resolve_default_model(body: ModelRuleIn,
                                user: User = Depends(current_user),
                                session: Session = Depends(get_session)):
    """A quale modello porterebbe ADESSO una regola, senza salvarla: è
    l'anteprima della procedura guidata, calcolata dal server con la stessa
    funzione che poi userà la creazione della chat. Catalogo irraggiungibile
    = modello vuoto, non un errore."""
    try:
        rule = model_rules.clean_rule(body.rule)
    except ValueError as e:
        raise from_internal(e)
    try:
        cat = await catalog.get_models(api_key=provisioning.key_for(user))
    except or_client.OpenRouterError:
        return {"model": "", "detail": None}
    # l'anteprima è della regola che si sta componendo, cioè quella personale
    # per chiunque non sia admin: si risolve sui modelli visibili
    return model_rules.explain(
        rule, model_access.visible_models(user, session, cat["models"]),
        allow_non_zdr=_allow_non_zdr(session))


@router.get("/default-model")
async def get_default_model(user: User = Depends(current_user),
                            session: Session = Depends(get_session)):
    """I due livelli della regola del modello predefinito — quella
    dell'installazione e quella personale di chi chiama — più il modello a cui
    la regola effettiva si risolve, calcolato dal server sul catalogo."""
    return await _model_payload(user, session)


@router.put("/default-model")
async def put_default_model(body: ModelRuleIn,
                            admin: User = Depends(require_admin),
                            session: Session = Depends(get_session)):
    """La regola dell'INSTALLAZIONE: vale per chi non ne ha una personale."""
    try:
        settings_store.set_default_model_rule(session, body.rule)
    except ValueError as e:
        raise from_internal(e)
    return await _model_payload(admin, session)


@router.put("/my-default-model")
async def put_my_default_model(body: ModelRuleIn,
                               user: User = Depends(current_user),
                               session: Session = Depends(get_session)):
    """La PROPRIA regola. rule=null torna su «default»: da quel momento la
    scelta è di nuovo quella dell'installazione, anche se cambia."""
    try:
        rule = model_rules.clean_rule(body.rule)
    except ValueError as e:
        raise from_internal(e)
    if rule and rule["kind"] == "fixed" and not model_access.is_admin(user):
        # la UI non propone i modelli fuori white list; qui si chiude anche
        # la porta dell'API
        try:
            cat = await catalog.get_models(api_key=provisioning.key_for(user))
            models = cat["models"]
        except or_client.OpenRouterError:
            models = None
        if not model_access.is_allowed(user, session, models, rule["model"]):
            raise ApiError(403, "model_not_allowed",
                           "Questo modello non è fra quelli consentiti "
                           "dall'amministratore: scegline un altro.")
    user.chat_model_rule = model_rules.dump(rule)
    session.commit()
    return await _model_payload(user, session)


# --- Modelli visibili agli utenti (white list, solo admin) -----------------

async def _access_payload(admin, session):
    """La white list + `locked`: il modello a cui si risolve ADESSO la regola
    madre, che la UI mostra selezionato e non deselezionabile (entra nella
    lista da solo, model_access.py). Catalogo irraggiungibile = locked
    vuoto per la guidata; il fisso non ha bisogno del catalogo."""
    out = dict(settings_store.model_access(session))
    rule = settings_store.default_model_rule(session)
    out["locked"] = ""
    if rule and rule["kind"] == "fixed":
        out["locked"] = rule["model"]
    elif rule:
        try:
            cat = await catalog.get_models(api_key=provisioning.key_for(admin))
            out["locked"] = model_rules.resolve(
                rule, cat["models"], allow_non_zdr=_allow_non_zdr(session))
        except or_client.OpenRouterError:
            pass
    return out


@router.get("/model-access")
async def get_model_access(admin: User = Depends(require_admin),
                           session: Session = Depends(get_session)):
    """{enabled, models, locked}: cosa vedono gli utenti non admin."""
    return await _access_payload(admin, session)


@router.put("/model-access")
async def put_model_access(body: ModelAccessIn,
                           admin: User = Depends(require_admin),
                           session: Session = Depends(get_session)):
    """Salva la white list. Il modello predefinito NON va nell'elenco: entra
    da solo a ogni uso, così segue la regola madre quando cambia."""
    try:
        settings_store.set_model_access(session, body.enabled, body.models)
    except ValueError as e:
        raise from_internal(e)
    return await _access_payload(admin, session)


# --- Anonimizzazione: due livelli, come il modello predefinito -------------
# Un GET solo per entrambi: la pagina impostazioni mostra sempre la lista
# globale (a tutti, in sola lettura per chi non è admin) e la propria. Le PUT
# sono distinte: /anonymization è dell'admin e vale per tutti, /my-terms è di
# chiunque — admin compreso, che ha anche lui la sua.
#   custom_terms           lista GLOBALE (solo l'admin la scrive)
#   my_custom_terms        la MIA lista personale
#   effective_custom_terms l'unione che verrà davvero applicata ai miei
#                          documenti, ogni voce con `scope`

def _anon_payload(user, session):
    out = settings_store.anon_defaults(session)
    mine = settings_store.user_terms(user)
    out["my_custom_terms"] = mine
    out["effective_custom_terms"] = settings_store.merge_terms(
        out["custom_terms"], mine, scoped=True)
    return out


@router.get("/anonymization")
def get_anon_defaults(user: User = Depends(current_user),
                      session: Session = Depends(get_session)):
    """I default dell'admin PIÙ i termini personali di chi chiede. Leggibili
    da ogni utente autenticato: la lista globale va mostrata anche a chi non
    può cambiarla, o i suoi termini personali sembrerebbero tutto ciò che
    viene coperto."""
    return _anon_payload(user, session)


@router.put("/anonymization")
def put_anon_defaults(body: AnonDefaultsIn,
                      admin: User = Depends(require_admin),
                      session: Session = Depends(get_session)):
    """La lista GLOBALE (e le categorie escluse): vale per tutti."""
    try:
        settings_store.set_anon_defaults(session, body.excluded_tags,
                                         body.custom_terms,
                                         body.chat_anonymization_policy)
    except ValueError as e:
        raise from_internal(e)
    return _anon_payload(admin, session)


@router.put("/my-terms")
def put_my_terms(body: MyTermsIn, user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """La PROPRIA lista di termini. Non tocca né la lista globale né le
    categorie escluse: un utente aggiunge protezione per sé, non ne toglie a
    nessuno — per questo non serve require_admin. Un termine già fissato
    dall'admin resta comunque coperto, anche se qui non compare."""
    try:
        settings_store.set_user_terms(session, user, body.custom_terms)
    except ValueError as e:
        raise from_internal(e)
    return _anon_payload(user, session)


# --- Lingua dell'interfaccia -----------------------------------------------
# Solo PUT: la lingua si legge da /api/auth/me, che il frontend chiama comunque
# a ogni avvio — un GET dedicato sarebbe un secondo giro per un campo solo.

@router.put("/my-language")
def put_my_language(body: LanguageIn, user: User = Depends(current_user),
                    session: Session = Depends(get_session)):
    """La lingua dell'interfaccia di chi chiede. Non è una preferenza che
    l'amministratore possa fissare per altri, quindi non c'è il livello
    globale che hanno il modello e i termini.

    Col cambio password obbligatorio in sospeso anche questa è bloccata, di
    proposito: il popup della lingua compare DOPO, al primo ingresso nell'app
    (LanguageProvider non lo apre finché il flag è alzato)."""
    try:
        lang = settings_store.set_user_lang(session, user, body.lang)
    except ValueError as e:
        raise from_internal(e)
    return {"lang": lang}


@router.put("/my-tour")
def put_my_tour(body: TourIn, user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """Segna il tutorial del primo accesso come finito (o saltato) per chi
    chiede. `done: false` lo fa ricomparire al prossimo ingresso: serve a chi
    vuole rivederlo dalle impostazioni."""
    user.tour_done = bool(body.done)
    session.commit()
    return {"tour_done": user.tour_done}

"""Quali modelli può vedere — e quindi scegliere — un utente NON admin.

L'amministratore decide fra «tutti i modelli» e una white list flaggata dal
catalogo (settings_store.model_access, pannello Impostazioni e ultimo passo
del wizard). L'insieme consentito è DINAMICO: alla white list si somma sempre
il modello a cui si risolve adesso la regola madre del modello predefinito.
Non è una cortesia ma una condizione di coerenza: le chat degli utenti nascono
su quel modello, e un default che l'utente non può vedere né riscegliere
sarebbe un errore di configurazione garantito. Con la regola guidata quel
modello cambia col catalogo, quindi si ricalcola a ogni uso, mai salvato.

L'admin non è mai limitato: è lui che scrive la lista.

Tre punti la applicano, e devono dire la stessa cosa:
  - il catalogo che arriva al browser (`allowed` accanto ai modelli: la UI
    nasconde il resto ma tiene i metadati del modello di una chat esistente);
  - la scelta del modello (creazione, cambio, invio: 403 model_not_allowed);
  - la regola PERSONALE del modello predefinito, risolta sul sottoinsieme.
"""

from . import settings_store
from .openrouter import model_rules


def allowed_ids(session, models):
    """L'insieme degli id consentiti agli utenti normali, o None = tutti.

    `models` è il catalogo (lista normalizzata) o None se irraggiungibile: la
    regola madre FISSA entra comunque (l'id è nella regola stessa), quella
    guidata solo se c'è il catalogo su cui risolverla — la white list resta
    valida anche a OpenRouter giù."""
    access = settings_store.model_access(session)
    if not access["enabled"]:
        return None
    ids = set(access["models"])
    rule = settings_store.default_model_rule(session)
    if rule and rule["kind"] == "fixed":
        ids.add(rule["model"])
    elif rule and models is not None:
        resolved = model_rules.resolve(
            rule, models,
            allow_non_zdr=bool(settings_store.get_int(session,
                                                      "chat_allow_non_zdr")))
        if resolved:
            ids.add(resolved)
    return ids


def is_admin(user):
    return getattr(user, "role", None) == "admin"


def for_user(user, session, models):
    """Gli id consentiti a QUESTO utente (None = tutti). L'admin vede tutto."""
    if is_admin(user):
        return None
    return allowed_ids(session, models)


def visible_models(user, session, models):
    """Il catalogo ridotto a ciò che l'utente può scegliere (intero per
    l'admin o senza white list). Su questo si risolve la sua regola personale:
    un default personale su un modello proibito non deve aprire chat."""
    ids = for_user(user, session, models)
    if ids is None:
        return models
    return [m for m in models if m["id"] in ids]


def is_allowed(user, session, models, model_id):
    """Può l'utente usare questo modello? Il modello vuoto («nessuno») è
    sempre ammesso: la scelta arriverà dopo."""
    if not model_id:
        return True
    ids = for_user(user, session, models)
    return ids is None or model_id in ids

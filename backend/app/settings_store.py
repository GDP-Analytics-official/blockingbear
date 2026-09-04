"""Parametri di ESERCIZIO modificabili a caldo dal pannello admin (tabella settings).

Differenza con config.py: quelle sono scelte di INSTALLAZIONE (core, code,
percorsi), lette una volta all'avvio e cambiarle richiede il riavvio; questi
sono parametri che l'amministratore regola dall'interfaccia e valgono dal job
successivo, senza riavviare niente.

Il REGISTRY qui sotto è l'unica fonte di verità: chiave, default, minimo
(facoltativo il massimo) e testi mostrati nell'interfaccia. Per aggiungere un
parametro basta una riga nel registro e leggerlo dove serve con
get_int(session, chiave): l'endpoint /api/settings e la pagina admin
(SettingsPage.jsx) si aggiornano da soli. Il valore vive nel DB come testo;
tipo e validazione stanno solo qui.

I parametri sono INTERI; per quelli che sono in realtà un interruttore basta
`"kind": "bool"` con min 0 e max 1 — cambia solo il controllo mostrato nella
pagina admin (casella invece di campo numerico), non il resto della catena.

`section` è una CHIAVE, non un titolo: il nome del gruppo lo scrive il
frontend nella lingua dell'utente (i18n/locales/*/settings.json, voce
`section.<chiave>`). `label` e `help` restano qui in italiano e servono da
ripiego: il frontend li traduce per chiave del parametro e, se una traduzione
manca, mostra questi — un parametro nuovo compare comunque, mai vuoto.
"""

import json
import re

from . import db
from .db import Setting
from .openrouter import model_rules

REGISTRY = {
    # --- Caricamenti e coda -------------------------------------------------
    "max_upload_mb": {
        "default": 50,
        "min": 1,
        "section": "uploads",
        "label": "Dimensione massima dei file caricabili (MB)",
        "help": "Vale per ogni formato: oltre, l'upload viene rifiutato "
                "subito (HTTP 413), prima ancora di entrare in coda.",
    },
    "max_queue": {
        "default": 20,
        "min": 1,
        "section": "uploads",
        "label": "Massimo di elaborazioni in attesa",
        "help": "Oltre, l'upload risponde «coda piena» e l'utente riprova "
                "più tardi. I file in attesa vivono nella RAM del server: "
                "alzarlo aumenta la memoria che la coda può occupare.",
    },
    # --- File Excel ---------------------------------------------------------
    # Tetto sull'ANALISI degli Excel enormi, in chunk (~120 parole l'uno, vedi
    # core.chunk_text). Il default 800 sono ~10 minuti di modello su una CPU
    # d'ufficio. L'elaborazione è in background (jobs.py), quindi alzarlo non
    # blocca l'upload; ma un foglio da 200k righe terrebbe comunque un worker
    # occupato per ORE.
    "xlsx_max_chunks": {
        "default": 800,
        "min": 0,
        "section": "xlsx",
        "label": "Dimensione massima analizzabile (blocchi di testo)",
        "help": "Il contenuto del foglio viene analizzato dal modello in "
                "blocchi da ~120 parole: su una CPU d'ufficio ogni blocco "
                "costa circa 0,75 secondi, quindi 800 blocchi ≈ 10 minuti "
                "di elaborazione. Oltre il limite il caricamento fallisce con "
                "un messaggio che riporta la dimensione del file. "
                "0 = nessun limite.",
    },
    # Percorso VELOCE per i fogli tabellari (engine/xlsx_table.py): il modello
    # analizza solo un campione di righe e le colonne PII si sostituiscono in
    # modo deterministico (unique -> placeholder).
    "xlsx_table_min_rows": {
        "default": 500,
        "min": 0,
        "section": "xlsx",
        "label": "Righe minime per l'anonimizzazione per colonna",
        "help": "I fogli in forma tabellare pulita (header + colonne omogenee) "
                "con almeno queste righe di dati vengono anonimizzati per "
                "colonna: il modello analizza solo un campione e le colonne "
                "PII si sostituiscono per intero, in modo deterministico. "
                "Sotto la soglia (o se il foglio non è tabellare) si usa "
                "l'analisi completa. 0 = percorso disattivato.",
    },
    "xlsx_table_sample_rows": {
        "default": 50,
        "min": 10,
        "section": "xlsx",
        "label": "Righe di campione per foglio tabellare",
        "help": "Quante righe (prime + distribuite + ultima) passano dal "
                "modello per decidere quali colonne contengono PII. Più "
                "righe = decisione più affidabile ma analisi più lenta.",
    },
    "xlsx_table_coverage_pct": {
        "default": 60,
        "min": 1,
        "section": "xlsx",
        "label": "Copertura per marcare una colonna PII (%)",
        "help": "Percentuale minima di valori campionati riconosciuti come "
                "PII perché l'intera colonna venga anonimizzata. Valori "
                "sopra 100 equivalgono a 100.",
    },
    "xlsx_preview_rows": {
        "default": 150,
        "min": 1,
        "section": "xlsx",
        "label": "Righe per foglio nell'anteprima",
        "help": "L'anteprima PDF si genera da una copia troncata a queste "
                "righe per foglio (il file compare come «anteprima parziale»); "
                "la redazione e la verifica dei residui lavorano sempre sul "
                "file completo. Migliaia di righe = PDF enormi e conversioni "
                "LibreOffice lente.",
    },
    # --- Conversione LibreOffice ---------------------------------------------
    "lo_timeout_s": {
        "default": 120,
        "min": 10,
        "section": "libreoffice",
        "label": "Timeout di una conversione (secondi)",
        "help": "Tempo massimo per una singola conversione (formati Office "
                "in ingresso e PDF di anteprima): oltre, l'elaborazione "
                "fallisce con «conversione scaduta». Alzare su macchine lente "
                "se i documenti grandi vanno in timeout.",
    },
    # --- Chat LLM -------------------------------------------------------------
    "chat_max_upload_mb": {
        "default": 20,
        "min": 1,
        "section": "chat",
        "label": "Dimensione massima di un allegato in chat (MB)",
        "help": "Tetto separato (e più basso) da quello dei file di "
                "progetto: gli allegati della chat vengono elaborati "
                "nella sandbox e conservati per tutta la conversazione.",
    },
    "chat_turn_cost_limit_cents": {
        "default": 100,
        "min": 0,
        "section": "chat",
        "label": "Tetto di spesa per singola risposta (centesimi di dollaro)",
        "help": "Una risposta può costare più di quanto sembri: il modello "
                "esegue codice più volte e ogni giro rispedisce tutta la "
                "conversazione. Raggiunto il tetto, al modello viene detto di "
                "smettere di eseguire codice e rispondere con quello che ha "
                "(la risposta arriva comunque). 0 = nessun tetto.",
    },
    "chat_model_attach_mb": {
        "default": 8,
        "min": 1,
        "section": "chat",
        "label": "Dimensione massima di un file inviato al modello (MB)",
        "help": "Immagini e PDF vengono mostrati DIRETTAMENTE al modello (se "
                "lo supporta), quindi viaggiano in ogni richiesta del turno e "
                "si pagano come token: oltre questa soglia il file resta solo "
                "nella sandbox, dove il modello lo apre con del codice.",
    },
    "chat_web_search": {
        "default": 1,
        "min": 0,
        "max": 1,
        "kind": "bool",
        "section": "chat",
        "label": "Consenti la ricerca web nelle chat",
        "help": "Aggiunge al modello due strumenti di sola lettura (ricerca "
                "e lettura pagine) tramite un browser locale al server. "
                "ATTENZIONE alle chat anonimizzate: le query di ricerca "
                "escono verso il motore (DuckDuckGo) con i valori REALI — la "
                "protezione vale sul percorso verso il provider LLM, che "
                "continua a vedere solo segnaposto: i contenuti scaricati "
                "dal web rientrano anonimizzati e passano dal controllo di "
                "uscita. Sui valori non ancora noti al registro la "
                "protezione del testo web è best-effort. Ogni utente può "
                "spegnere la ricerca nella singola conversazione.",
    },
    "chat_ai_marking": {
        "default": 1,
        "min": 0,
        "max": 1,
        "kind": "bool",
        "section": "chat",
        "label": "Marca i file generati dall'AI (AI Act, art. 50)",
        "help": "Scrive nei METADATI di ogni file prodotto dal modello una "
                "dicitura machine-readable in inglese («AI-generated "
                "content», col codice standard IPTC trainedAlgorithmicMedia): "
                "docx/xlsx/pptx nelle proprietà del documento, PDF nei campi "
                "Oggetto e Parole chiave, PNG in un blocco di testo interno, "
                "SVG in un commento. Il contenuto visibile non cambia mai. "
                "csv, json e txt non vengono marcati (un commento ne "
                "romperebbe la lettura): per quelli fa fede l'origine "
                "registrata dall'applicazione.",
    },
    "chat_allow_non_zdr": {
        "default": 0,
        "min": 0,
        "max": 1,
        "kind": "bool",
        "section": "chat",
        "label": "Consenti l'uso di modelli senza Zero Data Retention",
        "help": "Su ogni richiesta imponiamo ai provider Zero Data Retention "
                "e nessuna raccolta dati; circa un terzo dei modelli non ha "
                "provider conformi e viene rifiutato. Attivando questa voce, "
                "chi sceglie uno di quei modelli può concedere una deroga "
                "per la SINGOLA conversazione, con l'avviso sempre in vista: "
                "il provider potrà conservare i dati inviati e usarli per "
                "addestrare i suoi modelli. Disattivata, non può derogare "
                "nessuno — nemmeno l'amministratore.",
    },
    # --- Sessioni -------------------------------------------------------------
    "token_ttl_hours": {
        "default": 12,
        "min": 1,
        "section": "sessions",
        "label": "Durata della sessione di login (ore)",
        "help": "Scaduta, l'utente rifà il login. Vale per i login successivi "
                "al salvataggio: le sessioni già aperte mantengono la loro "
                "scadenza.",
    },
}


def get_int(session, key):
    """Valore corrente del parametro (dal DB, o il default se mai salvato).
    Un valore corrotto a mano nel DB ricade sul default: mai bloccare un job
    per un parametro illeggibile."""
    meta = REGISTRY[key]
    row = session.get(Setting, key)
    if row is None:
        return meta["default"]
    try:
        value = max(meta["min"], int(row.value))
    except (TypeError, ValueError):
        return meta["default"]
    return min(value, meta["max"]) if "max" in meta else value


def current(key):
    """Valore corrente per i punti SENZA una Session già aperta
    (engine/convert, auth): sessione usa-e-getta, costo trascurabile. Se il DB
    non è inizializzato (smoke test dei moduli engine) vale il default."""
    if db.SessionLocal is None:
        return REGISTRY[key]["default"]
    with db.SessionLocal() as s:
        return get_int(s, key)


def describe(session):
    """Registro + valori correnti, nell'ordine del registro (per GET /api/settings)."""
    return [{"key": key, "value": get_int(session, key), **meta}
            for key, meta in REGISTRY.items()]


# --- Default di anonimizzazione (pannello admin) ---------------------------
# Non stanno nel REGISTRY (che descrive parametri INTERI): sono due valori
# JSON nella stessa tabella settings, che l'admin fissa dal pannello.
#   excluded_tags: categorie da NON anonimizzare. Si salva l'esclusione (e non
#                  l'inclusione) perché un tag nuovo di un modello aggiornato
#                  deve nascere ATTIVO, mai in chiaro per una lista stantia.
#   custom_terms:  [{"text","tag"}], termini da coprire sempre (case
#                  insensitive, a parole intere), col tag scelto.
#
# I termini hanno DUE livelli, che si sommano invece di sostituirsi: questa
# lista globale (solo l'admin la scrive, tutti la subiscono) e quella personale
# di ogni utente (User.anon_terms_json). Vedi `merge_terms`: la lista che
# arriva al motore è sempre l'unione delle due, così un utente può coprire
# i propri termini senza imporli agli altri e senza poter smontare la regola
# dell'installazione.
_ANON_KEYS = {"excluded_tags": "anon_excluded_tags",
              "custom_terms": "anon_custom_terms"}
_CHAT_POLICY_KEY = "chat_anonymization_policy"
_CHAT_POLICIES = {"required", "optional"}


def clean_tag(raw):
    """Nome di tag -> forma placeholder (maiuscolo, solo A-Z0-9_): "codice
    cliente" -> "CODICECLIENTE". L'underscore si CONSERVA: i tag del modello
    possono contenerlo (es. ID_DOC) e il confronto con le label in analyze()
    è esatto — toglierlo qui significherebbe escludere un tag inesistente.
    ValueError se non resta almeno una lettera o cifra."""
    tag = re.sub(r"[^A-Za-z0-9_]+", "", str(raw or "")).upper()
    if not re.search(r"[A-Za-z0-9]", tag):
        raise ValueError("Nome del tag mancante o senza caratteri validi "
                         "(servono lettere o cifre).")
    return tag


def clean_excluded(raw):
    """Categorie escluse: lista di tag, normalizzati e senza duplicati."""
    if not isinstance(raw, (list, tuple)):
        raise ValueError("Categorie escluse: attesa una lista di tag.")
    return sorted({clean_tag(t) for t in raw})


def term_key(raw):
    """La chiave con cui due termini sono LO STESSO termine: spazi compressi,
    senza spazi ai bordi, case insensitive.

    Sta in una funzione sola perché la usano in tre punti che devono
    concordare — clean_terms (dedup dentro una lista), merge_terms (dedup fra
    i due livelli) e add_custom_term (il termine c'è già?). Se divergessero,
    lo stesso testo potrebbe entrare due volte con due tag diversi e
    detect_custom produrrebbe due candidati sullo stesso span: quale dei due
    vince diventerebbe un dettaglio dell'ordinamento."""
    return re.sub(r"\s+", " ", str(raw or "")).strip().casefold()


def clean_terms(raw):
    """Termini custom [{"text","tag"}]: spazi compressi, almeno 2 caratteri
    alfanumerici (la stessa soglia di pdf_export._too_noisy: coprire frammenti
    più corti devasterebbe il documento), tag normalizzato, dedup case
    insensitive sul testo (vince la prima voce)."""
    if not isinstance(raw, (list, tuple)):
        raise ValueError("Termini da anonimizzare: attesa una lista.")
    out, seen = [], set()
    for t in raw:
        if not isinstance(t, dict):
            raise ValueError("Termini da anonimizzare: voce non valida.")
        text = re.sub(r"\s+", " ", str(t.get("text") or "")).strip()
        alnum = re.sub(r"[\W_]+", "", text)
        if len(alnum) < 2 or (len(alnum) == 2 and alnum.isdigit()):
            raise ValueError(f"Termine troppo corto o ambiguo per essere "
                             f"anonimizzato in modo sicuro: «{text}».")
        key = term_key(text)
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "tag": clean_tag(t.get("tag"))})
    return out


def anon_defaults(session):
    """Default correnti: {"excluded_tags": [...], "custom_terms": [...]}.
    Un valore corrotto a mano nel DB ricade sul default vuoto: mai bloccare
    un upload per un parametro illeggibile."""
    out = {"excluded_tags": [], "custom_terms": [],
           "chat_anonymization_policy": chat_anonymization_policy(session)}
    cleaners = {"excluded_tags": clean_excluded, "custom_terms": clean_terms}
    for field, db_key in _ANON_KEYS.items():
        row = session.get(Setting, db_key)
        if row is None:
            continue
        try:
            out[field] = cleaners[field](json.loads(row.value))
        except (ValueError, TypeError):
            pass
    return out


def chat_anonymization_policy(session):
    """Policy riletta a ogni upload/invio. `optional` preserva il
    comportamento delle installazioni aggiornate finché un admin non impone
    esplicitamente la protezione obbligatoria."""
    row = session.get(Setting, _CHAT_POLICY_KEY)
    value = (row.value if row is not None else "optional").strip().lower()
    return value if value in _CHAT_POLICIES else "optional"


def set_chat_anonymization_policy(session, value, *, commit=True):
    value = str(value or "").strip().lower()
    if value not in _CHAT_POLICIES:
        raise ValueError("Anonimizzazione chat: scegli 'required' oppure "
                         "'optional'.")
    row = session.get(Setting, _CHAT_POLICY_KEY)
    if row is None:
        session.add(Setting(key=_CHAT_POLICY_KEY, value=value))
    else:
        row.value = value
    if commit:
        session.commit()
    return value


def set_anon_defaults(session, excluded_tags, custom_terms,
                      chat_anonymization_policy_value=None):
    """Valida e salva i default di anonimizzazione (ValueError user-facing,
    tutto-o-niente come set_values). Ritorna i valori normalizzati."""
    values = {"excluded_tags": clean_excluded(excluded_tags),
              "custom_terms": clean_terms(custom_terms)}
    for field, db_key in _ANON_KEYS.items():
        payload = json.dumps(values[field], ensure_ascii=False)
        row = session.get(Setting, db_key)
        if row is None:
            session.add(Setting(key=db_key, value=payload))
        else:
            row.value = payload
    if chat_anonymization_policy_value is not None:
        set_chat_anonymization_policy(
            session, chat_anonymization_policy_value, commit=False)
    session.commit()
    values["chat_anonymization_policy"] = chat_anonymization_policy(session)
    return values


# --- Termini personali (User.anon_terms_json) ------------------------------

def user_terms(user):
    """I termini PERSONALI di `user`, normalizzati. Utente assente o valore
    corrotto a mano nel DB = lista vuota: mai bloccare una redazione per un
    parametro illeggibile, e comunque i termini globali continuano a valere."""
    raw = getattr(user, "anon_terms_json", None)
    if not raw:
        return []
    try:
        return clean_terms(json.loads(raw))
    except (ValueError, TypeError):
        return []


def set_user_terms(session, user, terms, *, commit=True):
    """Sostituisce la lista personale di `user` (ValueError user-facing, come
    set_anon_defaults). Ritorna i valori normalizzati."""
    values = clean_terms(terms)
    user.anon_terms_json = json.dumps(values, ensure_ascii=False)
    if commit:
        session.commit()
    return values


def merge_terms(global_terms, personal_terms, *, scoped=False):
    """La lista EFFETTIVA di un utente: globali + suoi, dedup case insensitive
    sul testo (lo stesso metro di clean_terms).

    In caso di collisione VINCE IL GLOBALE. Il tag di un termine è una scelta
    dell'amministratore che vale per tutta l'installazione: lasciarlo
    rietichettare renderebbe lo stesso valore [ALTRO_n] nei documenti di chi
    l'ha ridefinito e [TAG_n] in quelli di tutti gli altri — due nomi per la
    stessa cosa, che è esattamente ciò che il registro esiste per evitare.

    `scoped=True` annota ogni voce con `scope` ("global" o "personal") per chi
    la MOSTRA; il motore riceve sempre e solo {text, tag} (vedi
    engine.core.detect_custom)."""
    out, seen = [], set()
    for scope, terms in (("global", global_terms), ("personal", personal_terms)):
        for term in terms or ():
            key = term_key(term.get("text"))
            if key in seen:
                continue          # già preso dal livello globale: quello vince
            seen.add(key)
            out.append({**term, "scope": scope} if scoped else term)
    return out


def add_custom_term(session, user, text, tag="CUSTOM"):
    """Aggiunge UN termine alla lista PERSONALE di `user` (casella
    `viewer.selection.saveTerm` del popup di selezione manuale). Non passa da require_admin: la
    lista è sua e l'operazione è solo additiva — rendere un testo sempre
    anonimizzato aumenta la protezione, mai il contrario.

    Il dedup guarda la lista EFFETTIVA (globale + personale): un termine già
    coperto dalla regola dell'installazione non va ricopiato tra i personali,
    o l'utente si ritroverebbe un doppione che non ha scritto e che non sparisce
    quando l'admin cambia idea. False = già coperto (con qualunque tag), True
    = aggiunto. ValueError se il termine non passa clean_terms — non dovrebbe
    accadere dai popup: la soglia di anonymize-text (_too_noisy) è la stessa,
    quella della chat più severa."""
    term = clean_terms([{"text": text, "tag": tag}])[0]
    mine = user_terms(user)
    effective = merge_terms(anon_defaults(session)["custom_terms"], mine)
    key = term_key(term["text"])
    if any(term_key(t["text"]) == key for t in effective):
        return False
    set_user_terms(session, user, mine + [term])
    return True


# --- Lingua dell'interfaccia -----------------------------------------------
# Preferenza PERSONALE (User.lang), senza controparte globale: non è un
# parametro di esercizio ma una proprietà di chi guarda lo schermo, e
# l'amministratore non ha motivo di imporla.
#
# Qui il backend fa SOLO da custode: l'elenco vero delle lingue e i testi
# stanno nel frontend (src/i18n), questo serve a non salvare in colonna un
# codice che nessun catalogo saprebbe poi caricare.
LANGS = ("it", "en")
DEFAULT_LANG = "it"


def clean_lang(raw):
    """Codice lingua normalizzato. Accetta anche le forme regionali che manda
    un browser ("it-IT", "en_GB"): conta la lingua, non il paese. ValueError
    user-facing se non è una delle lingue previste."""
    code = str(raw or "").strip().lower().replace("_", "-").split("-")[0]
    if code not in LANGS:
        raise ValueError(
            "Lingua non disponibile: scegli tra " + ", ".join(LANGS) + ".")
    return code


def user_lang(user):
    """La lingua di `user`, o None se non ne ha mai scelta una. None è
    diverso da DEFAULT_LANG: dice al frontend che può proporre la scelta e
    salvare la propria, mentre "it" è una decisione già presa."""
    try:
        return clean_lang(getattr(user, "lang", None))
    except ValueError:
        return None


def set_user_lang(session, user, raw, *, commit=True):
    """Salva la lingua di `user`. Ritorna il codice normalizzato."""
    code = clean_lang(raw)
    user.lang = code
    if commit:
        session.commit()
    return code


# --- Modello predefinito della chat (regola «madre») -----------------------
# Come i default di anonimizzazione: un JSON nella tabella settings, non un
# parametro intero del REGISTRY. La regola personale di ciascun utente vive
# invece su User.chat_model_rule; chi resta su «default» segue questa.
_DEFAULT_MODEL_KEY = "chat_default_model"


def default_model_rule(session):
    """La regola dell'installazione, o None se l'admin non ne ha fissata una."""
    row = session.get(Setting, _DEFAULT_MODEL_KEY)
    return model_rules.load(row.value if row is not None else None)


def set_default_model_rule(session, rule):
    """Valida e salva la regola madre (None = nessun default). ValueError
    user-facing. Ritorna la regola normalizzata."""
    rule = model_rules.clean_rule(rule)
    payload = model_rules.dump(rule)
    row = session.get(Setting, _DEFAULT_MODEL_KEY)
    if row is None:
        session.add(Setting(key=_DEFAULT_MODEL_KEY, value=payload))
    else:
        row.value = payload
    session.commit()
    return rule


# --- Modelli visibili agli utenti (white list) -----------------------------
# {"enabled": bool, "models": [id, ...]}: se `enabled`, chi non è admin vede e
# può scegliere solo i modelli elencati — più quello a cui si risolve ADESSO
# la regola madre, che entra da solo (vedi model_access.py): una white list
# che nascondesse il default farebbe nascere chat su un modello invisibile.
_MODEL_ACCESS_KEY = "chat_model_access"
_MODEL_ID_MAX = 128         # Conversation.model è String(128)
_MODEL_ACCESS_MAX = 1000    # il catalogo intero è ~400 voci


def clean_model_access(enabled, models):
    """{"enabled", "models"} normalizzato (id ripuliti, senza doppioni,
    nell'ordine dato) o ValueError user-facing."""
    if not isinstance(models, (list, tuple)):
        raise ValueError("L'elenco dei modelli deve essere una lista.")
    if len(models) > _MODEL_ACCESS_MAX:
        raise ValueError(f"Troppi modelli: il massimo è {_MODEL_ACCESS_MAX}.")
    out, seen = [], set()
    for raw in models:
        if not isinstance(raw, str):
            raise ValueError("Ogni modello deve essere un id testuale.")
        mid = raw.strip()
        if not mid or len(mid) > _MODEL_ID_MAX or any(c.isspace() for c in mid):
            raise ValueError(f"Id di modello non valido: {raw!r}.")
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return {"enabled": bool(enabled), "models": out}


def model_access(session):
    """La white list dell'installazione; assente o corrotta = tutti visibili."""
    row = session.get(Setting, _MODEL_ACCESS_KEY)
    if row is None or not row.value:
        return {"enabled": False, "models": []}
    try:
        data = json.loads(row.value)
        return clean_model_access(data.get("enabled"), data.get("models") or [])
    except (ValueError, TypeError, AttributeError):
        return {"enabled": False, "models": []}


def set_model_access(session, enabled, models, *, commit=True):
    """Valida e salva la white list. ValueError user-facing. Ritorna il
    valore normalizzato."""
    access = clean_model_access(enabled, models)
    payload = json.dumps(access, ensure_ascii=False)
    row = session.get(Setting, _MODEL_ACCESS_KEY)
    if row is None:
        session.add(Setting(key=_MODEL_ACCESS_KEY, value=payload))
    else:
        row.value = payload
    if commit:
        session.commit()
    return access


def set_values(session, values):
    """Valida e salva {chiave: valore}. Tutto-o-niente: il primo valore non
    valido solleva ValueError (messaggio già user-facing) e non salva nulla."""
    parsed = {}
    for key, raw in values.items():
        meta = REGISTRY.get(key)
        if meta is None:
            raise ValueError(f"Parametro sconosciuto: {key}.")
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{meta['label']}: serve un numero intero.")
        if v < meta["min"]:
            raise ValueError(f"{meta['label']}: il minimo è {meta['min']}.")
        if "max" in meta and v > meta["max"]:
            raise ValueError(f"{meta['label']}: il massimo è {meta['max']}.")
        parsed[key] = v
    for key, v in parsed.items():
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=str(v)))
        else:
            row.value = str(v)
    session.commit()

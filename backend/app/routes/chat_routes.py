"""Route della chat LLM via OpenRouter.

Due gruppi:
  /api/openrouter/*             catalogo modelli e stato dell'integrazione
  /api/chats/*                  conversazioni, messaggi (SSE), allegati

Ogni utente parla con OpenRouter con la SUA chiave di inferenza
(users.openrouter_key, creata dalla management key: openrouter/provisioning).

Lo streaming del turno NON usa il pattern EventSource dei job (token in query
string): qui serve un POST con body, quindi il frontend fa fetch + reader e
l'autenticazione viaggia nel normale header Authorization.

L'anonimizzazione è una proprietà della CONVERSAZIONE (`Conversation.
anonymized`, scelta alla creazione) e avviene tutta dentro lo stream del
turno, prima della chiamata a OpenRouter: gli eventi `anon_start` /
`anon_progress` / `anon_done` la raccontano all'utente mentre accade. Nessun
allegato viene toccato all'upload.

La privacy si impone LATO SERVER su ogni richiesta:
provider.zdr può solo stringere le impostazioni account, mai allentarle, e
data_collection="deny" esclude i provider che conservano i dati. Se nessun
provider è conforme OpenRouter risponde 503: la UI ha uno stato dedicato.
"""

import asyncio
import datetime
import difflib
import json
import mimetypes
import re
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import chat_anonymization as chat_anon
from .. import chat_staging
from .. import db, jobs, model_access, project_files, purge, settings_store
from ..auth import current_user, require_admin
from ..config import DATA_DIR
from ..db import (Attachment, ChatMessage, Conversation, ConversationEntity,
                  Job, Project, User, get_session, iso_utc)
from ..engine import ai_mark, image_ocr
from ..errors import ApiError, from_internal
from ..logging_setup import get_logger
from ..openrouter import (briefing, browser, catalog, model_rules,
                          provisioning, sandbox)
from ..openrouter import chat as or_chat
from ..openrouter import tools as or_tools
from ..openrouter import client as or_client

log = get_logger("blockingbear.chat")

router = APIRouter(tags=["chat"])

CHATS_DIR = DATA_DIR / "chats"

# zdr per richiesta è in OR con le impostazioni account (può
# solo stringere), quindi imporlo qui non ha controindicazioni
_PRIVACY_PROVIDER = {"zdr": True, "data_collection": "deny"}


def _provider_prefs(options):
    """Le preferenze provider della richiesta: di norma Zero Data Retention +
    nessuna raccolta dati.

    La DEROGA per conversazione (`options["allow_non_zdr"]`) le toglie tutte e
    due, e non è una dimenticanza: `data_collection: "deny"` vuol dire "solo
    provider che non raccolgono dati", quindi lasciarlo attivo escluderebbe
    comunque gli endpoint non-ZDR e il 503 resterebbe identico — una
    concessione finta. La deroga è esplicita fino in fondo: il provider potrà
    conservare i dati e usarli per addestrare. Per questo esiste solo se
    l'amministratore l'ha abilitata (vedi _guard_privacy_option) e
    l'interfaccia la dichiara per tutta la durata della conversazione."""
    if (options or {}).get("allow_non_zdr"):
        return {}
    return dict(_PRIVACY_PROVIDER)


def _guard_privacy_option(new_options, current_options, session):
    """La deroga esiste solo se l'amministratore l'ha abilitata per questa
    installazione (`chat_allow_non_zdr` nel pannello). Due livelli distinti:
    l'AMMINISTRATORE decide se la capacità esiste, l'UTENTE decide se usarla
    in quella conversazione — e la paga vedendo l'avviso per tutto il tempo.
    Con l'interruttore spento non deroga nessuno, nemmeno l'admin: così la
    promessa "nessun dato lascia il perimetro" è una proprietà del server,
    non della disciplina di chi lo usa.

    Si controlla il PASSAGGIO da spento ad acceso, non il valore: se
    l'interruttore globale viene spento dopo, le conversazioni già in deroga
    restano modificabili nelle altre opzioni (e _provider_prefs continua a
    rispettarne la scelta finché l'utente non la revoca)."""
    if not (new_options or {}).get("allow_non_zdr"):
        return
    if (current_options or {}).get("allow_non_zdr"):
        return
    if not settings_store.get_int(session, "chat_allow_non_zdr"):
        raise ApiError(403, "zdr_required",
                       "L'uso di modelli senza Zero Data Retention "
                       "non è abilitato su questo server: un "
                       "amministratore può consentirlo dalle "
                       "Impostazioni (sezione Chat LLM).")


_running = {}       # conv_id -> asyncio.Event: turno in corso (e suo stop)
# conv_id -> asyncio.Event: turno fermo in attesa che l'utente confermi le
# fusioni del registro (vedi il passo 1b di send_message). La alza la route
# /messages/continue, che gira sul loop: un Event asincrono non si tocca da
# un altro thread.
_merge_gates = {}

# conv_id -> buffer del turno (vedi _turn_buffer): il turno gira in un task
# in background e scrive qui i suoi eventi SSE; la risposta del POST e il
# riaggancio (GET /messages/live) sono solo lettori del buffer. È quello che
# permette a un refresh del browser di NON uccidere il turno: chiudere la
# connessione chiude un lettore, non il turno. Il buffer dell'ultimo turno
# resta finché non ne parte un altro (o la chat viene eliminata): così un
# riaggancio che arriva a turno appena concluso rigioca comunque tutto.
_turn_streams = {}

# quanto si aspetta la conferma delle fusioni prima di annullare il turno: una
# scheda abbandonata (o un portatile sospeso) non deve tenere la conversazione
# occupata per sempre (il turno ora sopravvive alla connessione, quindi questo
# timeout è l'unica uscita se il browser sparisce).
_MERGE_WAIT_S = 900


def _turn_buffer(conv_id):
    """Nuovo buffer per il turno che sta partendo: sostituisce quello del
    turno precedente (ormai rigiocabile solo da getChat, che ha i messaggi
    persistiti)."""
    buf = {"chunks": [], "done": False, "event": asyncio.Event()}
    _turn_streams[conv_id] = buf
    return buf


async def _pump_turn(gen, buf):
    """Consuma il generatore del turno e ne accumula gli eventi nel buffer.
    Il turno VIVE qui (asyncio.create_task), non nella risposta HTTP: nessun
    lettore, o un lettore che se ne va, non lo rallenta e non lo ferma.
    I ping keep-alive non si accumulano (ogni lettore genera i propri): in un
    replay sarebbero solo rumore vecchio."""
    try:
        async for chunk in gen:
            if not chunk.startswith(":"):
                buf["chunks"].append(chunk)
                buf["event"].set()
    finally:
        buf["done"] = True
        buf["event"].set()


async def _follow_turn(buf):
    """Un lettore del buffer: rigioca dall'inizio e poi segue in diretta fino
    alla fine del turno. È il corpo sia della risposta del POST sia del
    riaggancio. L'Event è condiviso tra i lettori: set() risolve subito i
    future già in attesa, quindi il clear() di un lettore non può far
    perdere il risveglio a un altro (e il ricontrollo dopo il clear copre
    l'append arrivato nel frattempo)."""
    i = 0
    while True:
        if i < len(buf["chunks"]):
            yield buf["chunks"][i]
            i += 1
            continue
        if buf["done"]:
            return
        buf["event"].clear()
        if i < len(buf["chunks"]) or buf["done"]:
            continue
        try:
            await asyncio.wait_for(buf["event"].wait(), timeout=15)
        except asyncio.TimeoutError:
            yield ": ping\n\n"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _leak_list(leaks, limit=3):
    """I valori sfuggiti, detti in modo che l'utente sappia cosa cercare."""
    shown = [f'"{value}" ({ph})' for ph, value in leaks[:limit]]
    rest = len(leaks) - len(shown)
    return ", ".join(shown) + (f" e altri {rest}" if rest > 0 else "")


def _sse(event):
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def _error_text(event, limit=4000):
    """Bound a persisted/displayed error without logging prompts or secrets."""
    text = str((event or {}).get("message") or "Unknown chat error").strip()
    return text[:limit]


def _log_text(value, limit=1000):
    """Make a provider error safe for a single structured log line."""
    return " ".join(str(value or "").split())[:limit]


# --- Catalogo e stato -----------------------------------------------------------

@router.get("/api/openrouter/models")
async def list_models(refresh: bool = False,
                      user: User = Depends(current_user),
                      session: Session = Depends(get_session)):
    """Catalogo normalizzato (cache 1h in RAM). refresh=1 forza il fetch.

    `allowed`: gli id che QUESTO utente può scegliere (white list
    dell'admin + modello predefinito, model_access.py), o null = tutti. Il
    catalogo resta intero perché la UI ha bisogno dei metadati anche del
    modello di una chat aperta prima della restrizione (nome nell'avviso,
    opzioni): nasconde dal selettore, non dimentica."""
    try:
        cat = await catalog.get_models(force=refresh,
                                       api_key=provisioning.key_for(user))
    except or_client.OpenRouterError as e:
        raise ApiError(502, "catalog_unavailable",
                       f"Catalogo OpenRouter non disponibile: {e}",
                       error=str(e))
    ids = model_access.for_user(user, session, cat["models"])
    # copia: il dizionario della cache è condiviso fra tutti gli utenti
    return {**cat, "allowed": None if ids is None else sorted(ids)}


async def _check_model_allowed(user, session, model):
    """403 se l'utente non può usare il modello (white list dell'admin). Il
    catalogo serve solo a risolvere il default guidato: se OpenRouter è giù
    si valuta con quello che si sa (model_access.allowed_ids)."""
    if not model or model_access.is_admin(user):
        return
    try:
        cat = await catalog.get_models(api_key=provisioning.key_for(user))
        models = cat["models"]
    except or_client.OpenRouterError:
        models = None
    if not model_access.is_allowed(user, session, models, model):
        raise ApiError(403, "model_not_allowed",
                       "Questo modello non è fra quelli consentiti "
                       "dall'amministratore: scegline un altro.")


@router.get("/api/openrouter/status")
async def openrouter_status(user: User = Depends(current_user),
                            session: Session = Depends(get_session)):
    """Stato dell'integrazione: QUESTO utente ha la sua chiave? sandbox
    attiva? deroga privacy abilitata? L'admin vede anche se la management key
    è impostata (mascherata), i crediti globali dell'account e la propria
    chiave mascherata."""
    key = provisioning.key_for(user)
    out = {"configured": key is not None, "sandbox": sandbox.status(),
           "browser": browser.status(),
           "personal_key": bool(user.openrouter_key),
           # feature abilitata dall'admin E browser utilizzabile: è la
           # condizione con cui la UI mostra l'interruttore per conversazione
           "web_search": bool(settings_store.get_int(session, "chat_web_search")
                              and browser.available()),
           "allow_non_zdr": bool(settings_store.get_int(
               session, "chat_allow_non_zdr")),
           "chat_anonymization_policy":
               settings_store.chat_anonymization_policy(session)}
    if user.openrouter_key:
        try:
            # limite e residuo della PROPRIA chiave: ogni utente vede il suo
            # budget senza aspettare la fattura (GET /key accetta la chiave
            # di inferenza stessa)
            out["key_info"] = await catalog.key_info(user.openrouter_key)
        except or_client.OpenRouterError:
            pass                    # il budget è cortesia, non blocca la chat
    if user.role == "admin":
        out["provisioning"] = provisioning.configured()
        out["management_masked"] = or_client.mask_key(
            or_client.get_management_key())
        out["masked"] = or_client.mask_key(key)
        if provisioning.configured():
            try:
                # saldo GLOBALE dell'account (management key), non della
                # singola chiave: è il numero che decide se si può spendere
                out["credits"] = await provisioning.credits()
            except or_client.OpenRouterError as e:
                out["credits_error"] = str(e)
    return out


# --- Conversazioni ---------------------------------------------------------------

def _get_conv(conv_id, user, session):
    conv = session.get(Conversation, conv_id)
    if conv is None or (user.role != "admin" and conv.owner_id != user.id):
        raise ApiError(404, "conversation_not_found",
                       "Conversazione non trovata.")
    return conv


def _scope(conv):
    """Lo scope del registro PII (e del lock): il progetto per le chat di
    progetto, la conversazione stessa altrimenti."""
    return conv.project_id or conv.id


class ChatIn(BaseModel):
    model: str = ""
    title: str = ""
    anonymized: bool = False
    # chat DENTRO un progetto: eredita il modo dal progetto (anonymized
    # viene ignorato) e ne condivide registro e file confermati
    project_id: str | None = None


class ChatPatch(BaseModel):
    title: str | None = None
    model: str | None = None
    options: dict | None = None
    anonymized: bool | None = None
    # {"excluded_tags": [...]} = categorie da lasciare in chiaro in QUESTA
    # conversazione; {"excluded_tags": null} torna a seguire i default admin
    anon_options: dict | None = None


def _n_messages(session, conv_id):
    return (session.query(func.count(ChatMessage.id))
            .filter_by(conv_id=conv_id).scalar() or 0)


def _conv_payload(session, conv):
    """Il descrittore + le categorie di anonimizzazione EFFETTIVE (default
    dell'admin, o quelle scelte in questa conversazione). Non sta in
    `Conversation.descriptor()` perché calcolarle richiede la sessione."""
    return {**conv.descriptor(),
            "anon_options": chat_anon.anon_options_view(session, conv)}


@router.get("/api/chats")
def list_chats(project: str | None = None,
               user: User = Depends(current_user),
               session: Session = Depends(get_session)):
    """Le PROPRIE conversazioni (anche per l'admin: la lista è personale;
    l'accesso puntuale per id resta possibile all'admin per assistenza).
    Senza `project` la lista sono le chat LIBERE; con `project` le chat di
    quel progetto (che non compaiono mai nella lista libera)."""
    q = session.query(Conversation).filter_by(owner_id=user.id)
    if project:
        q = q.filter_by(project_id=project)
    else:
        q = q.filter(Conversation.project_id.is_(None))
    return [c.descriptor()
            for c in q.order_by(Conversation.updated_at.desc()).limit(200)]


# --- ricerca nelle chat ------------------------------------------------------
# La ricerca del modal della sidebar. Fuzzy semplice: ogni termine matcha
# come substring case-insensitive, più un ripescaggio per i typo — la ILIKE
# porta su anche i messaggi che contengono solo il PREFISSO del termine e
# difflib conferma in Python, ma solo su quei candidati. Il grosso del lavoro
# resta al database (scansione con tetto sulle righe): Python rilegge al
# massimo _SEARCH_ROWS messaggi. ilike() e non like(): su Postgres LIKE è
# case-sensitive (su SQLite no), ilike compila nel giusto idioma per dialetto.

_FUZZY_PREFIX = 4      # prefisso della LIKE di ripescaggio typo
_FUZZY_MIN = 0.78      # somiglianza minima parola/termine (difflib)
_SNIPPET_CTX = 60      # caratteri di contesto attorno al primo match
_SEARCH_ROWS = 400     # tetto ai messaggi candidati riletti in Python


def _like_pattern(term):
    esc = (term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
    return f"%{esc}%"


def _token_score(text_low, token):
    """Quanto `token` è presente in `text_low`: 1.0 da substring esatta,
    ~0.9*ratio se una parola che inizia col suo stesso prefisso gli somiglia
    (typo in coda), 0 altrimenti. Il fuzzy guarda solo le parole col prefisso
    della LIKE di ripescaggio: è ciò che può aver portato qui la riga, e
    tiene il costo lineare."""
    if token in text_low:
        return 1.0
    if len(token) <= _FUZZY_PREFIX:
        return 0.0
    pre = token[:_FUZZY_PREFIX]
    best = 0.0
    for word in re.findall(r"\w+", text_low):
        if not word.startswith(pre) or abs(len(word) - len(token)) > 3:
            continue
        ratio = difflib.SequenceMatcher(None, word, token).ratio()
        if ratio >= _FUZZY_MIN:
            best = max(best, 0.9 * ratio)
    return best


def _search_snippet(body, tokens):
    """Una finestra di testo attorno al primo termine trovato, su una riga."""
    low = body.lower()
    pos = -1
    for t in tokens:
        p = low.find(t)
        if p < 0 and len(t) > _FUZZY_PREFIX:
            p = low.find(t[:_FUZZY_PREFIX])
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        pos = 0
    start = max(0, pos - _SNIPPET_CTX)
    end = min(len(body), pos + _SNIPPET_CTX * 2)
    piece = " ".join(body[start:end].split())
    return (("…" if start > 0 else "") + piece
            + ("…" if end < len(body) else ""))


# DEVE stare prima di GET /api/chats/{conv_id}: le route si provano in ordine
# di registrazione e "search" è un conv_id plausibile.
@router.get("/api/chats/search")
def search_chats(q: str = "", user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """Cerca nelle chat LIBERE dell'utente, su titoli e testo dei messaggi.

    Si cerca il testo COME MEMORIZZATO: nelle chat anonimizzate i messaggi
    utente hanno l'originale in chiaro (display_content), le risposte del
    modello restano coi TAG — un nome cercato si trova dove l'utente l'ha
    scritto, non dentro le risposte. I candidati sono i messaggi più recenti
    (limit su created_at desc): su archivi enormi la coda antica può restare
    fuori, ed è il compromesso voluto."""
    tokens = [t for t in dict.fromkeys(q.lower().split()) if len(t) >= 2][:6]
    if not tokens:
        return []

    text = func.coalesce(ChatMessage.display_content, ChatMessage.content)
    conds, title_conds = [], []
    for t in tokens:
        pats = [_like_pattern(t)]
        if len(t) > _FUZZY_PREFIX:
            pats.append(_like_pattern(t[:_FUZZY_PREFIX]))
        conds += [text.ilike(p, escape="\\") for p in pats]
        title_conds += [Conversation.title.ilike(p, escape="\\")
                        for p in pats]

    rows = (session.query(Conversation, text)
            .join(ChatMessage, ChatMessage.conv_id == Conversation.id)
            .filter(Conversation.owner_id == user.id,
                    Conversation.project_id.is_(None),
                    ChatMessage.role.in_(("user", "assistant")),
                    or_(*conds))
            # seq come spareggio: su Windows due now() consecutivi cadono
            # nello stesso microsecondo e i messaggi di un turno hanno
            # created_at IDENTICI — senza spareggio Postgres li restituisce
            # in ordine arbitrario (SQLite, per caso, in ordine di rowid) e
            # lo snippet cambierebbe da un motore all'altro.
            .order_by(ChatMessage.created_at.desc(), ChatMessage.seq.asc())
            .limit(_SEARCH_ROWS).all())
    titled = (session.query(Conversation)
              .filter(Conversation.owner_id == user.id,
                      Conversation.project_id.is_(None),
                      or_(*title_conds))
              .order_by(Conversation.updated_at.desc())
              .limit(50).all())

    found = {}   # conv_id -> punteggi per termine + snippet migliore

    def bucket(conv):
        return found.setdefault(conv.id, {
            "conv": conv, "scores": dict.fromkeys(tokens, 0.0),
            "snippet": "", "snippet_score": 0.0, "n_hits": 0})

    for conv, body in rows:
        low = (body or "").lower()
        per = {t: _token_score(low, t) for t in tokens}
        total = sum(per.values())
        if total <= 0:      # ripescato dal prefisso ma difflib non conferma
            continue
        b = bucket(conv)
        b["n_hits"] += 1
        for t, s in per.items():
            b["scores"][t] = max(b["scores"][t], s)
        # lo snippet viene dal messaggio più pertinente; a parità vince il
        # più recente (le righe arrivano già in ordine di data decrescente)
        if total > b["snippet_score"]:
            b["snippet_score"] = total
            b["snippet"] = _search_snippet(body, tokens)

    for conv in titled:
        per = {t: _token_score((conv.title or "").lower(), t) for t in tokens}
        if sum(per.values()) <= 0:
            continue
        b = bucket(conv)
        for t, s in per.items():
            b["scores"][t] = max(b["scores"][t], s)

    # prima chi copre più termini della query, poi la più recente
    ranked = sorted(found.values(),
                    key=lambda b: (-sum(b["scores"].values()),
                                   -(b["conv"].updated_at.timestamp()
                                     if b["conv"].updated_at else 0)))
    return [{"id": b["conv"].id,
             "title": b["conv"].title,
             "anonymized": bool(b["conv"].anonymized),
             "updated_at": iso_utc(b["conv"].updated_at),
             "snippet": b["snippet"],
             "n_hits": b["n_hits"]} for b in ranked[:30]]


async def _default_model(user, session):
    """Il modello con cui nasce una chat quando il browser non ne impone uno:
    la regola personale dell'utente, o quella dell'installazione se sta su
    «default» (openrouter/model_rules.py).

    Si risolve UNA VOLTA, alla creazione, e da lì il modello è fissato sulla
    conversazione: una regola che cambia idea a metà discussione cambierebbe
    interlocutore. Se non si risolve — catalogo irraggiungibile, azienda senza
    modelli usabili — vale il ripiego di «nessun default»: nessun modello, lo
    sceglie l'utente. Mai un errore in creazione per un default.

    Ritorna (id_modello, opzioni_iniziali): le opzioni sono quelle legate al
    modello fisso della regola, {} negli altri casi."""
    mother = settings_store.default_model_rule(session)
    rule = model_rules.effective(user, mother)
    if not rule:
        return "", {}
    try:
        cat = await catalog.get_models(api_key=provisioning.key_for(user))
    except or_client.OpenRouterError:
        return "", {}
    # la regola PERSONALE si risolve solo fra i modelli che l'utente può
    # scegliere (white list dell'admin); la madre sul catalogo intero, perché
    # è lei a definire il modello che entra sempre nella lista
    models = (cat["models"] if rule is mother
              else model_access.visible_models(user, session, cat["models"]))
    model = model_rules.resolve(
        rule, models,
        allow_non_zdr=bool(settings_store.get_int(session, "chat_allow_non_zdr")))
    # le opzioni iniziali viaggiano con il modello fisso della regola: senza
    # modello risolto non hanno a cosa riferirsi
    return model, (rule.get("options") or {}) if model else {}


@router.post("/api/chats")
async def create_chat(body: ChatIn, user: User = Depends(current_user),
                      session: Session = Depends(get_session)):
    """La scelta del modo si fa QUI e vale per tutta la conversazione: è il
    punto in cui l'ambiguità si risolve una volta sola, invece di ripresentarsi
    a ogni file e a ogni invio. Con la policy obbligatoria non c'è scelta.
    Una chat DENTRO un progetto non sceglie niente: il modo è quello del
    progetto (è la condizione perché registro e file siano coerenti)."""
    anonymized = body.anonymized
    project_id = None
    if body.project_id:
        project = session.get(Project, body.project_id)
        if project is None or (user.role != "admin"
                               and project.owner_id != user.id):
            raise ApiError(404, "project_not_found", "Progetto non trovato.")
        anonymized = bool(project.anonymized)
        project_id = project.id
    elif settings_store.chat_anonymization_policy(session) == "required":
        anonymized = True
    # tagli alle lunghezze di colonna (String(128)/String(200)): SQLite non
    # le applica, Postgres sì — senza taglio un input lungo diventa un 500
    model = body.model.strip()[:128]
    options = None
    if not model:
        model, options = await _default_model(user, session)
    else:
        await _check_model_allowed(user, session, model)
    conv = Conversation(owner_id=user.id, model=model[:128],
                        anonymized=1 if anonymized else 0,
                        project_id=project_id,
                        title=body.title.strip()[:200]
                        or "Nuova conversazione")
    if options:
        conv.options_json = json.dumps(options, ensure_ascii=False)
    session.add(conv)
    session.commit()
    return conv.descriptor()


@router.get("/api/chats/{conv_id}")
def get_chat(conv_id: str, user: User = Depends(current_user),
             session: Session = Depends(get_session)):
    """La conversazione intera: messaggi, allegati, registro con i suggerimenti
    di fusione e speso complessivo.

    I messaggi sono decodificati per la lettura con la mappa della LORO
    versione del registro, non con quella corrente: un turno partito prima di
    una fusione si rilegge come è partito. `mode_locked` dice se il modo
    (anonimizzato / in chiaro) è ormai fissato, `busy` se c'è un turno in
    volo. Conversazione di un altro utente: 404."""
    conv = _get_conv(conv_id, user, session)
    msgs = (session.query(ChatMessage).filter_by(conv_id=conv.id)
            .order_by(ChatMessage.seq).all())
    atts = (session.query(Attachment).filter_by(conv_id=conv.id)
            .order_by(Attachment.created_at).all())
    # totale speso nella conversazione: la somma dei turni (usage sta sul
    # messaggio assistant finale di ciascuno). Il costo di un'analisi lunga
    # non deve restare invisibile fino alla fattura di fine mese.
    total = {"cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0}
    for m in msgs:
        if not m.usage_json:
            continue
        try:
            u = json.loads(m.usage_json)
        except ValueError:
            continue
        for k in total:
            total[k] += u.get(k) or 0
    mappings_by_version = {}

    def mapping_for(message):
        version = message.mapping_version or 0
        if version not in mappings_by_version:
            mappings_by_version[version] = chat_anon.conversation_mapping(
                session, _scope(conv), max_version=version)
        return mappings_by_version[version]

    return {**_conv_payload(session, conv),
            "chat_anonymization_policy":
                settings_store.chat_anonymization_policy(session),
            # il modo si cambia solo finché non è partito niente: dopo, un
            # cambio renderebbe la conversazione mista, che è esattamente
            # quello che questa versione toglie di mezzo
            "mode_locked": bool(msgs),
            "messages": [m.descriptor(mapping_for(m),
                                      chat_anon.decode_for_display,
                                      chat_anon.protected_placeholders)
                         for m in msgs],
            "attachments": [a.descriptor() for a in atts],
            "entity_suggestions": chat_anon.merge_suggestions(session,
                                                              _scope(conv)),
            "usage_total": total,
            "busy": conv.id in _running}


@router.patch("/api/chats/{conv_id}")
async def patch_chat(conv_id: str, body: ChatPatch,
                     user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    """Modifica titolo, modello, modo, opzioni del turno e categorie da
    anonimizzare: si manda solo quello che cambia.

    I due vincoli che questa route fa rispettare: il modo non si cambia più
    da quando è partito un messaggio (409 `mode_locked_started`) né mai su
    una chat di progetto (409 `mode_locked_project`), e le categorie non si
    toccano con un turno in corso (409 `categories_turn_running`). Cambiare le
    categorie scarta l'anteprima pre-invio già calcolata."""
    conv = _get_conv(conv_id, user, session)
    if body.title is not None:
        conv.title = body.title.strip()[:200] or conv.title
    if body.model is not None:
        await _check_model_allowed(user, session, body.model.strip()[:128])
        conv.model = body.model.strip()[:128]
    if body.anonymized is not None and bool(conv.anonymized) != body.anonymized:
        if conv.project_id:
            raise ApiError(
                409, "mode_locked_project",
                "Il modo di una chat di progetto è quello del progetto "
                "e non si cambia.")
        if _n_messages(session, conv.id):
            raise ApiError(
                409, "mode_locked_started",
                "Il modo di una conversazione già avviata non si cambia: "
                "i turni già inviati resterebbero nel contesto nella forma "
                "in cui sono partiti. Crea una nuova conversazione.")
        if (not body.anonymized and settings_store.chat_anonymization_policy(
                session) == "required"):
            raise ApiError(
                403, "anon_required",
                "L'amministratore ha reso obbligatoria l'anonimizzazione "
                "delle chat su questo server.")
        conv.anonymized = 1 if body.anonymized else 0
    if body.options is not None:
        _guard_privacy_option(body.options,
                              json.loads(conv.options_json or "{}"), session)
        conv.options_json = json.dumps(body.options, ensure_ascii=False)
    if body.anon_options is not None:
        # Le categorie valgono dai turni SUCCESSIVI: cambiarle mentre un turno
        # è in volo darebbe un messaggio protetto con una regola e verificato
        # con un'altra (il controllo di uscita rilegge le opzioni correnti).
        if conv.project_id:
            raise ApiError(
                409, "categories_from_project",
                "Le categorie da anonimizzare di una chat di progetto "
                "si gestiscono dal progetto: valgono per tutte le sue chat "
                "e per i suoi file.")
        if conv.id in _running:
            raise ApiError(
                409, "categories_turn_running",
                "C'è una risposta in corso in questa conversazione: le "
                "categorie da anonimizzare si cambiano a turno finito.")
        try:
            chat_anon.set_anon_options(conv,
                                       body.anon_options.get("excluded_tags"))
        except ValueError as e:
            raise from_internal(e)
        # cambiare le categorie invalida l'anteprima pre-invio già calcolata
        chat_staging.discard(conv.id)
    session.commit()
    return _conv_payload(session, conv)


@router.delete("/api/chats/{conv_id}")
def delete_chat(conv_id: str, user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """Elimina la conversazione e tutto quello che le appartiene.

    Il registro PII se ne va solo con una chat LIBERA: quello di una chat di
    progetto è del progetto e sopravvive alle sue chat."""
    conv = _get_conv(conv_id, user, session)
    # messaggi, allegati, byte su disco, sandbox e — solo per una chat LIBERA —
    # il suo registro: la cascata sta in app/purge.py
    purge.purge_conversation(session, conv)
    return {"ok": True}


# --- Registro delle entità ------------------------------------------------------

class MergeIn(BaseModel):
    into: str


def _registry_payload(session, conv):
    """La risposta comune alla revisione delle entità. Con un turno in
    anteprima porta anche il descrittore staged: la fusione NON ri-redige
    niente (i file che partiranno sono quelli, il modello saprà che i due tag
    sono la stessa entità dal system prompt), ma l'ultimo step del modal
    ricalcola da lì la lista dei suggerimenti."""
    scope = _scope(conv)
    out = {"entities": chat_anon.registry_view(session, scope),
           "suggestions": chat_anon.merge_suggestions(session, scope)}
    if chat_staging.state(conv.id) is not None:
        out["staged"] = chat_staging.descriptor(conv.id)
    return out


@router.get("/api/chats/{conv_id}/entities")
def list_entities(conv_id: str, user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    """Il registro della conversazione + le fusioni che il resolver non si
    sente di fare da solo. Serve alla revisione: un cognome da solo e il nome
    intero restano entità diverse finché non è l'utente a dirlo."""
    conv = _get_conv(conv_id, user, session)
    return _registry_payload(session, conv)


@router.post("/api/chats/{conv_id}/entities/{entity_id}/merge")
def merge_entity(conv_id: str, entity_id: str, body: MergeIn,
                 user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """Dichiara che due entità del registro sono la stessa persona o lo stesso
    ente: `entity_id` confluisce in `into`.

    Non ri-redige niente di già scritto — i segnaposto partiti restano quelli
    — ma da qui in avanti i due valori condividono un solo segnaposto, e il
    system prompt dice al modello che sono la stessa entità. Risponde con il
    registro aggiornato e i suggerimenti ricalcolati."""
    conv = _get_conv(conv_id, user, session)
    scope = _scope(conv)
    with chat_anon.conversation_lock(scope):
        session.expire_all()
        holder = chat_anon.registry_holder(session, conv)
        source = session.get(ConversationEntity, entity_id)
        target = session.get(ConversationEntity, body.into)
        if (source is None or target is None or source.conv_id != scope
                or target.conv_id != scope):
            raise ApiError(404, "entity_not_found", "Entità non trovata.")
        try:
            chat_anon.merge_entities(session, holder, source, target)
        except ValueError as e:
            raise from_internal(e, 409)
        session.commit()
    return _registry_payload(session, conv)


@router.post("/api/chats/{conv_id}/entities/{entity_id}/keep-separate")
def keep_entity_separate(conv_id: str, entity_id: str,
                         user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
    """Rifiuta un suggerimento di fusione: l'entità resta distinta e non viene
    più proposta.

    È la risposta negativa a merge: senza, la revisione riproporrebbe la
    stessa coppia a ogni apertura."""
    conv = _get_conv(conv_id, user, session)
    entity = session.get(ConversationEntity, entity_id)
    if entity is None or entity.conv_id != _scope(conv):
        raise ApiError(404, "entity_not_found", "Entità non trovata.")
    entity.merge_checked = 1
    session.commit()
    return _registry_payload(session, conv)


# --- Allegati --------------------------------------------------------------------

# stessa sanificazione dei file di progetto: caratteri riservati + taglio a
# 200 conservando l'estensione (le colonne filename sono VARCHAR(256) e
# Postgres le applica davvero)
_safe_name = project_files.safe_filename


def _att_path(att):
    return CHATS_DIR / att.conv_id / f"{att.id}_{att.filename}"


def _original_att_path(att):
    if att.original_path:
        return Path(att.original_path)
    return _att_path(att)


@router.post("/api/chats/{conv_id}/attachments")
async def upload_attachment(conv_id: str, file: UploadFile,
                            user: User = Depends(current_user),
                            session: Session = Depends(get_session)):
    """Allegato di input: i byte restano sul server (la sandbox li monta in
    /workspace/inputs). Verso OpenRouter parte solo ciò che il codice del
    modello stampa — con un'eccezione voluta: immagini e PDF, se il modello
    scelto sa guardarli, vengono allegati al messaggio (vedi _model_part).

    Subito dopo il salvataggio si calcola la SCHEDA del contenuto
    (openrouter/briefing.py, nessun LLM): fogli e colonne di un Excel, pagine
    di un PDF... Finisce nel contesto del messaggio e sul chip in interfaccia.

    Qui NON si anonimizza: in una chat anonimizzata si controlla solo che il
    formato sia trattabile (rifiuto immediato altrimenti) e il file resta
    `pending` fino all'invio, quando viene redatto insieme a tutto il turno."""
    conv = _get_conv(conv_id, user, session)
    data = await file.read()
    limit = settings_store.get_int(session, "chat_max_upload_mb")
    if len(data) > limit * 1024 * 1024:
        raise ApiError(413, "chat_file_too_large",
                       f"File troppo grande: il limite per gli "
                       f"allegati in chat è {limit} MB.", max=limit)
    filename = _safe_name(file.filename)
    if conv.anonymized:
        problem = await asyncio.to_thread(chat_anon.upload_eligibility,
                                          filename, data)
        if problem:
            raise HTTPException(415, problem)
    index = ((session.query(func.count(Attachment.id))
              .filter_by(conv_id=conv.id, direction="in").scalar()) or 0) + 1
    ext = Path(filename).suffix.lower()
    model_filename = f"allegato_{index:02d}{ext}"
    att = Attachment(conv_id=conv.id, direction="in", source="upload",
                     filename=filename, display_filename=filename,
                     model_filename=model_filename, size=len(data),
                     anonymization_status=("pending" if conv.anonymized
                                           else "raw"),
                     mime=file.content_type
                     or mimetypes.guess_type(filename)[0])
    session.add(att)
    session.flush()
    path = _att_path(att)
    att.original_path = str(path)
    session.commit()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    # in un thread: parsare un xlsx da 20 MB nel loop asincrono bloccherebbe
    # tutte le altre richieste (compreso lo streaming di un'altra chat)
    card = await asyncio.to_thread(briefing.describe, str(path), filename,
                                   att.mime)
    att.briefing_json = json.dumps(card, ensure_ascii=False)
    # quante immagini OCR-izzabili contiene: il frontend le usa per il popup
    # "vuoi usare l'OCR?" all'invio (0 sui formati legacy: si convertono solo
    # al momento del turno)
    att.n_images = await asyncio.to_thread(image_ocr.count_images, data, ext)
    session.commit()
    # un allegato nuovo rende stantia un'eventuale anteprima pre-invio
    chat_staging.discard(conv.id)
    return att.descriptor()


@router.get("/api/chats/{conv_id}/attachments/{att_id}")
def download_attachment(conv_id: str, att_id: str,
                        user: User = Depends(current_user),
                        session: Session = Depends(get_session)):
    """Scarica un allegato con il suo nome vero.

    Per un upload è sempre l'ORIGINALE: la copia redatta si scarica da
    /anonymized, ed è un dettaglio di quello che esce verso il modello, non
    di quello che l'utente ha caricato. Byte spariti dal disco: 410
    `file_gone`."""
    conv = _get_conv(conv_id, user, session)
    att = session.get(Attachment, att_id)
    if att is None or att.conv_id != conv.id:
        raise ApiError(404, "attachment_not_found", "Allegato non trovato.")
    # Il download dell'input è sempre l'originale locale; la variante protetta
    # e il filename neutro sono un dettaglio dell'egress verso il modello.
    path = (_original_att_path(att) if att.direction == "in" else _att_path(att))
    if not path.is_file():
        raise ApiError(410, "file_gone",
                       "File non più disponibile sul server.")
    return FileResponse(path, filename=att.filename,
                        media_type=att.mime or "application/octet-stream")


@router.get("/api/chats/{conv_id}/attachments/{att_id}/anonymized")
def download_attachment_anonymized(conv_id: str, att_id: str,
                                   user: User = Depends(current_user),
                                   session: Session = Depends(get_session)):
    """La copia redatta di un upload. Esiste solo dopo che anonymize_turn l'ha
    scritta (invio o anteprima pre-invio, vedi chat_staging): prima di allora
    404, è la
    condizione con cui il frontend decide se mostrare il bottone. Lo status
    `failed` può lasciare su disco un _protected stantio di un tentativo
    precedente: non va servito, quindi si guarda lo status e non il path."""
    conv = _get_conv(conv_id, user, session)
    att = session.get(Attachment, att_id)
    if att is None or att.conv_id != conv.id or att.direction != "in":
        raise ApiError(404, "attachment_not_found", "Allegato non trovato.")
    if att.anonymization_status != "protected" or not att.protected_path:
        raise ApiError(404, "anon_version_missing",
                       "Versione anonimizzata non disponibile: "
                       "viene creata all'invio del messaggio.")
    path = Path(att.protected_path)
    if not path.is_file():
        raise ApiError(410, "file_gone",
                       "File non più disponibile sul server.")
    # L'estensione viene dal file protetto: la redazione lavora sul formato
    # convertito (.doc -> .docx), quindi può non coincidere con l'originale.
    stem = (att.display_filename or att.filename).rsplit(".", 1)[0] or "allegato"
    name = f"{stem}_anonimizzato{path.suffix}"
    return FileResponse(path, filename=name,
                        media_type=mimetypes.guess_type(name)[0]
                        or "application/octet-stream")


@router.delete("/api/chats/{conv_id}/attachments/{att_id}")
def remove_chat_attachment(conv_id: str, att_id: str,
                           user: User = Depends(current_user),
                           session: Session = Depends(get_session)):
    """Toglie un allegato non ancora inviato: riga, byte originali, copia
    redatta e cache OCR.

    Un allegato già partito in un messaggio non si rimuove (409
    `attachment_already_sent`): la history deve restare quella che il modello
    ha visto. Scarta anche l'anteprima pre-invio, che conteneva il file."""
    conv = _get_conv(conv_id, user, session)
    with chat_anon.conversation_lock(_scope(conv)):
        session.expire_all()
        att = session.get(Attachment, att_id)
        if att is None or att.conv_id != conv.id or att.direction != "in":
            raise ApiError(404, "attachment_not_found", "Allegato non trovato.")
        if att.message_id:
            raise ApiError(409, "attachment_already_sent",
                           "Un allegato già inviato resta nella history.")
        paths = {_original_att_path(att)}
        if att.protected_path:
            paths.add(Path(att.protected_path))
        ocr_cache = chat_anon._ocr_cache_path(att)
        if ocr_cache is not None:
            paths.add(ocr_cache)
        session.delete(att)
        session.commit()
        chat_staging.discard(conv.id)   # l'anteprima conteneva questo file
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}


# --- Invio messaggio (streaming SSE) ----------------------------------------------

class MessageIn(BaseModel):
    content: str
    model: str | None = None
    options: dict | None = None
    # preview=True: "anonimizza e mostra prima di inviare" — il turno viene
    # anonimizzato e messo in ANTEPRIMA (chat_staging), niente parte.
    # from_staged=True: invia il turno già preparato (e eventualmente
    # ritoccato) dall'anteprima, senza ri-anonimizzare.
    preview: bool = False
    from_staged: bool = False
    # ocr=True: le immagini negli allegati di QUESTO turno (o gli allegati
    # immagine stessi) vengono lette con l'OCR e redatte nei pixel. È la
    # scelta del popup "sono state trovate delle immagini": mai un default.
    ocr: bool = False


def _next_seq(session, conv_id):
    top = (session.query(func.max(ChatMessage.seq))
           .filter_by(conv_id=conv_id).scalar())
    return (top or 0) + 1


# --- La storia verso OpenRouter (allegati inclusi) ---------------------------

def _model_part(att, modalities, max_bytes):
    """L'allegato come CONTENUTO che il modello guarda, o None se deve
    restare solo nella sandbox (regole in briefing.as_model_part, le stesse
    dei file di progetto)."""
    return briefing.as_model_part(chat_anon.attachment_path(att),
                                  chat_anon.attachment_model_name(att),
                                  att.mime, modalities, max_bytes)


def _attachment_block(atts, shown_ids):
    """Le schede degli allegati, in coda al testo del messaggio utente.
    Formato asciutto e delimitato: è contesto, non un esempio di risposta."""
    lines = ["<allegati>",
             "File allegati a questo messaggio. Nella sandbox sono in "
             "/workspace/inputs (sola lettura) col nome esatto qui sotto."]
    for att in atts:
        name = chat_anon.attachment_model_name(att)
        line = briefing.as_prompt(name, att.size,
                                  chat_anon.attachment_model_briefing(att))
        # quante immagini contiene il file (conteggio dell'upload, taglie
        # minime già filtrate): senza questa riga il modello assume che non
        # ce ne siano — è il gancio del tool read_document_images. Sui file
        # immagine è ovvio e si tace.
        if (att.n_images or 0) and not (att.mime or "").startswith("image/"):
            line += f"\n    immagini nel file: {att.n_images}"
        if att.id in shown_ids:
            line += ("\n    (allegato anche qui sotto: puoi guardarlo "
                     "direttamente)")
        lines.append(line)
    lines.append("</allegati>")
    return "\n".join(lines)


def _with_attachments(msg, atts, modalities, max_bytes):
    """Il messaggio utente + schede dei file (+ i file stessi se il modello
    sa guardarli). Con parti multimodali `content` diventa una lista, col
    testo per primo."""
    parts, shown = [], set()
    for att in atts:
        part = _model_part(att, modalities, max_bytes)
        if part is not None:
            parts.append(part)
            shown.add(att.id)
    text = ((msg.get("content") or "") + "\n\n"
            + _attachment_block(atts, shown)).strip()
    if not parts:
        return {**msg, "content": text}
    return {**msg, "content": [{"type": "text", "text": text}] + parts}


def _history(session, conv_id, modalities, max_bytes):
    """Il thread nel formato OpenRouter, con gli allegati ricuciti sul
    messaggio utente che li ha portati (Attachment.message_id).

    Si ricostruisce da zero a ogni turno perché i byte non stanno in SQLite:
    stessa posizione e stesso contenuto a ogni giro, quindi il prefisso della
    conversazione resta identico e il prompt caching regge."""
    by_msg = {}
    for att in (session.query(Attachment)
                .filter_by(conv_id=conv_id, direction="in")
                .order_by(Attachment.created_at)):
        if att.message_id:
            by_msg.setdefault(att.message_id, []).append(att)
    out = []
    for m in (session.query(ChatMessage).filter_by(conv_id=conv_id)
              .order_by(ChatMessage.seq)):
        # An assistant row containing only an error is a durable UI marker,
        # not a model message. Sending an empty failed response back to the
        # provider would corrupt the next turn's conversation history.
        if (m.role == "assistant" and m.error
                and not m.model_content and not m.tool_calls_json):
            continue
        d = m.to_openrouter()
        if m.role == "user" and by_msg.get(m.id):
            d = _with_attachments(d, by_msg[m.id], modalities, max_bytes)
        out.append(d)
    return out


def _restore_artifact(att, path, mapping, source=None, images=None):
    """Rimette i valori veri nel file appena prodotto. Un artifact consegnato
    coi TAG è inservibile — e il modello lo annuncia come pronto all'uso —
    quindi si ripristina subito quel che si sa ripristinare, dichiarando in
    chiaro cosa è rimasto fuori (immagini e altri formati binari).

    Ritorna i byte scritti, o None se il file è rimasto com'era."""
    try:
        data, n, left, extra = chat_anon.restore_artifact(path, mapping,
                                                          source, images)
    except Exception as e:                      # un artifact rotto non ferma il turno
        log.warning(f"ripristino di {path.name} fallito: {e!r}")
        return None
    if data is None:
        att.anonymization_report_json = json.dumps({
            "notice": "Prodotto da input protetti: questo formato non può "
                      "essere ripristinato, contiene ancora i segnaposto."
        }, ensure_ascii=False)
        return None
    path.write_bytes(data)
    att.size = path.stat().st_size
    att.anonymization_status = "restored" if not left else "protected"
    notice = (f"{n} segnaposto sostituiti con i valori reali."
              if not left else
              f"{n} segnaposto sostituiti; {len(left)} non erano nel "
              "registro e restano nel file.")
    # I limiti del ripristino PDF vanno detti, non sono deducibili dal file:
    # un valore più largo del segnaposto viene scritto più piccolo (il PDF non
    # rifluisce il testo) e un segnaposto disegnato dentro un'immagine non è
    # testo, quindi non è né sostituibile né rilevabile. Il primo limite non
    # esiste quando il PDF è stato rigenerato dal .docx sorgente.
    if extra.get("chart"):
        notice = (f"Grafico ridisegnato dall'SVG sorgente: {n} segnaposto "
                  "sostituiti con i valori reali nelle etichette.")
    if extra.get("image_restored"):
        notice = ("Copia di un'immagine allegata: consegnata la versione "
                  "originale al posto di quella redatta.")
    if extra.get("rerendered"):
        notice += (" Il PDF è stato rigenerato dal documento sorgente: "
                   "l'impaginazione tiene conto della lunghezza dei valori "
                   "reali.")
    if extra.get("shrunk"):
        notice += (f" {len(extra['shrunk'])} valori sono più lunghi del "
                   "segnaposto e sono stati scritti con un corpo ridotto per "
                   "non invadere il testo accanto.")
    swapped = extra.get("images_swapped") or 0
    if swapped:
        notice += (f" {swapped} grafici incastonati sono stati sostituiti con "
                   "la versione ripristinata.")
    if extra.get("images", 0) > swapped:
        notice += (f" Il documento contiene {extra['images']} immagini: se il "
                   "modello ha disegnato dei segnaposto dentro un'immagine "
                   "senza lasciarne l'SVG sorgente, quelli restano.")
    if extra.get("lost"):
        notice += (f" ATTENZIONE: {len(extra['lost'])} segnaposto sono stati "
                   "rimossi senza riuscire a scrivere il valore.")
    att.anonymization_report_json = json.dumps(
        {"restored": n, "remaining": left, "notice": notice, **extra},
        ensure_ascii=False)
    return data


# Da quale file si rigenera un artifact, per estensione: il PDF dal documento
# che lo ha prodotto (reimpagina invece di rimpicciolire), il grafico png
# dall'SVG gemello (nel png le etichette sono pixel, nell'SVG sono testo).
_ARTIFACT_SOURCES = {".pdf": ".docx", ".png": ".svg"}


def _artifact_order(item):
    """I grafici prima di tutto il resto. Un documento che ne incastona uno
    deve trovarlo già ripristinato, e l'ordine alfabetico non lo garantisce:
    "fatturato.docx" viene prima di "grafico.png" e il .docx uscirebbe con le
    etichette coi segnaposto."""
    return (0 if Path(item[1]).suffix.lower() in (".png", ".svg") else 1,
            item[0])


def _register_artifacts(conv_id, event, anonymized=False, mapping=None,
                        media=None, sources=None, registered=None):
    """I file prodotti dall'esecuzione (percorsi HOST nello staging sandbox,
    che verrà distrutto) diventano Attachment con i byte copiati in
    CHATS_DIR: è il livello artifact degli allegati. L'evento verso il
    browser perde i percorsi host e guadagna i descrittori scaricabili.

    `sources`, `media` e `registered` accumulano per tutto il TURNO, non per
    evento: il modello scrive il .docx in un'esecuzione e lo converte in PDF
    in quella dopo, e disegna il grafico prima di incastonarlo in un
    documento. Una memoria per evento perderebbe il sorgente proprio nei casi
    in cui serve. `media` (chat_anonymization.MediaPool) porta anche le
    immagini degli input protetti accoppiate ai loro originali: si caricano
    al primo artifact del turno, mai prima.

    `registered` ({percorso relativo: attachment id}) evita i doppioni: lo
    stesso percorso può uscire da `new_files` più volte nel turno (tipico:
    la prima esecuzione fallisce DOPO il savefig e il modello riesegue il
    codice da capo) e in quel caso è lo stesso file riscritto, quindi si
    aggiorna la riga esistente invece di crearne una seconda.

    L'SVG di un grafico fa eccezione e si accoppia solo DENTRO l'esecuzione che
    lo ha prodotto: png e svg escono dallo stesso `savefig`, e un png
    ridisegnato più tardi senza il suo svg verrebbe accoppiato con quello
    vecchio — consegnando il grafico sbagliato coi nomi veri. Meglio un grafico
    non ripristinato, che si vede e si dichiara."""
    result = dict(event["result"])
    host_files = result.pop("files", None) or {}
    descriptors = []
    if sources is None:
        sources = {}
    # Registro vuoto = nessun segnaposto in circolazione: il modello non ha
    # mai visto un TAG, quindi gli output sono già in chiaro e non c'è
    # niente da ripristinare né da dichiarare. Senza questo distinguo ogni
    # artifact di una chat anonimizzata uscirebbe "protected" con l'avviso
    # "contiene ancora i segnaposto" anche a registro vuoto.
    restore = bool(anonymized and mapping)
    if host_files:
        # I sorgenti sono i percorsi nello STAGING, che nessuno riscrive: la
        # copia in CHATS_DIR viene ripristinata, quella qui conserva i
        # segnaposto (e il modello, che legge solo la sandbox, non vede mai i
        # valori reali).
        here = {(p.suffix.lower(), p.stem): p
                for p in map(Path, host_files.values())}
        sources.update(here)
        with db.SessionLocal() as s:
            marking = settings_store.get_int(s, "chat_ai_marking")
            if restore and media is not None:
                conv = s.get(Conversation, conv_id)
                if conv is not None:
                    media.load_inputs(s, conv)
            for _rel, host in sorted(host_files.items(), key=_artifact_order):
                src = Path(host)
                if not src.is_file():
                    continue
                filename = _safe_name(src.name)
                if restore:
                    # il modello nomina i file con quello che legge, TAG
                    # inclusi: "Sollecito_[FULLNAME_1].docx" non è un nome
                    filename = _safe_name(
                        chat_anon.restore_filename(filename, mapping))
                att = None
                if registered is not None and _rel in registered:
                    att = s.get(Attachment, registered[_rel])
                if att is not None:
                    # stesso percorso già consegnato in questo turno: è il
                    # file riscritto, la riga (e il suo id, a cui la UI è
                    # già agganciata) si riusa coi byte nuovi
                    dst = Path(att.original_path)
                    att.filename = filename
                    att.size = src.stat().st_size
                    att.anonymization_status = ("protected" if restore
                                                else "raw")
                    att.anonymization_report_json = None
                    s.commit()
                else:
                    att = Attachment(conv_id=conv_id, direction="out",
                                     source="sandbox", filename=filename,
                                     size=src.stat().st_size,
                                     mime=mimetypes.guess_type(filename)[0],
                                     anonymization_status=("protected" if restore
                                                           else "raw"))
                    s.add(att)
                    s.flush()
                    dst = _att_path(att)
                    att.original_path = str(dst)
                    s.commit()
                if registered is not None:
                    registered[_rel] = att.id
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                if restore:
                    ext = src.suffix.lower()
                    known = here if ext == ".png" else sources
                    source = known.get(
                        (_ARTIFACT_SOURCES.get(ext, ""), src.stem))
                    data = _restore_artifact(att, dst, mapping, source, media)
                    if data is not None and ext == ".png" and media is not None:
                        # un grafico ripristinato vale anche per le copie
                        # incastonate nei documenti dello stesso turno
                        media.add_pair(src.read_bytes(), data)
                    s.commit()
                # marcatura AI Act (art. 50) nei metadati: DOPO il ripristino,
                # che riscrive o rigenera i byte e cancellerebbe il tag
                if marking and ai_mark.mark_file(dst):
                    att.size = dst.stat().st_size
                    s.commit()
                descriptors.append(att.descriptor())
    # message_id degli artifact: lo mette _persist_turn a fine turno, quando
    # il messaggio assistant esiste (qui c'è solo lo step in corso)
    return {**event, "result": result, "attachments": descriptors}


def _persist_turn(conv_id, new_messages, done_event, artifact_ids=(),
                  anonymized=False, mapping_version=0, error_message=None):
    """Salva i messaggi prodotti dal turno (già in formato OpenRouter:
    to_openrouter() li ricostruirà identici) e aggancia gli artifact del
    turno al messaggio assistant finale (la UI mostra i download sotto la
    bolla giusta). Sessione propria: gira nel finally del generatore SSE,
    anche a client disconnesso. Non solleva mai."""
    try:
        with db.SessionLocal() as s:
            conv = s.get(Conversation, conv_id)
            if conv is None:
                return              # conversazione eliminata nel frattempo
            if anonymized:
                # i tool web possono aver registrato entità a metà turno:
                # i messaggi del turno si stampano con la versione di FINE
                # turno, o i segnaposto nati dal web non si decodificherebbero
                # più al ricaricamento della chat (mapping_for è limitata
                # alla versione della riga)
                holder = chat_anon.registry_holder(s, conv)
                mapping_version = max(mapping_version,
                                      holder.mapping_version or 0)
            seq = _next_seq(s, conv_id)
            last_assistant = None
            for m in new_messages:
                row = ChatMessage(conv_id=conv_id, seq=seq, role=m["role"],
                                  content=m.get("content"),
                                  model_content=m.get("content"),
                                  anonymized=(1 if anonymized else 0),
                                  mapping_version=mapping_version)
                if m["role"] == "assistant":
                    if m.get("tool_calls"):
                        row.tool_calls_json = json.dumps(
                            m["tool_calls"], ensure_ascii=False)
                    if m.get("reasoning_details"):
                        row.reasoning_json = json.dumps(
                            m["reasoning_details"], ensure_ascii=False)
                    elif m.get("reasoning"):
                        row.reasoning_json = json.dumps(
                            {"text": m["reasoning"]}, ensure_ascii=False)
                    last_assistant = row
                elif m["role"] == "tool":
                    row.tool_call_id = m.get("tool_call_id")
                s.add(row)
                seq += 1
            if error_message:
                # A pre-stream provider failure produces no assistant model
                # message. Create a UI-only marker so reloads retain the
                # failure; _history deliberately excludes an empty marker.
                if last_assistant is None:
                    last_assistant = ChatMessage(
                        conv_id=conv_id, seq=seq, role="assistant",
                        content=None, model_content=None,
                        anonymized=(1 if anonymized else 0),
                        mapping_version=mapping_version)
                    s.add(last_assistant)
                    seq += 1
                last_assistant.error = str(error_message)[:4000]
            if last_assistant is not None and (done_event is not None
                                               or error_message):
                # valori esterni (OpenRouter) dentro colonne a lunghezza
                # fissa: si tagliano, sono solo informativi in UI
                last_assistant.finish_reason = (
                    ((done_event or {}).get("finish_reason")
                     or ("error" if error_message else "")))[:32] or None
                if done_event is not None:
                    last_assistant.model = (
                        done_event.get("model") or "")[:128] or None
                    last_assistant.usage_json = json.dumps(
                        done_event.get("usage") or {})
            if last_assistant is not None and artifact_ids:
                s.flush()               # serve l'id del messaggio appena creato
                (s.query(Attachment)
                 .filter(Attachment.id.in_(list(artifact_ids)))
                 .update({"message_id": last_assistant.id},
                         synchronize_session=False))
            conv.updated_at = _now()
            s.commit()
    except Exception as e:
        log.error(f"salvataggio del turno {conv_id} fallito: {e!r}")


def _progress_throttle(emit, min_interval=0.25):
    """Gli engine ticcano a ogni blocco di testo: verso il browser basta un
    aggiornamento ogni tanto, purché i cambi di pezzo o di fase passino
    sempre (sono quelli che raccontano la storia)."""
    state = {"t": 0.0, "key": None}

    def on_progress(snap):
        now = time.monotonic()
        key = (snap.get("index"), snap.get("stage"), snap.get("phase"))
        if key == state["key"] and now - state["t"] < min_interval:
            return
        state["t"], state["key"] = now, key
        emit(snap)

    return on_progress


def _decode_str(text, mapping):
    return chat_anon.decode_for_display(str(text or ""), mapping)[0]


def _decode_web_event(ev, mapping, scope):
    """Copia di un evento tool web coi campi testuali decodificati per il
    DISPLAY: l'utente vede la query REALE che è partita (l'egress diventa
    ispezionabile ricerca per ricerca) e i risultati coi valori veri. La
    forma persistita resta canonica coi tag — stessa regola del testo
    assistant: qui si decodifica solo ciò che va sullo schermo.

    Gli URL non passano dalla mappa (il canonico "Mario Rossi" ha lo spazio,
    l'URL vero no): si mostra l'originale ricordato dal server
    (or_tools.real_url), che è anche l'unico link cliccabile sensato."""
    def decode(key, value):
        if not isinstance(value, str) or not value:
            return value
        if key == "url":
            real = or_tools.real_url(scope, value)
            if real is not None:
                return real
        return _decode_str(value, mapping)

    out = dict(ev)
    if out.get("args"):
        out["display_args"] = {k: decode(k, v)
                               for k, v in out["args"].items()}
    result = out.get("result")
    if isinstance(result, dict):
        r = dict(result)
        for key in ("url", "title", "content", "stderr", "notice"):
            if isinstance(r.get(key), str) and r[key]:
                r[key] = decode(key, r[key])
        if isinstance(r.get("results"), list):
            r["results"] = [
                {k: decode(k, v) for k, v in row.items()}
                if isinstance(row, dict) else row
                for row in r["results"]]
        out["result"] = r
    return out


def _egress_check(session, conv_id, model_content, attachments, exclude=None):
    """L'unico controllo di uscita, sui contenuti che partono ADESSO.

    Difesa in profondità: non sostituisce il rilevatore, ma
    intercetta un errore di cablaggio o la ricomparsa di un valore già noto.
    Si guardano solo i pezzi nuovi del turno: i file dei turni precedenti sono
    partiti con il registro di allora e ricontrollarli vorrebbe dire bloccare
    la conversazione per una superficie diventata nota dopo.

    `exclude` = le categorie che questa conversazione ha scelto di lasciare in
    chiaro: i loro valori NON sono fughe, sono la scelta dell'utente. Il PATCH
    delle categorie è rifiutato a turno in corso, quindi qui si rileggono le
    stesse opzioni con cui l'anonimizzazione ha appena lavorato."""
    leaks = chat_anon.known_surface_leaks(session, conv_id, model_content,
                                          exclude=exclude)
    if leaks:
        raise chat_anon.TurnAnonymizationError(
            "Controllo di uscita fallito: il messaggio anonimizzato contiene "
            f"ancora {_leak_list(leaks)}. Non è stato inviato niente.")
    for att in attachments:
        if att.anonymization_status != "protected":
            continue
        if not att.protected_path or (chat_anon.attachment_path(att)
                                      != Path(att.protected_path)):
            raise chat_anon.TurnAnonymizationError(
                f"Controllo di uscita fallito: {att.display_filename or att.filename} "
                "risulta protetto ma punta all'originale.", att.id)
        path = Path(att.protected_path)
        if path.suffix.lower() == ".xlsx":
            from ..engine.xlsx import known_xlsx_leaks
            mapping = chat_anon.conversation_mapping(
                session, conv_id, include_aliases=True, exclude=exclude)
            try:
                file_leaks = known_xlsx_leaks(path.read_bytes(), mapping)
            except Exception as exc:
                raise chat_anon.TurnAnonymizationError(
                    "Cannot verify the complete Excel workbook; nothing was sent.",
                    att.id) from exc
            if file_leaks:
                raise chat_anon.TurnAnonymizationError(
                    "Excel output check failed: the workbook still contains "
                    f"{_leak_list(file_leaks)}. Nothing was sent.", att.id)
        card = chat_anon.attachment_model_briefing(att)
        leaks = chat_anon.known_surface_leaks(
            session, conv_id, json.dumps(card or {}, ensure_ascii=False),
            exclude=exclude)
        if leaks:
            raise chat_anon.TurnAnonymizationError(
                "Controllo di uscita fallito: la scheda di "
                f"{att.display_filename or att.filename} contiene ancora "
                f"{_leak_list(leaks)}.", att.id)


@router.post("/api/chats/{conv_id}/messages")
async def send_message(conv_id: str, body: MessageIn,
                       user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    """Invia un messaggio e streamma il turno come SSE (via fetch POST, non
    EventSource). In una conversazione anonimizzata lo stream si apre PRIMA
    dell'anonimizzazione e la racconta mentre accade:

        anon_start → anon_progress* → anon_done → start → (turno del modello)

    Anonimizzare qui, e non all'upload, è il punto della versione: quando si
    redige, il registro contiene già tutto il turno — tutti i file e il
    messaggio — quindi nessun file può essere protetto con un registro
    incompleto e non esistono contenuti dello stesso turno su versioni diverse.

    Gli altri eventi sono quelli di chat.run_turn, più:
      - tool_result arricchito con "attachments" (artifact scaricabili);
      - ": ping" ogni 15 s nei silenzi (esecuzioni sandbox lunghe).
    Un errore con "fatal": true dice al browser che NIENTE è stato inviato
    (il testo scritto va restituito all'utente, non perso).
    Il turno gira in un task in background e la risposta ne rigioca gli
    eventi da un buffer: chiudere la richiesta (refresh della pagina) NON lo
    ferma — ci si riaggancia con GET /messages/live, si ferma con POST
    /stop."""
    conv = _get_conv(conv_id, user, session)
    if user.openrouter_key is None and provisioning.configured():
        # utente creato mentre OpenRouter era giù: la chiave personale si
        # recupera qui, al primo bisogno
        try:
            await provisioning.ensure_user_key(session, user)
        except or_client.OpenRouterError as exc:
            log.warning("OpenRouter key provisioning failed user_id=%s: %s",
                        user.id, _log_text(exc))
    api_key = provisioning.key_for(user)
    if api_key is None:
        raise ApiError(409, "user_key_missing",
                       "Chiave OpenRouter non configurata per il "
                       "tuo utente: chiedi all'amministratore.")
    if conv.id in _running:
        raise ApiError(409, "turn_running",
                       "C'è già una risposta in corso in questa "
                       "conversazione: fermala o attendi.")
    content = (body.content or "").strip()
    if not content:
        raise ApiError(422, "message_empty", "Messaggio vuoto.")
    if body.model is not None:
        conv.model = body.model.strip()[:128]
    if body.options is not None:
        _guard_privacy_option(body.options,
                              json.loads(conv.options_json or "{}"), session)
        conv.options_json = json.dumps(body.options, ensure_ascii=False)
    if not conv.model:
        raise ApiError(422, "model_missing",
                       "Scegli un modello prima di inviare.")
    # si controlla il modello della CONVERSAZIONE, non solo quello appena
    # mandato: una chat aperta prima che l'admin restringesse la lista si
    # ferma qui con un messaggio chiaro, non prosegue su un modello proibito
    await _check_model_allowed(user, session, conv.model)
    anonymized = bool(conv.anonymized)
    if (not anonymized
            and settings_store.chat_anonymization_policy(session) == "required"):
        raise ApiError(
            409, "anon_required_conv_blocked",
            "L'amministratore ha reso obbligatoria l'anonimizzazione "
            "delle chat: questa conversazione in chiaro non può proseguire. "
            "Creane una nuova, che nascerà anonimizzata.")
    if (body.preview or body.from_staged) and not anonymized:
        raise ApiError(422, "anon_only_preview",
                       "L'anteprima dell'anonimizzazione esiste "
                       "solo nelle conversazioni anonimizzate.")
    session.commit()

    pending_atts = (session.query(Attachment)
                    .filter_by(conv_id=conv.id, direction="in", message_id=None)
                    .order_by(Attachment.created_at).all())
    # fotografia: un upload che arrivasse durante l'anonimizzazione non è
    # parte di questo turno e non deve finire attaccato a questo messaggio
    pending = [{"id": a.id, "filename": a.display_filename or a.filename}
               for a in pending_atts]
    pending_ids = [a["id"] for a in pending]
    if any(jobs.doc_busy(i) for i in pending_ids):
        # rielaborazione con OCR in corso su un allegato di questo turno: il
        # worker sta riscrivendo proprio i file che partirebbero adesso
        raise ApiError(409, "ocr_running_send",
                       "C'è una rielaborazione con OCR in corso su "
                       "un allegato di questo invio: attendi che "
                       "finisca, o annullala, prima di inviare.")

    staged = chat_staging.state(conv.id) if body.from_staged else None
    if body.from_staged and (staged is None
                             or staged["content"] != content
                             or staged["att_ids"] != pending_ids):
        # l'anteprima non corrisponde più a quello che si sta inviando
        # (riavvio del server, allegati cambiati, testo cambiato)
        chat_staging.discard(conv.id)
        raise ApiError(409, "preview_stale",
                       "L'anteprima non è più valida: "
                       "ripeti l'invio per rigenerarla.")
    options = json.loads(conv.options_json or "{}")
    model = conv.model
    title_from = content[:80] if _n_messages(session, conv.id) == 0 else None

    cancel = asyncio.Event()
    _running[conv.id] = cancel
    loop = asyncio.get_running_loop()

    async def stream():
        or_messages, base_len = None, 0
        done_event, artifact_ids = None, []
        error_message = None
        mapping_version = 0
        persisted = False
        try:
            # l'evento di apertura porta il testo del messaggio: serve al
            # browser che si RIAGGANCIA a un turno in corso (dopo un refresh)
            # per ricostruire la bolla utente, che a quel punto potrebbe non
            # essere ancora persistita
            yield _sse({"type": "turn", "content": content,
                        "anonymized": anonymized,
                        "preview": bool(body.preview)})
            # --- 1. anonimizzazione di tutto il turno ---------------------
            model_content = content
            if anonymized and body.from_staged:
                # turno già anonimizzato (e rivisto) nell'anteprima: si
                # riparte da lì, senza rifare niente. Il controllo di uscita
                # al passo 2 vale comunque, sui contenuti che partono ADESSO.
                model_content = staged["model_content"]
                mapping_version = staged["mapping_version"]
            elif anonymized and body.preview:
                yield _sse({"type": "anon_start", "files": pending,
                            "total": len(pending) + 1})
                queue = asyncio.Queue()

                async def stage():
                    try:
                        return await asyncio.to_thread(
                            chat_staging.stage_turn, conv_id, content,
                            pending_ids, cancel,
                            _progress_throttle(
                                lambda snap: loop.call_soon_threadsafe(
                                    queue.put_nowait, snap)),
                            ocr=body.ocr)
                    finally:
                        queue.put_nowait(None)

                task = asyncio.create_task(stage())
                while True:
                    snap = await queue.get()
                    if snap is None:
                        break
                    yield _sse({"type": "anon_progress", **snap})
                try:
                    await task
                except chat_anon.TurnAnonymizationError as exc:
                    yield _sse({"type": "error", "fatal": True,
                                "attachment_id": exc.attachment_id,
                                "message": ("Invio annullato durante "
                                            "l'anonimizzazione: non è partito "
                                            "niente." if exc.canceled
                                            else str(exc))})
                    return
                # niente parte: il browser apre l'anteprima e l'invio vero
                # arriverà (se arriverà) con from_staged=True
                yield _sse({"type": "staged",
                            **chat_staging.descriptor(conv_id)})
                return
            elif anonymized:
                # un'anteprima preparata e mai inviata a questo punto è
                # stantia: si riparte dall'anonimizzazione piena
                chat_staging.discard(conv_id)
                yield _sse({"type": "anon_start", "files": pending,
                            "total": len(pending) + 1})
                queue = asyncio.Queue()

                async def anonymize():
                    try:
                        return await asyncio.to_thread(
                            chat_anon.anonymize_turn, conv_id, content,
                            pending_ids, cancel,
                            _progress_throttle(
                                lambda snap: loop.call_soon_threadsafe(
                                    queue.put_nowait, snap)),
                            ocr=body.ocr)
                    finally:
                        queue.put_nowait(None)

                task = asyncio.create_task(anonymize())
                while True:
                    snap = await queue.get()
                    if snap is None:
                        break
                    yield _sse({"type": "anon_progress", **snap})
                try:
                    model_content, mapping_version, atts = await task
                except chat_anon.TurnAnonymizationError as exc:
                    yield _sse({"type": "error", "fatal": True,
                                "attachment_id": exc.attachment_id,
                                "message": ("Invio annullato durante "
                                            "l'anonimizzazione: non è partito "
                                            "niente." if exc.canceled
                                            else str(exc))})
                    return
                yield _sse({"type": "anon_done", "attachments": atts,
                            "mapping_version": mapping_version})

            # --- 1b. fusioni da confermare PRIMA di partire ---------------
            # In una chat libera il registro nasce qui: se propone fusioni, la
            # domanda va fatta ADESSO. La scelta entra nel system prompt di
            # QUESTO turno (merge_notes, passo 3), mentre chiederla dopo
            # l'invio vorrebbe dire che il modello ha già visto i due tag
            # separati senza sapere che sono la stessa entità.
            # Niente viene ri-redatto: i file protetti sono quelli, cambia solo
            # la mappa. Dall'anteprima pre-invio (from_staged) la domanda è
            # già stata fatta nell'ultimo step del modal: qui non si ripete.
            if anonymized and not body.from_staged:
                with db.SessionLocal() as s:
                    conv_row = s.get(Conversation, conv_id)
                    suggestions = (chat_anon.merge_suggestions(s,
                                                               _scope(conv_row))
                                   if conv_row is not None else [])
                if suggestions:
                    gate = asyncio.Event()
                    _merge_gates[conv_id] = gate
                    yield _sse({"type": "merge_check",
                                "suggestions": suggestions})
                    try:
                        waited = 0
                        while True:
                            if cancel.is_set():
                                yield _sse({"type": "error", "fatal": True,
                                            "message": "Invio annullato: non "
                                                       "è partito niente."})
                                return
                            if gate.is_set():
                                break
                            try:
                                await asyncio.wait_for(gate.wait(), timeout=15)
                            except asyncio.TimeoutError:
                                waited += 15
                                if waited >= _MERGE_WAIT_S:
                                    yield _sse({
                                        "type": "error", "fatal": True,
                                        "message": "Fusioni non confermate: "
                                                   "l'invio è stato annullato "
                                                   "e non è partito niente. "
                                                   "Ripeti l'invio."})
                                    return
                                yield ": ping\n\n"   # keep-alive dell'attesa
                    finally:
                        _merge_gates.pop(conv_id, None)

            # --- 2. controllo di uscita + persistenza del messaggio -------
            with db.SessionLocal() as s:
                conv_row = s.get(Conversation, conv_id)
                if conv_row is None:
                    yield _sse({"type": "error", "fatal": True,
                                "message": "Conversazione eliminata."})
                    return
                scope = _scope(conv_row)
                project_row = (s.get(Project, conv_row.project_id)
                               if conv_row.project_id else None)
                atts_now = [a for a in (s.get(Attachment, i)
                                        for i in pending_ids) if a is not None]
                if anonymized:
                    try:
                        _egress_check(
                            s, scope, model_content, atts_now,
                            chat_anon.excluded_groups(
                                chat_anon.anon_options(s, conv_row)
                                ["excluded_tags"]))
                    except chat_anon.TurnAnonymizationError as exc:
                        yield _sse({"type": "error", "fatal": True,
                                    "attachment_id": exc.attachment_id,
                                    "message": str(exc)})
                        return
                files = {}
                if project_row is not None:
                    # i file CONFERMATI del progetto, sempre nel dizionario
                    # della sandbox (protetti nei progetti anonimizzati)
                    for name, path in project_files.chat_files(
                            s, project_row).items():
                        if not path.is_file():
                            yield _sse({"type": "error", "fatal": True,
                                        "message": f"{name} (file di "
                                        "progetto) non è più disponibile "
                                        "sul server."})
                            return
                        files[name] = str(path)
                for att in atts_now + [
                        a for a in s.query(Attachment)
                        .filter_by(conv_id=conv_id, direction="in")
                        .filter(Attachment.message_id.isnot(None))]:
                    path = chat_anon.attachment_path(att)
                    name = chat_anon.attachment_model_name(att)
                    if path is None or not path.is_file():
                        yield _sse({"type": "error", "fatal": True,
                                    "message": f"{name} non è più "
                                    "disponibile sul server."})
                        return
                    files[name] = str(path)
                if title_from:
                    conv_row.title = title_from
                user_msg = ChatMessage(
                    conv_id=conv_id, seq=_next_seq(s, conv_id), role="user",
                    content=model_content, display_content=content,
                    model_content=model_content,
                    anonymized=(1 if anonymized else 0),
                    mapping_version=mapping_version)
                s.add(user_msg)
                s.flush()
                for att in atts_now:
                    att.message_id = user_msg.id
                conv_row.updated_at = _now()
                s.commit()
                persisted = True    # da qui l'errore non è più "fatale":
                                    # il messaggio è nel thread
                if body.from_staged:
                    # l'anteprima ha fatto il suo lavoro: via lo stato e i
                    # PDF (i file protetti restano, sono il turno)
                    chat_staging.discard(conv_id)

                # --- 3. il turno del modello -----------------------------
                entry = None
                try:
                    cat = await catalog.get_models(api_key=api_key)
                    entry = catalog.find(cat["models"], model)
                except or_client.OpenRouterError as exc:
                    log.warning(
                        "OpenRouter catalog lookup failed conversation=%s "
                        "model=%s: %s", conv_id, model, _log_text(exc))
                    pass        # in dubbio si dichiara il tool: un eventuale
                                # rifiuto arriva come evento error
                if entry is not None and not entry.get("tools"):
                    declared = []       # il modello non sa usare strumenti
                else:
                    # i tool disponibili ADESSO (execute_python solo se
                    # Docker c'è: promettere la sandbox al modello lo
                    # farebbe solo sbagliare). I tool web ci sono solo se
                    # l'admin ha abilitato la feature E la conversazione non
                    # l'ha spenta (attiva di default): due livelli, come per
                    # la deroga ZDR.
                    web_on = bool(
                        settings_store.get_int(s, "chat_web_search")
                        and options.get("web_search", True))
                    declared = or_tools.available_specs(web=web_on)
                tools = [spec.schema(anonymized) for spec in declared]
                modalities = set((entry or {}).get("input_modalities")
                                 or ["text"])
                provider_prefs = _provider_prefs(options)
                if or_chat.file_input_refused(model, provider_prefs):
                    # il catalogo dice "file" ma l'endpoint scelto dalle
                    # regole privacy lo ha già rifiutato (chat.py): i PDF
                    # restano nella sandbox, non si riprova a ogni turno
                    modalities.discard("file")
                reasoning, params = catalog.sanitize_options(options, entry)
                cents = settings_store.get_int(s, "chat_turn_cost_limit_cents")
                attach_mb = settings_store.get_int(s, "chat_model_attach_mb")
                history = _history(s, conv_id, modalities,
                                   attach_mb * 1024 * 1024)
                # le schede dei file di progetto stanno nel SYSTEM prompt e
                # i file stessi (immagini e PDF, stesse regole degli
                # allegati) in un messaggio utente sintetico subito dopo:
                # entrambi cambiano solo quando cambia l'elenco dei file
                # confermati, quindi il prompt caching paga una volta per
                # modifica
                project_block, project_msgs = None, []
                if project_row is not None:
                    p_parts, p_shown = project_files.model_parts(
                        s, project_row, modalities, attach_mb * 1024 * 1024)
                    project_block = project_files.files_block(
                        s, project_row, p_shown)
                    if p_parts:
                        project_msgs = [{
                            "role": "user",
                            "content": [{"type": "text",
                                         "text": "Copie dei file di progetto "
                                                 "(schede nel messaggio di "
                                                 "sistema): puoi guardarle "
                                                 "direttamente."}] + p_parts}]
                system = or_chat.system_prompt(
                    tool_names=[spec.name for spec in declared],
                    anonymized=anonymized,
                    project_block=project_block,
                    merge_block=(chat_anon.merge_notes(s, scope)
                                 if anonymized else None))
                or_messages = ([{"role": "system", "content": system}]
                               + project_msgs + history)
                base_len = len(or_messages)
                mapping = chat_anon.conversation_mapping(s, scope)
                user_descriptor = user_msg.descriptor(
                    mapping, chat_anon.decode_for_display)

            done_event = None
            canonical_text = []
            media = chat_anon.MediaPool()   # immagini ripristinabili del turno
            sources = {}        # (estensione, nome): file da cui rigenerare
            registered = {}     # percorso sandbox -> attachment id del turno
            decoder = chat_anon.StreamDecoder(mapping)
            flushed = False
            events = asyncio.Queue()

            def text_event(pair):
                text, entities = pair
                if not text and not entities:
                    return None
                event = {"type": "text", "delta": text,
                         "entities": entities}
                code_values = decoder.take_protected()
                if code_values:
                    event["code_values"] = code_values
                return event

            def flush_event():
                nonlocal flushed
                if flushed:
                    return None
                flushed = True
                return text_event(decoder.flush())

            async def pump():
                try:
                    async for ev in or_chat.run_turn(
                            conv_id, or_messages, model, api_key,
                            files=files or None, tools=tools,
                            reasoning=reasoning, params=params,
                            provider=provider_prefs,
                            cost_limit=(cents / 100.0) if cents else None,
                            anonymized=anonymized, scope=scope,
                            cancel=cancel):
                        await events.put(ev)
                except Exception as e:      # difensivo: mai stream troncato muto
                    # il tipo serve: parecchie eccezioni di trasporto hanno il
                    # messaggio vuoto, e "Errore interno: " da solo non dice
                    # niente né all'utente né a chi legge il log
                    log.exception(f"turno {conv_id}")
                    await events.put({
                        "type": "error",
                        "message": f"Errore interno: {type(e).__name__}"
                                   + (f": {e}" if str(e) else "")})
                finally:
                    await events.put(None)

            task = asyncio.create_task(pump())
            try:
                yield _sse({"type": "start", "user_message": user_descriptor})
                while True:
                    try:
                        ev = await asyncio.wait_for(events.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"      # sandbox al lavoro: keep-alive
                        continue
                    if ev is None:
                        shown = flush_event()
                        if shown is not None:
                            yield _sse(shown)
                        break
                    if ev["type"] == "error":
                        error_message = _error_text(ev)
                        if ev.get("source") == "openrouter":
                            log.warning(
                                "OpenRouter turn failed conversation=%s "
                                "model=%s status=%s code=%s: %s",
                                conv_id, model, ev.get("status"),
                                ev.get("code"), _log_text(error_message))
                    if ev["type"] == "text":
                        # La forma canonica coi TAG resta in or_messages/history;
                        # verso il browser esce la parte già decodificabile
                        # (vedi StreamDecoder: TAG spezzati e regioni markdown
                        # aperte restano in coda finché non sono definitive).
                        delta = ev.get("delta") or ""
                        canonical_text.append(delta)
                        shown = text_event(decoder.feed(delta))
                        if shown is not None:
                            yield _sse(shown)
                        continue
                    if anonymized and ev.get("kind") in ("search", "page"):
                        if ev["type"] == "tool_result":
                            # il handler web può aver registrato entità
                            # NUOVE (mapping_version avanza a metà turno):
                            # mappa ed eventi successivi — decoder del testo
                            # compreso — devono vedere i segnaposto appena nati
                            with db.SessionLocal() as s2:
                                mapping = chat_anon.conversation_mapping(
                                    s2, scope)
                            decoder.mapping = mapping
                        if ev["type"] in ("tool_call", "tool_result"):
                            ev = _decode_web_event(ev, mapping, scope)
                    if ev["type"] == "tool_result":
                        # in un thread: qui dentro si copiano file, si ridisegna
                        # un grafico e si riconverte un PDF con LibreOffice —
                        # secondi di lavoro sincrono. Sul loop bloccherebbero
                        # sia il keep-alive dell'SSE sia la richiesta che il
                        # turno ha in volo verso OpenRouter (visto: ConnectTimeout
                        # a 15 s mentre il profilo LibreOffice si inizializzava).
                        ev = await asyncio.to_thread(
                            _register_artifacts, conv_id, ev,
                            anonymized=anonymized, mapping=mapping,
                            media=media, sources=sources,
                            registered=registered)
                        artifact_ids.extend(a["id"] for a in ev["attachments"]
                                            if a["id"] not in artifact_ids)
                    if ev["type"] == "done":
                        done_event = ev
                        if (ev.get("finish_reason") == "error"
                                and not error_message):
                            error_message = (
                                "OpenRouter ended the response with an error.")
                            log.warning(
                                "OpenRouter turn ended with finish_reason=error "
                                "conversation=%s model=%s", conv_id, model)
                        shown = flush_event()
                        if shown is not None:
                            yield _sse(shown)
                        code_values = chat_anon.protected_placeholders(
                            "".join(canonical_text), mapping)
                        if code_values:
                            ev = {**ev, "code_values": code_values}
                    yield _sse(ev)
            finally:
                task.cancel()               # no-op se il turno è finito
        except Exception as exc:            # noqa: BLE001
            # Uno stream troncato è un vicolo cieco muto: qualunque guasto
            # imprevisto deve arrivare all'utente come evento, non come
            # connessione che cade. `fatal` dice se il messaggio è stato
            # salvato (e quindi se il testo scritto va restituito).
            log.exception(f"turno {conv_id} interrotto: {exc!r}")
            error_message = (
                f"Turno interrotto da un errore interno: {exc}")[:4000]
            yield _sse({"type": "error", "fatal": not persisted,
                        "message": error_message})
            if persisted:
                done_event = {"type": "done", "finish_reason": "error"}
                yield _sse(done_event)
        finally:
            # prima il commit, POI lo sblocco di `busy` (_running): al primo
            # getChat con busy=False i messaggi del turno devono già esserci.
            # Con lo sblocco prima del commit c'era una finestra in cui la UI
            # ricaricava il thread senza la risposta appena conclusa —
            # invisibile su SQLite (commit sotto il microsecondo), concreta su
            # Postgres. Il try/finally annidato garantisce lo sblocco anche se
            # la persistenza fallisce.
            try:
                if or_messages is not None:
                    _persist_turn(conv_id, or_messages[base_len:], done_event,
                                  artifact_ids, anonymized=anonymized,
                                  mapping_version=mapping_version,
                                  error_message=error_message)
            finally:
                _running.pop(conv_id, None)
                _merge_gates.pop(conv_id, None)

    # il turno gira in un task suo e la risposta è solo un lettore del
    # buffer: chiudere la richiesta (refresh, cambio pagina) non lo ferma —
    # si ferma con POST /stop. Il riferimento al task sta nel buffer, così
    # il garbage collector non può portarselo via a metà lavoro.
    buf = _turn_buffer(conv.id)
    buf["task"] = asyncio.create_task(_pump_turn(stream(), buf))
    return StreamingResponse(_follow_turn(buf), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/api/chats/{conv_id}/messages/live")
def attach_messages(conv_id: str, user: User = Depends(current_user),
                    session: Session = Depends(get_session)):
    """Il riaggancio dopo un refresh (o un cambio di pagina): rigioca
    DALL'INIZIO gli eventi del turno in corso — così il browser ricostruisce
    esattamente quello che mostrava: bolla utente, avanzamento
    dell'anonimizzazione, dialog delle fusioni, testo parziale — e poi segue
    in diretta fino al done. Vale anche per il turno appena concluso (il
    buffer resta finché non ne parte un altro): copre la corsa tra il
    getChat che dice `busy` e il turno che finisce un attimo dopo.
    404 se non c'è niente da rigiocare (nessun turno da quando il server è
    su): il browser si affida allo stato persistito di getChat."""
    conv = _get_conv(conv_id, user, session)
    buf = _turn_streams.get(conv.id)
    if buf is None:
        raise ApiError(404, "no_live_turn",
                       "Nessun turno da rigiocare in questa conversazione.")
    return StreamingResponse(_follow_turn(buf), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/api/chats/{conv_id}/messages/continue")
async def continue_message(conv_id: str, user: User = Depends(current_user),
                           session: Session = Depends(get_session)):
    """Ripresa dopo la revisione delle fusioni (`chat.merge.continue`): il turno
    fermo al passo 1b riprende da dove si era interrotto. Le fusioni sono già state applicate
    dagli endpoint del registro, qui non si tocca niente. `async` di proposito:
    l'Event del turno vive sul loop e non si alza da un thread."""
    _get_conv(conv_id, user, session)
    gate = _merge_gates.get(conv_id)
    if gate is None:
        raise ApiError(409, "no_turn_awaiting",
                       "Nessun turno in attesa di conferma in questa "
                       "conversazione.")
    gate.set()
    return {"ok": True}


@router.post("/api/chats/{conv_id}/stop")
async def stop_message(conv_id: str, user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    """Il bottone di stop (`chat.composer.stop`): alza l'evento di annullamento
    del turno in corso. L'effetto arriva al primo checkpoint del loop (durante
    un'esecuzione sandbox lunga può voler dire qualche secondo). Il turno gira
    in un task di background che sopravvive alla chiusura della connessione,
    quindi questo è l'UNICO modo di fermarlo. `async` di proposito: il gate delle fusioni è un Event del loop e
    va alzato dal loop (sveglia subito l'attesa, che vede cancel e chiude)."""
    conv = _get_conv(conv_id, user, session)
    cancel = _running.get(conv.id)
    if cancel is None:
        return {"stopping": False}
    cancel.set()
    gate = _merge_gates.get(conv.id)
    if gate is not None:
        gate.set()
    return {"stopping": True}


# --- Anteprima pre-invio del turno anonimizzato (chat_staging) ---------------
# Il visualizzatore è lo STESSO dei file di progetto: questi endpoint ne
# rispecchiano il contratto (pagine PNG, estrazione da rettangolo, modifiche
# alla mappa), ma lavorano sul turno in anteprima e sul registro della
# conversazione invece che su un Document.

class StagedDeanonymizeIn(BaseModel):
    placeholder: str | None = None      # "[FULLNAME_1]": lascia in chiaro questo
    label: str | None = None            # "FULLNAME": tutta la categoria del turno


class StagedAnonymizeTextIn(BaseModel):
    text: str
    save_term: bool = False              # anche nei termini fissi (default admin)
    term_tag: str | None = None          # tag del termine fisso (vuoto = CUSTOM)


class StagedExtractIn(BaseModel):
    source: str                          # original | anonymized
    page: int
    rect: list[float]                    # [x0, y0, x1, y1] in punti PDF


class StagedSealIn(BaseModel):
    page: int                            # pagina (0-based) su cui è disegnato
    rect: list[float]                    # [x0, y0, x1, y1] in punti PDF
    all_pages: bool = False              # replica sulla stessa posizione ovunque


class StagedColumnIn(BaseModel):
    sheet: str
    column: str


def _no_turn_running(conv):
    if conv.id in _running:
        raise ApiError(409, "conv_job_running",
                       "C'è un'elaborazione in corso in questa "
                       "conversazione: attendi che finisca.")


def _no_job_on_turn(conv_id):
    """Nessuna rielaborazione con OCR in coda o in corso su un pezzo di questo
    turno: ri-redigere l'anteprima mentre il worker la sta riscrivendo darebbe
    uno stato incoerente (è la stessa guardia dei file di progetto, dove il
    job blocca il file con doc_busy)."""
    st = chat_staging.state(conv_id)
    for item in (st or {}).get("items", ()):
        if item.get("att_id") and jobs.doc_busy(item["att_id"]):
            raise ApiError(
                409, "ocr_running_edit",
                "C'è una rielaborazione con OCR in corso su un allegato "
                "di questo invio: attendi che finisca, o annullala, "
                "prima di fare modifiche.")


@router.get("/api/chats/{conv_id}/staged")
def get_staged(conv_id: str, user: User = Depends(current_user),
               session: Session = Depends(get_session)):
    """Lo stato dell'anteprima com'è adesso. Serve al frontend per riprendere
    il modal dopo un'elaborazione asincrona (la rielaborazione con OCR di un
    allegato), che riscrive il turno da un thread worker."""
    conv = _get_conv(conv_id, user, session)
    desc = chat_staging.descriptor(conv.id)
    if desc is None:
        raise ApiError(404, "no_preview",
                       "Nessuna anteprima in corso per questa "
                       "conversazione.")
    return desc


@router.delete("/api/chats/{conv_id}/staged")
def discard_staged(conv_id: str, user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    """L'utente chiude l'anteprima senza inviare: via stato e PDF. I file
    protetti degli allegati restano (il prossimo invio li riscrive)."""
    conv = _get_conv(conv_id, user, session)
    chat_staging.discard(conv.id)
    return {"ok": True}


@router.get("/api/chats/{conv_id}/staged/{item_id}/pages/{source}/{n}.png")
def staged_page(conv_id: str, item_id: str, source: str, n: int,
                user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """Una pagina dell'anteprima pre-invio, renderizzata in PNG.

    `source` è `original` o `anonymized`: sono i due lati che il
    visualizzatore mostra affiancati. L'anteprima vive in RAM e muore con
    l'invio o con lo scarto: da lì in poi la pagina risponde 404
    `page_unavailable_preview`."""
    if source not in ("original", "anonymized"):
        raise ApiError(404, "unknown_source", "Sorgente sconosciuta.")
    conv = _get_conv(conv_id, user, session)
    entry = chat_staging.page_png(conv.id, item_id, source, n)
    if entry is None:
        raise ApiError(404, "page_unavailable_preview",
                       "Pagina non disponibile: l'anteprima non "
                       "esiste più (ripeti l'invio).")
    resp = Response(content=entry["png"], media_type="image/png")
    # MAI in cache nel browser: l'URL di un pezzo dell'anteprima è lo stesso a
    # ogni turno (stessa conversazione, item "prompt", rev che riparte da 0 a
    # ogni preparazione), quindi una risposta cacheabile farebbe rivedere dal
    # secondo turno in poi le pagine del PRIMO messaggio. Il costo è nullo:
    # il rendering resta in cache lato server (png_cache).
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@router.post("/api/chats/{conv_id}/staged/{item_id}/extract")
def staged_extract(conv_id: str, item_id: str, body: StagedExtractIn,
                   user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    """Testo sotto un rettangolo dell'anteprima (selezione manuale), con
    fallback OCR dove il layer testuale non dà nulla."""
    conv = _get_conv(conv_id, user, session)
    if len(body.rect) != 4:
        raise ApiError(422, "rect_invalid", "Rettangolo non valido.")
    if body.source not in ("original", "anonymized"):
        raise ApiError(404, "unknown_source", "Sorgente sconosciuta.")
    try:
        return chat_staging.extract(conv.id, item_id, body.source,
                                    body.page, body.rect)
    except chat_staging.StagedEditError as e:
        raise from_internal(e)


@router.post("/api/chats/{conv_id}/staged/{item_id}/seal-area")
def staged_seal_area(conv_id: str, item_id: str, body: StagedSealIn,
                     user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    """SIGILLA un'area dell'anteprima anonimizzata di un allegato (solo
    PDF/immagini): il contenuto sotto il rettangolo viene rimosso dalla copia
    protetta che partirà verso il modello. Persiste sull'allegato (vale
    anche per invio diretto e reinvii). Risponde con lo stato staged completo."""
    conv = _get_conv(conv_id, user, session)
    _no_turn_running(conv)
    _no_job_on_turn(conv.id)
    try:
        return chat_staging.seal_area(conv.id, item_id, body.page, body.rect,
                                      body.all_pages)
    except chat_staging.StagedEditError as e:
        raise from_internal(e)


@router.delete("/api/chats/{conv_id}/staged/{item_id}/seal-area/{n}")
def staged_remove_seal(conv_id: str, item_id: str, n: int,
                       user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    """Rimuove un'area sigillata: la ri-redazione riparte dall'originale e il
    contenuto torna nella copia protetta."""
    conv = _get_conv(conv_id, user, session)
    _no_turn_running(conv)
    _no_job_on_turn(conv.id)
    try:
        return chat_staging.remove_seal(conv.id, item_id, n)
    except chat_staging.StagedEditError as e:
        raise from_internal(e)


@router.post("/api/chats/{conv_id}/staged/deanonymize")
def staged_deanonymize(conv_id: str, body: StagedDeanonymizeIn,
                       user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    """Lascia in chiaro un valore (o una categoria del turno): l'entità resta
    nel registro come `excluded` — i placeholder già partiti nei turni
    precedenti continuano a decodificarsi — e TUTTO il turno in anteprima
    viene ri-redatto. Risponde con lo stato staged completo."""
    conv = _get_conv(conv_id, user, session)
    _no_turn_running(conv)
    _no_job_on_turn(conv.id)
    try:
        return chat_staging.deanonymize(conv.id, body.placeholder, body.label)
    except chat_staging.StagedEditError as e:
        raise from_internal(e)


@router.post("/api/chats/{conv_id}/staged/anonymize-text")
def staged_anonymize_text(conv_id: str, body: StagedAnonymizeTextIn,
                          user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    """Anonimizza un testo selezionato nell'anteprima come [TAG_n] nel
    registro della conversazione (vale anche per i turni futuri) e ri-redige
    tutto il turno. Se il valore era stato deanonimizzato, lo RI-include. Il
    tag è quello scelto nel popup quando si salva anche il termine fisso,
    CUSTOM altrimenti: lo stesso tag nel registro e nei default."""
    conv = _get_conv(conv_id, user, session)
    _no_turn_running(conv)
    _no_job_on_turn(conv.id)
    # tag validato PRIMA della ri-redazione: un tag invalido deve dare 422
    # senza toccare il turno
    term_tag = "CUSTOM"
    if body.save_term:
        try:
            term_tag = settings_store.clean_tag(body.term_tag or "CUSTOM")
        except ValueError as e:
            raise from_internal(e)
    try:
        out = chat_staging.anonymize_text(conv.id, body.text, term_tag)
    except chat_staging.StagedEditError as e:
        raise from_internal(e)
    # solo a redazione riuscita; la soglia della chat (_searchable, >=4
    # alfanumerici) è più severa di clean_terms, quindi qui non fallisce.
    # Va sulla lista PERSONALE di chi clicca (vedi settings_store.
    # add_custom_term): il termine è già nel registro di questa chat, il
    # salvataggio riguarda i suoi documenti futuri.
    if body.save_term:
        out["saved_term"] = settings_store.add_custom_term(session, user,
                                                           body.text, term_tag)
    return out


# --- Colonne xlsx e rielaborazione OCR nell'anteprima ------------------------
# Gemelli degli endpoint dei file di progetto: nell'anteprima pre-invio si può
# fare quello che si fa sulla preview di un file di progetto. Le due differenze
# sono di scala, non di grammatica: la colonna è sincrona (ri-redige il turno
# come ogni altra modifica dell'anteprima) e la rielaborazione con OCR va in
# coda come i caricamenti (rilegge davvero il file, decine di secondi).

@router.get("/api/chats/{conv_id}/staged/{item_id}/columns")
def staged_columns(conv_id: str, item_id: str,
                   user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    """Le colonne cliccabili delle due anteprime di un allegato xlsx (vuoto
    sugli altri formati e sul messaggio)."""
    conv = _get_conv(conv_id, user, session)
    return chat_staging.columns_layout(conv.id, item_id)


@router.post("/api/chats/{conv_id}/staged/{item_id}/column-info")
def staged_column_info(conv_id: str, item_id: str, body: StagedColumnIn,
                       user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    """Conteggi di una colonna xlsx per il popover che precede la scelta:
    valori distinti, quanti sono trattabili e quanti sono già nel registro.

    Letti dal file intero, non dall'anteprima troncata: il numero che l'utente
    vede prima di anonimizzare una colonna deve essere quello vero."""
    conv = _get_conv(conv_id, user, session)
    try:
        return chat_staging.column_info(conv.id, item_id, body.sheet,
                                        body.column)
    except chat_staging.StagedEditError as e:
        raise from_internal(e)


@router.post("/api/chats/{conv_id}/staged/{item_id}/anonymize-column")
def staged_anonymize_column(conv_id: str, item_id: str, body: StagedColumnIn,
                            user: User = Depends(current_user),
                            session: Session = Depends(get_session)):
    """ADDITIVA: ogni valore distinto della colonna entra nel registro
    ([CUSTOM_n] ai nuovi, il suo segnaposto a chi c'è già) e tutto il turno
    viene ri-redatto. Risponde con lo stato staged completo."""
    conv = _get_conv(conv_id, user, session)
    _no_turn_running(conv)
    _no_job_on_turn(conv.id)
    if not re.fullmatch(r"[A-Za-z]{1,3}", body.column or ""):
        raise ApiError(422, "column_invalid", "Colonna non valida.")
    try:
        return chat_staging.anonymize_column(conv.id, item_id, body.sheet,
                                             body.column.upper())
    except chat_staging.StagedEditError as e:
        raise from_internal(e)


@router.post("/api/chats/{conv_id}/staged/{item_id}/deanonymize-column")
def staged_deanonymize_column(conv_id: str, item_id: str, body: StagedColumnIn,
                              user: User = Depends(current_user),
                              session: Session = Depends(get_session)):
    """I valori della colonna tornano in chiaro: le entità restano nel
    registro come `excluded` (i segnaposto già partiti si decodificano per
    sempre) e il turno viene ri-redatto."""
    conv = _get_conv(conv_id, user, session)
    _no_turn_running(conv)
    _no_job_on_turn(conv.id)
    if not re.fullmatch(r"[A-Za-z]{1,3}", body.column or ""):
        raise ApiError(422, "column_invalid", "Colonna non valida.")
    try:
        return chat_staging.deanonymize_column(conv.id, item_id, body.sheet,
                                               body.column.upper())
    except chat_staging.StagedEditError as e:
        raise from_internal(e)


@router.post("/api/chats/{conv_id}/staged/{item_id}/reprocess-ocr",
             status_code=202)
def staged_reprocess_ocr(conv_id: str, item_id: str,
                         user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
    """Rilegge un allegato dell'anteprima con l'OCR ATTIVO (job in coda, come
    i caricamenti di progetto) e ri-redige il turno. Additiva: il registro può
    solo imparare. È il rimedio per un allegato preparato con l'OCR spento,
    dove la selezione manuale legge testo dentro un'immagine ma la redazione
    non saprebbe coprirlo nei pixel."""
    conv = _get_conv(conv_id, user, session)
    _no_turn_running(conv)
    if not conv.anonymized:
        raise ApiError(422, "anon_only_ocr_chat",
                       "L'OCR esiste solo nelle conversazioni "
                       "anonimizzate.")
    if not image_ocr.available():
        raise ApiError(503, "ocr_unavailable",
                       "OCR non disponibile su questo server: "
                       "pacchetti rapidocr/onnxruntime non "
                       "installati.")
    st = chat_staging.state(conv.id)
    if st is None:
        raise ApiError(409, "no_preview_resend",
                       "Nessuna anteprima in corso per questa "
                       "conversazione: ripeti l'invio.")
    item = next((i for i in st["items"] if i["id"] == item_id), None)
    if item is None:
        raise ApiError(404, "preview_item_not_found",
                       "Anteprima non trovata: ripeti l'invio.")
    if item["kind"] != "attachment":
        raise ApiError(422, "ocr_attachments_only",
                       "La rielaborazione con OCR vale solo sugli "
                       "allegati dell'invio.")
    _no_job_on_turn(conv.id)
    if jobs.pending_count() >= settings_store.get_int(session, "max_queue"):
        raise ApiError(429, "queue_full",
                       "Coda di elaborazione piena: riprova tra "
                       "qualche minuto.")
    job = Job(owner_id=user.id, filename=item["filename"],
              kind="chat_reprocess", doc_id=item["att_id"])
    session.add(job)
    session.commit()                 # prima di submit: il worker legge la riga
    try:
        jobs.submit_chat_reprocess(job.id, user.id, conv.id, item_id,
                                   item["att_id"])
    except ValueError as e:
        job.status, job.error = "failed", str(e)
        session.commit()
        raise from_internal(e, 409)
    return job.descriptor(position=jobs.position(job))

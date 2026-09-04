"""Modelli SQLAlchemy: utenti (admin/standard), progetti e chat col REGISTRO
delle entità (la mappa placeholder->valore).

Il DB tiene SOLO ciò che serve dopo la sessione: il registro (è lo scopo
dell'app: decodificare in un secondo momento) e i metadati. I byte dei file
stanno su disco: data/projects/ (vedi project_files.py) e data/chats/.

DUE motori, scelti con DATABASE_URL (config.py): SQLite di default (zero
configurazione) o Postgres per installazioni multi-utente. Le differenze
vivono SOLO qui in init_db (creazione engine) e nelle migrazioni Alembic
(backend/alembic/, applicate all'avvio da db_migrate.py): modelli, query e
logica applicativa sono identici sui due dialetti — e devono restarlo.
"""

import datetime
import json
import os
import uuid

from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Index, Integer,
                        String, Text, UniqueConstraint, create_engine, event)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL, ensure_dirs
from .db_migrate import run_migrations

Base = declarative_base()


def _uuid():
    return uuid.uuid4().hex


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso_utc(dt):
    """Serializzazione ISO-8601 dei timestamp per il frontend, SEMPRE con
    l'offset UTC. I valori sono scritti in UTC (_now), ma SQLite li rilegge
    naive (il dialect scarta l'offset in scrittura) mentre Postgres li rilegge
    aware: senza questa normalizzazione i due motori manderebbero stringhe
    diverse e `new Date()` nel browser interpreterebbe quella senza offset
    come ora LOCALE, mostrando orari diversi a seconda del database."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.isoformat()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)   # pbkdf2$iter$salt$hash
    role = Column(String(16), nullable=False, default="standard")  # admin | standard
    created_at = Column(DateTime(timezone=True), default=_now)
    # Chiave OpenRouter PERSONALE (openrouter/provisioning.py): creata dal
    # backend con la management key, nominata blockingbear-<username>. Il testo
    # in chiaro esiste solo nella risposta di creazione, quindi va tenuto qui
    # (stesso perimetro di rischio dei file secret.key/openrouter.key: disco
    # locale del server); l'hash è l'id con cui si fanno PATCH/DELETE e il
    # join con le analytics. NULL = utente ancora senza chiave (management key
    # non configurata, o OpenRouter irraggiungibile alla creazione).
    openrouter_key = Column(Text)
    openrouter_key_hash = Column(String(128))
    # Modello predefinito PERSONALE della chat: la regola JSON di
    # openrouter/model_rules.py. NULL/"" = segui la regola dell'installazione
    # (settings_store.default_model_rule).
    chat_model_rule = Column(Text)
    # Termini da anonimizzare SEMPRE, PERSONALI di questo utente: stessa forma
    # di quelli globali dell'amministratore ([{"text","tag"}], vedi
    # settings_store.clean_terms). I due livelli non si sostituiscono, si
    # SOMMANO: la lista che vale davvero è l'unione (settings_store.
    # merge_terms), e nessuno può togliersi un termine deciso dall'admin.
    # NULL/"" = nessun termine personale.
    anon_terms_json = Column(Text)
    # Alzato dall'admin alla creazione dell'account («cambia password al primo
    # accesso»): finché è vero ogni endpoint autenticato risponde 403
    # password_change_required — tranne /api/auth/me e /api/auth/password — e
    # il frontend mostra solo la schermata di cambio password. Lo abbassa
    # POST /api/auth/password.
    must_change_password = Column(Boolean, nullable=False, default=False,
                                  server_default="0")
    # Lingua dell'INTERFACCIA scelta dall'utente ("it" | "en", vedi
    # settings_store.LANGS). Non ha niente a che vedere con la lingua dei
    # documenti: il rilevamento delle PII non cambia. NULL = mai scelta, vale
    # il default dell'installazione (italiano); la scelta fatta nel browser
    # prima di avere un profilo viene salvata qui al primo login.
    lang = Column(String(8))
    # Tutorial del primo accesso (frontend/src/onboarding/): vero quando
    # l'utente lo ha finito o saltato. Sta sul profilo e non nel browser per
    # lo stesso motivo di `lang`: la postazione può essere condivisa e il
    # tutorial va mostrato alla persona, una volta sola.
    tour_done = Column(Boolean, nullable=False, default=False,
                       server_default="0")


class Setting(Base):
    """Parametri di esercizio modificabili dal pannello admin. Il valore è
    TEXT: tipo, default e validazione stanno nel registro (settings_store.py),
    il DB tiene solo le chiavi che l'amministratore ha cambiato."""
    __tablename__ = "settings"
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)


class Job(Base):
    """Elaborazione asincrona di un upload. Lo STATO sta qui (sopravvive al
    riavvio); i bytes del file stanno solo in RAM (jobs.py): se il server si
    riavvia, i job non conclusi diventano `failed` e si ricarica il file."""
    __tablename__ = "jobs"
    id = Column(String(32), primary_key=True, default=_uuid)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    filename = Column(String(256), nullable=False)
    # project_upload    = anonimizzazione di un file caricato in un progetto;
    # project_column    = anonimizzazione manuale di una colonna di un file
    #                     xlsx già elaborato (vedi project_files.py);
    # project_reprocess = rilevamento rifatto con l'OCR attivo su quel file;
    # project_realign   = quel file ri-redatto col registro corrente (il file
    #                     era rimasto indietro: vedi project_files.stale_leaks);
    # chat_reprocess    = lo stesso, ma su un allegato del turno in anteprima
    #                     di una chat (doc_id = allegato, vedi chat_staging.py)
    kind = Column(String(16), nullable=False, default="project_upload")
    status = Column(String(16), nullable=False, default="queued")  # queued | processing | done | failed | canceled
    error = Column(Text)
    doc_id = Column(String(32))            # id del ProjectFile: valorizzato a
                                           # status=done (upload) o dal submit
                                           # (column)
    project_id = Column(String(32))        # il progetto a cui tornare in UI
    created_at = Column(DateTime(timezone=True), default=_now)
    finished_at = Column(DateTime(timezone=True))

    def descriptor(self, position=None):
        d = {
            "id": self.id,
            "filename": self.filename,
            "kind": self.kind or "upload",
            "status": self.status,
            "error": self.error,
            "doc_id": self.doc_id,
            "project_id": self.project_id,
            "created_at": iso_utc(self.created_at),
        }
        if position is not None:
            d["position"] = position
        return d


class Project(Base):
    """Un PROGETTO: contenitore di file pre-caricati + N chat che li vedono
    tutte. Il modo (anonimizzato / in chiaro) si sceglie alla creazione e vale
    per file e chat: dentro un progetto non esistono modi misti.

    Il progetto è il PORTATORE DEL REGISTRO delle sue chat: le righe
    ConversationEntity/Alias di un progetto hanno conv_id = project.id (la
    colonna è uno scope id) e `mapping_version` è il contatore monotono
    condiviso — così lo stesso valore ha lo stesso placeholder in tutti i
    file e in tutte le chat del progetto, e la semantica per-versione
    (snapshot sui messaggi, max_version in decodifica) resta identica a
    quella delle chat singole."""
    __tablename__ = "projects"
    id = Column(String(32), primary_key=True, default=_uuid)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(200), nullable=False, default="Nuovo progetto")
    # 1 = tutto anonimizzato (file del progetto, allegati e prompt di ogni
    # chat), 0 = tutto in chiaro. Immutabile dopo la creazione.
    anonymized = Column(Integer, nullable=False, default=0)
    # Categorie escluse A LIVELLO DI PROGETTO (stesso formato di
    # Conversation.anon_options_json): valgono per l'anonimizzazione dei file
    # e di tutte le chat; le chat di progetto non hanno un override proprio.
    anon_options_json = Column(Text, nullable=False, default="{}")
    # Il contatore monotono del registro condiviso (vedi sopra).
    mapping_version = Column(Integer, nullable=False, default=0)
    # L'utente ha rifiutato l'avviso sui file ancora da confermare all'apertura
    # di una chat (`anon.unconfirmed.dontAsk`): è una preferenza DI QUESTO progetto
    # (i file da confermare sono suoi), non un'impostazione globale.
    skip_unconfirmed_warning = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    def descriptor(self):
        return {
            "id": self.id,
            "name": self.name,
            "anonymized": bool(self.anonymized),
            "mapping_version": self.mapping_version or 0,
            "skip_unconfirmed_warning": bool(self.skip_unconfirmed_warning),
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at),
        }


class ProjectFile(Base):
    """Un file caricato nell'ambiente del progetto (non in una chat).

    È l'unione delle due nature che gli servono: la PREVIEW rivedibile
    (boxes, page_sizes, rev, sealed — lo stesso viewer dell'anteprima chat) e
    l'ESPOSIZIONE al modello nelle chat del progetto (nome neutro, schede
    briefing, versione del registro a cui la copia protetta è stata scritta).
    La mappa NON sta qui: è il registro del progetto (ConversationEntity con
    conv_id = project_id). I byte stanno in data/projects/{project_id}/
    (vedi project_files.py).

    `confirmed`: il file diventa visibile alle chat solo quando l'utente,
    vista l'anteprima, lo conferma. Prima della conferma (e in ogni caso
    finché il progetto non ha messaggi) sono ammesse anche le modifiche
    distruttive; dopo, solo operazioni additive (vedi routes/projects.py)."""
    __tablename__ = "project_files"
    # nome dell'indice ESPLICITO: la migrazione baseline crea lo stesso indice
    # con lo stesso nome su entrambi i motori
    __table_args__ = (
        Index("ix_project_files_project_id", "project_id"),
    )
    id = Column(String(32), primary_key=True, default=_uuid)
    project_id = Column(String(32), ForeignKey("projects.id"), nullable=False)
    filename = Column(String(256), nullable=False)
    # nome neutro verso modello e sandbox (il filename è PII): progetto_NN.ext
    model_filename = Column(String(256))
    mime = Column(String(128))
    size = Column(Integer, nullable=False, default=0)
    n_pages = Column(Integer, nullable=False, default=0)
    rev = Column(Integer, nullable=False, default=0)
    confirmed = Column(Integer, nullable=False, default=0)
    # versione del registro di progetto a cui la copia protetta è stata
    # scritta: il controllo di uscita per-versione è lo stesso degli allegati
    mapping_version = Column(Integer, nullable=False, default=0)
    # Esito dell'ULTIMO controllo di allineamento (project_files.stale_leaks):
    # `stale_version` è la mapping_version del progetto a cui il controllo è
    # stato fatto (-1 = mai), `stale_json` le superfici trovate ancora in
    # chiaro. Verdetto in memoria: senza, ogni apertura del progetto
    # rileggerebbe e riscandirebbe tutti i file.
    stale_version = Column(Integer, nullable=False, default=-1)
    stale_json = Column(Text, nullable=False, default="[]")
    n_images = Column(Integer, nullable=False, default=0)
    # scheda del contenuto (openrouter/briefing.py): dell'originale e — nei
    # progetti anonimizzati — della copia protetta (quella che vede il modello)
    briefing_json = Column(Text)
    model_briefing_json = Column(Text)
    # JSON della preview (solo progetti anonimizzati)
    report_json = Column(Text, nullable=False, default="{}")
    by_label_json = Column(Text, nullable=False, default="{}")
    original_boxes_json = Column(Text, nullable=False, default="{}")
    anonymized_boxes_json = Column(Text, nullable=False, default="{}")
    page_sizes_json = Column(Text, nullable=False, default="[]")
    sealed_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime(timezone=True), default=_now)

    def briefing(self):
        try:
            return json.loads(self.briefing_json) if self.briefing_json else None
        except ValueError:
            return None

    def model_briefing(self):
        try:
            return (json.loads(self.model_briefing_json)
                    if self.model_briefing_json else None)
        except ValueError:
            return None

    def descriptor(self, mapping=None):
        """Il descrittore nella forma che DocumentViewer consuma. `mapping`
        arriva dal chiamante: è il registro del progetto filtrato ai
        placeholder di QUESTO file."""
        sizes = json.loads(self.page_sizes_json or "[]")
        if isinstance(sizes, list):
            sizes = {"original": sizes, "anonymized": sizes}
        d = {
            "id": self.id,
            "filename": self.filename,
            "model_filename": self.model_filename,
            "size": self.size,
            "created_at": iso_utc(self.created_at),
            "n_pages": self.n_pages,
            "rev": self.rev or 0,
            "confirmed": bool(self.confirmed),
            "mapping_version": self.mapping_version or 0,
            "n_images": self.n_images or 0,
            # scheda del contenuto originale: il frontend la usa per il chip
            # informativo (etichetta tipo "PDF, 2 pagine")
            "briefing": self.briefing(),
            "by_label": json.loads(self.by_label_json or "{}"),
            "report": json.loads(self.report_json or "{}"),
            "original_boxes": json.loads(self.original_boxes_json or "{}"),
            "anonymized_boxes": json.loads(self.anonymized_boxes_json or "{}"),
            "page_sizes": sizes,
            "sealed": json.loads(self.sealed_json or "[]"),
        }
        if mapping is not None:
            d["mapping"] = mapping
        return d


class Conversation(Base):
    """Una chat LLM via OpenRouter. Il thread
    completo sta in chat_messages; qui i metadati e le scelte per-conversazione
    (modello, opzioni di ragionamento)."""
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_project_id", "project_id"),
    )
    id = Column(String(32), primary_key=True, default=_uuid)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # chat DENTRO un progetto: eredita il modo dal progetto, condivide il suo
    # registro (scope = project_id) e vede i suoi file confermati. NULL = chat
    # libera, col proprio registro e nessun file di progetto.
    project_id = Column(String(32))
    title = Column(String(200), nullable=False, default="Nuova conversazione")
    model = Column(String(128), nullable=False, default="")
    # opzioni del turno scelte in UI (reasoning, ...): JSON libero, il backend
    # le rilegge e le applica a ogni messaggio
    options_json = Column(Text, nullable=False, default="{}")
    # IL modo della conversazione: 1 = tutto anonimizzato (ogni allegato e ogni
    # prompt), 0 = chat normale. Si sceglie alla creazione e si blocca al primo
    # invio. Non esiste una via di mezzo: un turno in chiaro e uno protetto
    # nella stessa conversazione mostrerebbero al modello lo stesso valore in
    # due forme senza modo di collegarle.
    anonymized = Column(Integer, nullable=False, default=0)
    # Scelte di anonimizzazione di QUESTA conversazione, sovrascritte sui
    # default dell'admin: {"excluded_tags": [...]} = categorie da lasciare in
    # chiaro. "{}" (il default) vuol dire "quelle dell'amministratore", e non è
    # la stessa cosa di una lista vuota: senza override, un cambio del pannello
    # admin si vede subito anche nelle chat già aperte.
    anon_options_json = Column(Text, nullable=False, default="{}")
    # Versione monotona del registro PII conversation-scoped. Non riscrive mai
    # i messaggi già inviati: fotografa soltanto il mapping usato dal turno.
    mapping_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    def descriptor(self):
        return {
            "id": self.id,
            "title": self.title,
            "project_id": self.project_id,
            "model": self.model,
            "options": json.loads(self.options_json or "{}"),
            "anonymized": bool(self.anonymized),
            "mapping_version": self.mapping_version or 0,
            "created_at": iso_utc(self.created_at),
            "updated_at": iso_utc(self.updated_at),
        }


class ChatMessage(Base):
    """Un messaggio del thread, nel formato più vicino possibile a quello di
    OpenRouter: to_openrouter() ricostruisce ESATTAMENTE ciò che va rimandato
    all'API al turno successivo — incluso reasoning_details, che va rispedito
    invariato o i modelli reasoning degradano nel tool calling."""
    __tablename__ = "chat_messages"
    id = Column(String(32), primary_key=True, default=_uuid)
    conv_id = Column(String(32), ForeignKey("conversations.id"), nullable=False)
    seq = Column(Integer, nullable=False)          # ordine nel thread
    role = Column(String(16), nullable=False)      # system|user|assistant|tool
    content = Column(Text)                         # testo (tool: JSON esito)
    # Per gli user message `display_content` è l'originale locale e
    # `model_content` la fotografia immutabile realmente inviata. Per gli
    # assistant `model_content`/content conservano sempre i TAG canonici.
    display_content = Column(Text)
    model_content = Column(Text)
    anonymized = Column(Integer)                   # 0 raw | 1 protetto
    mapping_version = Column(Integer, nullable=False, default=0)
    tool_calls_json = Column(Text)                 # assistant: [{id,function,...}]
    # id della chiamata (ruolo tool). Text e non String(64): lo genera il
    # MODELLO e va rispedito identico a OpenRouter — non si può troncare
    # (spezzerebbe l'aggancio coi tool_calls del messaggio assistant) e
    # Postgres, a differenza di SQLite, le lunghezze le applica davvero.
    tool_call_id = Column(Text)
    reasoning_json = Column(Text)                  # [{...}] reasoning_details
                                                   # oppure {"text": "..."}
    model = Column(String(128))                    # modello EFFETTIVO (risposta)
    finish_reason = Column(String(32))
    # User-facing turn failure, persisted so a refresh does not erase the
    # only explanation of an unsuccessful provider request. It is never sent
    # back to OpenRouter as part of message history.
    error = Column(Text)
    usage_json = Column(Text)                      # usage+costo (fine turno)
    created_at = Column(DateTime(timezone=True), default=_now)

    def to_openrouter(self):
        msg = {"role": self.role,
               "content": (self.model_content
                           if self.model_content is not None else self.content)}
        if self.role == "assistant":
            if self.tool_calls_json:
                msg["tool_calls"] = json.loads(self.tool_calls_json)
            if self.reasoning_json:
                r = json.loads(self.reasoning_json)
                if isinstance(r, list):
                    msg["reasoning_details"] = r
                elif r.get("text"):
                    msg["reasoning"] = r["text"]
        elif self.role == "tool":
            msg["tool_call_id"] = self.tool_call_id
        return msg

    def descriptor(self, mapping=None, decode=None, hidden=None):
        canonical = (self.model_content
                     if self.model_content is not None else self.content)
        shown = (self.display_content if self.role == "user"
                 and self.display_content is not None else canonical)
        entities = []
        code_values = {}
        if self.role == "assistant" and decode and canonical:
            shown, entities = decode(canonical, mapping or {})
            if hidden:
                # TAG rimasti dentro codice/link: servono al bottone "copia coi
                # valori reali", non al rendering
                code_values = hidden(canonical, mapping or {})
        d = {
            "id": self.id,
            "seq": self.seq,
            "role": self.role,
            "content": shown,
            "anonymized": (None if self.anonymized is None
                            else bool(self.anonymized)),
            "mapping_version": self.mapping_version or 0,
            "created_at": iso_utc(self.created_at),
        }
        if self.tool_calls_json:
            calls = json.loads(self.tool_calls_json)
            if decode and mapping:
                # gli argomenti decodificati per il DISPLAY (la query reale
                # partita, nello step del tool): la forma canonica coi TAG
                # resta in function.arguments — è quella che si rispedisce
                for tc in calls:
                    try:
                        args = json.loads((tc.get("function") or {})
                                          .get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    if isinstance(args, dict) and args:
                        tc["display_args"] = {
                            k: decode(v, mapping)[0] if isinstance(v, str)
                            else v for k, v in args.items()}
            d["tool_calls"] = calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.reasoning_json:
            r = json.loads(self.reasoning_json)
            # in UI serve solo il testo leggibile, mai i blocchi cifrati
            if isinstance(r, dict):
                d["reasoning"] = r.get("text")
            else:
                d["reasoning"] = "".join(
                    b.get("text") or b.get("summary") or ""
                    for b in r if isinstance(b, dict)) or None
        if self.model:
            d["model"] = self.model
        if self.finish_reason:
            d["finish_reason"] = self.finish_reason
        if self.error:
            d["error"] = self.error
        if self.usage_json:
            d["usage"] = json.loads(self.usage_json)
        if entities:
            d["entities"] = entities
        if code_values:
            d["code_values"] = code_values
        return d


class ConversationEntity(Base):
    """Entità PII stabile nella conversazione; gli alias sono righe separate
    perché più superfici possono condividere lo stesso placeholder.

    `merged_into` realizza la fusione delle entità SENZA riscrivere il
    passato: la riga resta (i messaggi già inviati contengono il suo
    placeholder e devono continuare a decodificarsi), ma da quel momento le
    superfici che ci arrivavano risolvono sull'entità di destinazione. Il
    placeholder assorbito non viene mai riassegnato.

    NOTA sullo scope: `conv_id` è in realtà uno SCOPE ID. Per le chat
    libere è l'id della conversazione; per i PROGETTI è l'id del progetto —
    tutte le chat di un progetto e i suoi file condividono lo stesso
    registro. PER QUESTO la colonna NON ha una ForeignKey verso
    conversations: su Postgres il vincolo viene applicato davvero e
    rifiuterebbe le righe di progetto (su SQLite sarebbe solo decorativo,
    PRAGMA foreign_keys spento).
    L'integrità la garantisce app/purge.py: il registro se ne va
    col suo portatore — Project o Conversation a seconda dello scope
    (chat_anonymization.registry_holder)."""
    __tablename__ = "conversation_entities"
    __table_args__ = (
        UniqueConstraint("conv_id", "placeholder",
                         name="uq_conv_entity_placeholder"),
        Index("ix_conv_entities_conv_id", "conv_id"),
    )
    id = Column(String(32), primary_key=True, default=_uuid)
    conv_id = Column(String(32), nullable=False)     # SCOPE id, vedi sopra
    placeholder = Column(String(96), nullable=False)
    label = Column(String(64), nullable=False)
    canonical_value = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)
    mapping_version = Column(Integer, nullable=False, default=0)
    merged_into = Column(String(32))       # id dell'entità che l'ha assorbita
    merge_checked = Column(Integer, nullable=False, default=0)  # 1 = tenute separate
    # 1 = l'utente ha scelto (dall'anteprima pre-invio) di lasciare questo
    # valore IN CHIARO: da qui in poi non si sostituisce più, ma la riga resta
    # nel dizionario canonico perché i placeholder già partiti si decodificano
    # per sempre (stesso principio delle categorie escluse).
    excluded = Column(Integer, nullable=False, default=0)


class ConversationEntityAlias(Base):
    __tablename__ = "conversation_entity_aliases"
    __table_args__ = (
        UniqueConstraint("entity_id", "original_surface",
                         name="uq_entity_alias_surface"),
        Index("ix_conv_aliases_entity_id", "entity_id"),
        Index("ix_conv_aliases_normalized", "normalized_key"),
    )
    id = Column(String(32), primary_key=True, default=_uuid)
    entity_id = Column(String(32), ForeignKey("conversation_entities.id"),
                       nullable=False)
    original_surface = Column(Text, nullable=False)
    normalized_key = Column(Text, nullable=False)
    source = Column(String(32), nullable=False, default="detector")
    confidence = Column(String(16), nullable=False, default="exact")
    created_at = Column(DateTime(timezone=True), default=_now)
    # Versione del registro a cui questa superficie è diventata nota: il
    # controllo di uscita su un allegato già protetto deve confrontarsi solo
    # con ciò che si sapeva quando quel file è stato scritto.
    mapping_version = Column(Integer, nullable=False, default=0)


class Attachment(Base):
    """File di una conversazione, nelle DUE direzioni: upload
    dell'utente (in) e artifact prodotti dalla sandbox (out). I byte stanno su
    disco in DATA_DIR/chats/{conv_id}/, mai in SQLite.

    message_id lega il file al TURNO: per gli artifact è il messaggio
    assistant che li ha prodotti, per gli upload il messaggio utente che li ha
    portati in conversazione (NULL = caricato ma non ancora inviato). Serve a
    ricostruire sempre la stessa storia verso OpenRouter — allegato multimodale
    incluso — e a mostrare i file sotto la bolla giusta."""
    __tablename__ = "attachments"
    id = Column(String(32), primary_key=True, default=_uuid)
    conv_id = Column(String(32), ForeignKey("conversations.id"), nullable=False)
    direction = Column(String(8), nullable=False, default="in")   # in | out
    source = Column(String(16), nullable=False, default="upload")  # upload | sandbox
    filename = Column(String(256), nullable=False)
    mime = Column(String(128))
    size = Column(Integer, nullable=False, default=0)
    message_id = Column(String(32))     # il messaggio del turno (vedi sopra)
    # scheda del contenuto calcolata all'upload SENZA LLM (openrouter/
    # briefing.py): fogli e colonne di un xlsx, pagine di un PDF... Si salva
    # perché ri-parsare il file a ogni messaggio sarebbe uno spreco.
    briefing_json = Column(Text)
    # direction="in": pending (in chat anonimizzata, non ancora inviato) |
    # protected (redatto all'invio) | raw (chat normale) | failed (l'ultimo
    # tentativo di invio si è rotto su questo file: il reinvio riprova).
    # direction="out": raw | protected | restored (vedi _restore_artifact).
    anonymization_status = Column(String(16), nullable=False, default="raw")
    # nome mostrato all'utente quando differisce da `filename` (conversione
    # d'ingresso: chi carica un .doc scarica un .docx)
    display_filename = Column(String(256))
    # nome neutro verso modello e sandbox: il filename originale è PII
    model_filename = Column(String(256))
    # byte su disco: l'originale locale e, quando il turno l'ha scritta, la
    # copia protetta che è realmente uscita verso il modello
    original_path = Column(Text)
    protected_path = Column(Text)
    # scheda dell'allegato calcolata sulla copia PROTETTA (quella che il
    # modello vede) e report dell'anonimizzazione del turno
    model_briefing_json = Column(Text)
    anonymization_report_json = Column(Text)
    # versione del registro con cui la copia protetta è stata scritta: sotto
    # questa versione il file è rimasto indietro e va ri-redatto
    mapping_version = Column(Integer, nullable=False, default=0)
    # quante immagini OCR-izzabili contiene il file (contate all'upload):
    # il frontend le usa per il popup "vuoi usare l'OCR?" all'invio
    n_images = Column(Integer, nullable=False, default=0)
    # Aree SIGILLATE dall'anteprima pre-invio (solo allegati PDF/immagine):
    # stesso formato di Document.sealed_json. Stanno sulla riga dell'allegato
    # (non nello stato staged in RAM) perché ogni ri-protezione riparte
    # dall'originale — invio diretto e reinvio inclusi — e deve riapplicarle.
    sealed_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime(timezone=True), default=_now)

    def briefing(self):
        try:
            return json.loads(self.briefing_json) if self.briefing_json else None
        except ValueError:
            return None

    def descriptor(self):
        try:
            model_briefing = (json.loads(self.model_briefing_json)
                              if self.model_briefing_json else None)
        except ValueError:
            model_briefing = None
        try:
            report = (json.loads(self.anonymization_report_json)
                      if self.anonymization_report_json else None)
        except ValueError:
            report = None
        # Nome proposto per il download della copia redatta. L'estensione va
        # presa dal file protetto, non dall'originale: la redazione lavora sul
        # formato convertito (.doc -> .docx, .xls -> .xlsx).
        anonymized_filename = None
        if (self.direction == "in" and self.anonymization_status == "protected"
                and self.protected_path):
            base = self.display_filename or self.filename
            stem = base.rsplit(".", 1)[0] or "allegato"
            ext = os.path.splitext(self.protected_path)[1]
            anonymized_filename = f"{stem}_anonimizzato{ext}"
        return {
            "id": self.id,
            "direction": self.direction,
            "source": self.source,
            "filename": self.display_filename or self.filename,
            "display_filename": self.display_filename or self.filename,
            "model_filename": self.model_filename,
            "mime": self.mime,
            "size": self.size,
            "message_id": self.message_id,
            "briefing": self.briefing(),
            "model_briefing": model_briefing,
            "anonymization_status": self.anonymization_status or "raw",
            "anonymized_filename": anonymized_filename,
            "anonymization_report": report,
            "mapping_version": self.mapping_version or 0,
            "n_images": self.n_images or 0,
            "created_at": iso_utc(self.created_at),
        }


_engine = None
SessionLocal = None


def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    # WAL: i lettori non vengono mai bloccati dallo scrittore
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def init_db():
    """Crea l'engine sul motore scelto da DATABASE_URL e porta lo schema a
    head con le migrazioni Alembic (adozione dei blockingbear.db esistenti
    inclusa, vedi db_migrate.py). Questo ramo condizionale è L'UNICO punto,
    insieme alle migrazioni, in cui i due dialetti si distinguono: nel codice
    applicativo non devono esistere "if sqlite / if postgres"."""
    global _engine, SessionLocal
    ensure_dirs()
    url = make_url(DATABASE_URL)
    if url.get_backend_name() == "sqlite":
        # timeout 180s: un turno di anonimizzazione tiene la transazione di
        # scrittura aperta per tutta la sua durata (NER/OCR/redazione), quindi
        # una seconda chat concorrente deve poter aspettare ben oltre i 5s di
        # default o muore con "database is locked"
        _engine = create_engine(
            DATABASE_URL,
            connect_args={"check_same_thread": False, "timeout": 180})
        event.listen(_engine, "connect", _sqlite_pragmas)
    else:
        # Postgres: niente PRAGMA e niente connect_args SQLite (psycopg li
        # rifiuterebbe). I turni di anonimizzazione tengono una connessione
        # occupata a lungo, quindi il pool deve poterne dare parecchie in
        # parallelo; pre_ping scarta le connessioni morte (riavvio del server
        # Postgres) invece di far fallire la prima richiesta che le pesca.
        _engine = create_engine(DATABASE_URL, pool_pre_ping=True,
                                pool_size=10, max_overflow=20)
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    run_migrations(_engine)
    return SessionLocal


def get_session():
    """Dependency FastAPI: una sessione per richiesta."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()

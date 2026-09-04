"""Coda di elaborazione in-process: upload -> Job -> worker -> ProjectFile.

Perché NON Celery/Redis: l'installazione presso i clienti è un singolo
servizio e i file in attesa vivono nella RAM di QUESTO processo; un broker
esterno costringerebbe i bytes in chiaro a transitare fuori dal processo.

Architettura:
  - lo STATO dei job sta in SQLite (tabella jobs): sopravvive al riavvio;
  - i BYTES del file in ATTESA stanno in `_payloads` (solo RAM: è una coda
    transitoria): al boot i job rimasti queued/processing diventano failed;
    a elaborazione conclusa originale e copia protetta sono su disco
    (data/projects/, vedi project_files.py);
  - N_WORKERS thread consumatori, ognuno con la SUA istanza del modello
    (la pipeline HF non è thread-safe); il tetto di CPU è unico e globale
    (torch.set_num_threads(CPU_THREADS): il pool intra-op è per-processo);
  - i job dello STESSO utente sono sempre sequenziali anche con più worker:
    la coda condivisa contiene utenti (ognuno al massimo una volta), non job;
    ogni utente ha la sua FIFO di job e mentre un worker lo serve l'utente
    non rientra in coda, quindi nessun altro worker può prenderne i job;
  - i cambi di stato vengono pubblicati alle code asyncio degli endpoint SSE
    via loop.call_soon_threadsafe (unico ponte thread -> event loop).
"""

import collections
import datetime
import queue
import threading
import time

from . import db
from .config import CPU_THREADS, MODEL_DIR, N_WORKERS
from .db import Job
from .engine import PiiEngine
from .engine.convert import ConvertError
from .engine.docx import DocxError
from .engine.image_ocr import OcrError
from .engine.pdf import PdfError
from .engine.pptx import PptxError
from .engine.progress import JobCanceled, JobControl
from .engine.txt import TxtError
from .engine.xlsx import XlsxError
from .logging_setup import get_logger

log = get_logger("blockingbear.jobs")

ENGINES = [PiiEngine(MODEL_DIR) for _ in range(N_WORKERS)]

_ready = queue.Queue()          # owner_id con lavoro da fare, ognuno max 1 volta
_user_queues = {}               # owner_id -> deque di job_id in ordine FIFO
_scheduled = set()              # owner in _ready OPPURE in lavorazione su un worker
_active = set()                 # owner ADESSO su un worker (per position())
_payloads = {}                  # job_id -> ("upload", bytes, filename) oppure
                                #           ("column", params): SOLO RAM
_doc_busy = set()               # doc_id con un job di colonna o di rielaborazione
                                # OCR in coda/lavorazione: le modifiche concorrenti
                                # alla mappa sono rifiutate
_subscribers = {}               # job_id -> set(asyncio.Queue) degli SSE aperti
_cancels = {}                   # job_id -> threading.Event (annullamento cooperativo)
_progress = {}                  # job_id -> ultimo snapshot di avanzamento (SOLO RAM)
_lock = threading.Lock()
_loop = None                    # event loop del server (per publish thread-safe)
_torch_configured = False

# intervallo minimo tra due publish di progresso dello stesso job: i tick del
# modello arrivano ogni ~6s, ma quelli della redazione xlsx possono essere
# fitti; i cambi di fase e i tick di completamento passano sempre
_PROGRESS_MIN_INTERVAL = 0.4


def loaded():
    """True solo dopo un forward riuscito su TUTTI i worker.

    Verificarne uno solo lascerebbe la coda non deterministica: un documento
    potrebbe finire su un'altra istanza caricata ma non eseguibile.
    """
    return bool(ENGINES) and all(e.loaded for e in ENGINES)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _configure_torch():
    """set_num_threads PRIMA di qualsiasi inferenza. Idempotente e lockata:
    la chiamano sia il warm-up sia i worker, vince il primo."""
    global _torch_configured
    with _lock:
        if _torch_configured:
            return
        import torch
        torch.set_num_threads(CPU_THREADS)
        _torch_configured = True


# --- API per le route -----------------------------------------------------

def pending_count():
    with _lock:
        return len(_payloads)


def submit_project(job_id, owner_id, project_id, data, filename, options=None):
    """Accoda l'elaborazione di un file per un progetto ANONIMIZZATO, già
    committata su DB (il worker deve trovare la riga). L'owner entra in _ready
    solo se non è già schedulato: così un solo worker alla volta consuma la
    sua FIFO (sequenzialità per utente)."""
    _enqueue(job_id, owner_id,
             ("project_upload", project_id, data, filename, options or {}))


def submit_project_column(job_id, owner_id, project_id, file_id, sheet,
                          column):
    """Accoda l'anonimizzazione di una COLONNA xlsx di un file di progetto.
    Il file resta bloccato (doc_busy) finché il job non si conclude:
    modifiche concorrenti al registro invaliderebbero il lavoro del worker.
    ValueError se il file ha già un job in corso."""
    params = {"project_id": project_id, "file_id": file_id,
              "sheet": sheet, "column": column}
    _enqueue(job_id, owner_id, ("project_column", params), busy_doc=file_id)


def submit_chat_reprocess(job_id, owner_id, conv_id, item_id, att_id):
    """Accoda la RIELABORAZIONE con OCR di un allegato del turno in anteprima
    di una chat (chat_staging.reprocess_ocr). Come per i file di progetto
    l'allegato resta bloccato (doc_busy) fino alla conclusione: una modifica
    dell'anteprima mentre il worker la sta ri-redigendo darebbe uno stato
    incoerente. ValueError se l'allegato ha già un job in corso."""
    params = {"conv_id": conv_id, "item_id": item_id, "att_id": att_id}
    _enqueue(job_id, owner_id, ("chat_reprocess", params), busy_doc=att_id)


def submit_project_reprocess(job_id, owner_id, project_id, file_id):
    """Accoda la RIELABORAZIONE con OCR di un file di progetto esistente
    (project_files.reprocess_ocr). Come i job di colonna, il file resta
    bloccato (doc_busy) fino alla conclusione. ValueError se il file ha
    già un job in corso."""
    params = {"project_id": project_id, "file_id": file_id}
    _enqueue(job_id, owner_id, ("project_reprocess", params),
             busy_doc=file_id)


def submit_project_realign(job_id, owner_id, project_id, file_id):
    """Accoda la RI-REDAZIONE di un file di progetto col registro corrente
    (project_files.realign): è la risposta al warning di disallineamento.
    Come gli altri job sul file, lo blocca (doc_busy) fino alla conclusione.
    ValueError se il file ha già un job in corso."""
    params = {"project_id": project_id, "file_id": file_id}
    _enqueue(job_id, owner_id, ("project_realign", params), busy_doc=file_id)


def _enqueue(job_id, owner_id, payload, busy_doc=None):
    with _lock:
        if busy_doc is not None:
            if busy_doc in _doc_busy:
                raise ValueError("Su questo documento c'è già "
                                 "un'elaborazione in corso.")
            _doc_busy.add(busy_doc)
        _payloads[job_id] = payload
        _cancels[job_id] = threading.Event()
        _user_queues.setdefault(owner_id, collections.deque()).append(job_id)
        first = owner_id not in _scheduled
        if first:
            _scheduled.add(owner_id)
    if first:
        _ready.put(owner_id)


def doc_busy(doc_id):
    """True se un job di colonna sta lavorando (o aspetta di lavorare) su
    questo documento: le route di modifica rispondono 409."""
    with _lock:
        return doc_id in _doc_busy


def _busy_doc_of(payload):
    if payload and payload[0] in ("project_column", "project_reprocess",
                                  "project_realign"):
        return payload[1]["file_id"]
    if payload and payload[0] == "chat_reprocess":
        return payload[1]["att_id"]
    return None


def position(job):
    """Quanti job verranno serviti prima di questo (0 = è il prossimo), nel
    round-robin per utente: i k job dello stesso utente davanti in FIFO, più
    per ogni ALTRO utente attivo i suoi turni che si intercalano (al massimo
    k+1: uno per ogni giro). Esatta con 1 worker; con N worker sovrastima
    (si svuota prima del previsto), che per un'attesa è l'errore giusto.
    Solo display: la verità sta nelle strutture in RAM, non nel DB."""
    if job.status != "queued":
        return None
    with _lock:
        mine = _user_queues.get(job.owner_id)
        try:
            k = list(mine or ()).index(job.id)
        except ValueError:
            return 0                    # già estratto: sta per partire
        # anche un job in corso DELLO STESSO utente è un "davanti"
        n = k + (1 if job.owner_id in _active else 0)
        for owner, dq in _user_queues.items():
            if owner == job.owner_id:
                continue
            # un owner in lavorazione occupa un turno anche a FIFO vuota
            busy = 1 if owner in _active else 0
            n += min(len(dq) + busy, k + 1)
        return n


def subscribe(job_id):
    import asyncio
    q = asyncio.Queue()
    with _lock:
        _subscribers.setdefault(job_id, set()).add(q)
    return q


def unsubscribe(job_id, q):
    with _lock:
        subs = _subscribers.get(job_id)
        if subs:
            subs.discard(q)
            if not subs:
                _subscribers.pop(job_id, None)


def describe(job):
    """Descrittore del job + avanzamento in RAM (se in lavorazione): è il
    payload unico di GET /api/jobs*, del cancel e degli eventi SSE."""
    d = job.descriptor(position=position(job))
    if job.status == "processing":
        with _lock:
            p = _progress.get(job.id)
        if p:
            d["progress"] = p
    return d


def _publish_payload(job_id, payload):
    """Manda un payload già costruito agli SSE del job (da thread worker)."""
    with _lock:
        qs = list(_subscribers.get(job_id, ()))
    for q in qs:
        _loop.call_soon_threadsafe(q.put_nowait, payload)


def _publish(job):
    """Manda lo stato corrente a tutti gli SSE di questo job (da thread worker)."""
    _publish_payload(job.id, describe(job))


def cancel(job, session):
    """Annulla un job. In coda -> rimozione immediata (status `canceled`,
    payload liberato); in lavorazione -> alza il flag cooperativo: il primo
    checkpoint dell'engine solleva JobCanceled e il worker chiude il job.
    Su un job già concluso non fa nulla. Ritorna il descrittore aggiornato."""
    if job.status in ("done", "failed", "canceled"):
        return describe(job)
    removed = False
    with _lock:
        dq = _user_queues.get(job.owner_id)
        if dq is not None and job.id in dq:
            dq.remove(job.id)
            payload = _payloads.pop(job.id, None)
            _doc_busy.discard(_busy_doc_of(payload))
            _cancels.pop(job.id, None)
            # se la FIFO resta vuota NON si smonta la schedulazione qui:
            # l'owner può essere già in _ready (queue.Queue non permette
            # rimozioni) — il worker gestisce il "turno a vuoto"
            removed = True
        else:
            ev = _cancels.get(job.id)
            if ev is not None:
                ev.set()             # il worker lo vedrà al prossimo check()
    if removed:
        job.status = "canceled"
        job.finished_at = _now()
        session.commit()
        _publish(job)
        _broadcast_queued(session)   # le posizioni degli altri scalano
    return describe(job)


def _broadcast_queued(session):
    """Quando un job parte, le posizioni degli altri scalano: aggiorna chi ascolta."""
    with _lock:
        watched = list(_subscribers.keys())
    for jid in watched:
        j = session.get(Job, jid)
        if j is not None and j.status == "queued":
            _publish(j)


# --- Worker -----------------------------------------------------------------

def _progress_publisher(job_id, base_descriptor):
    """Callback di JobControl: salva lo snapshot in RAM e lo pubblica agli SSE,
    throttlato. `base_descriptor` è il descrittore fotografato a inizio
    lavorazione (status=processing per tutta la durata): niente giri in DB
    a ogni tick."""
    last = [0.0]

    def on_progress(snap):
        with _lock:
            _progress[job_id] = snap
        # i cambi di fase (done=None) e i completamenti passano sempre
        force = snap.get("done") is None or snap.get("done") == snap.get("total")
        now = time.monotonic()
        if not force and now - last[0] < _PROGRESS_MIN_INTERVAL:
            return
        last[0] = now
        _publish_payload(job_id, dict(base_descriptor, progress=snap))

    return on_progress


def _run_job(job_id, payload, engine):
    with db.SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        if payload is None:
            # riavvio tra enqueue e consumo: il payload RAM non c'è più
            job.status, job.error = "failed", "Riavvio del server: ricarica il file."
            job.finished_at = _now()
            session.commit()
            _publish(job)
            return
        job.status = "processing"
        session.commit()
        _publish(job)
        _broadcast_queued(session)
        ctl = JobControl(cancel_event=_cancels.get(job_id),
                         on_progress=_progress_publisher(job_id, job.descriptor()))
        try:
            ctl.check()                 # annullato tra l'estrazione e l'avvio
            # project_files/chat_staging importati qui e non in testa:
            # importano chat_anonymization, che importa questo modulo (ciclo)
            from . import project_files
            if payload[0] == "project_column":
                doc = project_files.process_column(payload[1], session,
                                                   ctl=ctl)
            elif payload[0] == "project_reprocess":
                doc = project_files.reprocess_ocr(payload[1], engine,
                                                  session, ctl=ctl)
            elif payload[0] == "project_realign":
                # nessun motore: si ri-redige col registro che c'è già
                doc = project_files.realign(payload[1], session, ctl=ctl)
            elif payload[0] == "chat_reprocess":
                from . import chat_staging
                doc = chat_staging.reprocess_ocr(payload[1], engine, session,
                                                 ctl=ctl)
            else:                       # project_upload
                _kind, project_id, data, name, options = payload
                doc = project_files.process_upload(
                    data, name, project_id, engine, session, ctl=ctl,
                    ocr=options.get("ocr", False))
            job.status, job.doc_id = "done", doc.id
        except JobCanceled:
            job.status = "canceled"
        except (PdfError, DocxError, PptxError, XlsxError, TxtError,
                OcrError, ConvertError) as e:
            job.status, job.error = "failed", str(e)
        except Exception as e:          # errori di ELABORAZIONE inattesi
            from .chat_staging import StagedEditError
            from .project_files import ProjectFileError
            if isinstance(e, (ProjectFileError, StagedEditError)):
                job.status, job.error = "failed", str(e)
            else:
                job.status, job.error = "failed", f"Errore interno: {e}"
        job.finished_at = _now()
        session.commit()
        _publish(job)


def _fail_job(job_id, exc):
    """Ripiego per guasti FUORI dall'elaborazione (commit fallito, publish):
    prova a non lasciare il job appeso, ma soprattutto non solleva MAI —
    qualunque cosa succeda qui, il worker deve tornare in coda."""
    log.error(f"errore di contorno sul job {job_id}: {exc!r}")
    try:
        with db.SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is not None and job.status not in ("done", "failed"):
                job.status, job.error = "failed", f"Errore interno: {exc}"
                job.finished_at = _now()
                session.commit()
                _publish(job)
    except Exception as e:
        log.error(f"impossibile marcare failed il job {job_id}: {e!r}")


def _worker(engine):
    _configure_torch()
    while True:
        owner_id = _ready.get()
        with _lock:
            # la FIFO può essere VUOTA se l'utente ha annullato i job in coda
            # (cancel non può togliere l'owner da _ready): turno a vuoto
            dq = _user_queues.get(owner_id)
            if not dq:
                _user_queues.pop(owner_id, None)
                _scheduled.discard(owner_id)
                continue
            job_id = dq.popleft()
            payload = _payloads.pop(job_id, None)
            _active.add(owner_id)
        try:
            _run_job(job_id, payload, engine)
        except Exception as e:
            # _run_job cattura già gli errori di elaborazione: qui arriva solo
            # il contorno (es. SQLite "database is locked" su un commit). Senza
            # questo except il thread morirebbe e la coda si congelerebbe.
            _fail_job(job_id, e)
        finally:
            # rimette l'owner in _ready SOLO ora: mentre il job girava nessun
            # altro worker poteva servirlo (è questo che rende la sequenza
            # per-utente); se non ha altro lavoro esce dalla schedulazione
            with _lock:
                _progress.pop(job_id, None)
                _cancels.pop(job_id, None)
                _doc_busy.discard(_busy_doc_of(payload))
                _active.discard(owner_id)
                more = bool(_user_queues.get(owner_id))
                if not more:
                    _user_queues.pop(owner_id, None)
                    _scheduled.discard(owner_id)
            if more:
                _ready.put(owner_id)


def _warmup():
    """Carica i modelli ed esegue una vera inferenza in background.

    L'API risponde subito, ma `model_loaded` resta false finche' ogni worker
    non completa il primo forward. Sulla GPU e' il momento in cui
    torch.compile/Triton cerca il compilatore C.

    Gira in un thread daemon, quindi un errore qui NON ferma il server: la
    diagnosi la deve dare il log, o l'installazione sembra riuscita e il guasto
    salta fuori solo al primo documento. Il caso di gran lunga piu' frequente e'
    la cartella del modello mancante o vuota — nel deployment in container e'
    proprio quello che si ottiene saltando il download, perche' Docker crea la
    sorgente di un bind mount inesistente come cartella VUOTA — quindi lo si
    nomina esplicitamente invece di lasciare passare l'OSError di transformers.
    """
    _configure_torch()
    if not (MODEL_DIR / "config.json").is_file():
        log.error(
            "Modello PII non trovato: %s non contiene config.json. "
            "L'anonimizzazione NON funzionera' (GET /api/health riporta "
            "\"model_loaded\": false e ogni documento fallira'). Scaricare il "
            "checkpoint in quella cartella: hf download "
            "rizzoaiacademy/rizzo-pii-0.3B --revision v1.5.0 --local-dir "
            "<cartella>. In container la cartella dell'host e' quella montata "
            "dal compose (BLOCKINGBEAR_MODEL_DIR_HOST).", MODEL_DIR)
        return
    try:
        for e in ENGINES:
            e.warmup()
    except Exception:
        log.exception(
            "Warm-up PII fallito durante una vera inferenza: "
            "GET /api/health manterra' model_loaded=false. Su CUDA verificare "
            "anche la toolchain C richiesta da torch.compile/Triton.")


def sweep_stale(session):
    """Al boot: i job rimasti queued/processing hanno perso il payload (era in
    RAM) -> failed con messaggio chiaro; i conclusi più vecchi di 30 giorni
    si eliminano (lo storico utile è il ProjectFile, non il Job)."""
    for j in session.query(Job).filter(Job.status.in_(("queued", "processing"))):
        j.status, j.error = "failed", "Riavvio del server: ricarica il file."
        j.finished_at = _now()
    cutoff = _now() - datetime.timedelta(days=30)
    (session.query(Job)
     .filter(Job.status.in_(("done", "failed", "canceled")),
             Job.created_at < cutoff)
     .delete(synchronize_session=False))
    session.commit()


def start(loop):
    """Da chiamare nello startup FastAPI, DOPO init_db (i worker usano il DB)."""
    global _loop
    _loop = loop
    threading.Thread(target=_warmup, daemon=True).start()
    for engine in ENGINES:
        threading.Thread(target=_worker, args=(engine,), daemon=True).start()

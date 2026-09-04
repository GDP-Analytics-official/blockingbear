"""Progetti: contenitori di file pre-caricati + N chat che li vedono tutti.

Il modo (anonimizzato / in chiaro) si sceglie alla creazione — stesse regole
delle chat, policy admin inclusa — e vale per file e chat del progetto. Le
regole di modifica dei file stanno in project_files.py; qui la loro
applicazione:

  - finché il progetto NON ha messaggi: ogni modifica è ammessa
    (deanonimizza, colonne, sigilli, riallineamenti) su qualunque file;
  - dal primo messaggio di una chat: solo operazioni ADDITIVE (anonimizza in
    più, sigilli nuovi, upload di nuovi file) — i placeholder sono partiti
    verso il modello e il passato non si riscrive;
  - un file diventa visibile alle chat solo alla CONFERMA (dopo la revisione
    dell'anteprima); un file mai confermato si può sempre riallineare al
    registro corrente.
"""

import json
import re

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import chat_staging, jobs, probe_store, project_files, purge
from .. import chat_anonymization as chat_anon
from .. import settings_store
from ..auth import current_user
from ..db import (Conversation, ConversationEntity, Job, Project, ProjectFile,
                  User, get_session, iso_utc)
from ..engine import image_ocr
from ..engine import pdf as pdf_engine
from ..engine.txt import TEXT_EXTS
from ..errors import ApiError, from_internal
from ..project_files import ProjectFileError
from .chat_routes import _running

router = APIRouter(prefix="/api/projects", tags=["projects"])

# tutti i formati che il motore sa redigere
_ACCEPTED = (".pdf", ".docx", ".doc", ".odt", ".pptx", ".ppt", ".odp",
             ".xlsx", ".xls", ".xlsm", ".ods") + TEXT_EXTS \
    + image_ocr.IMAGE_EXTS


class ProjectIn(BaseModel):
    name: str = ""
    anonymized: bool = False
    # {"excluded_tags": [...]}: categorie da lasciare in chiaro, scelte alla
    # creazione e valide per file e chat del progetto
    anon_options: dict | None = None


class ProjectPatch(BaseModel):
    name: str | None = None
    anon_options: dict | None = None
    # rifiuto dell'avviso sui file da confermare, per progetto
    # (`anon.unconfirmed.dontAsk`)
    skip_unconfirmed_warning: bool | None = None


class DeanonymizeIn(BaseModel):
    placeholder: str | None = None
    label: str | None = None


class AnonymizeTextIn(BaseModel):
    text: str
    save_term: bool = False
    term_tag: str | None = None


class ExtractIn(BaseModel):
    source: str
    page: int
    rect: list[float]


class SealIn(BaseModel):
    page: int
    rect: list[float]
    all_pages: bool = False


class ColumnIn(BaseModel):
    sheet: str
    column: str


class MergeIn(BaseModel):
    into: str


def _get_project(project_id, user, session):
    project = session.get(Project, project_id)
    if project is None or (user.role != "admin"
                           and project.owner_id != user.id):
        raise ApiError(404, "project_not_found", "Progetto non trovato.")
    return project


def _get_file(project, file_id, session):
    pf = session.get(ProjectFile, file_id)
    if pf is None or pf.project_id != project.id:
        raise ApiError(404, "file_not_found", "File non trovato.")
    return pf


def _ensure_file_not_busy(pf):
    if jobs.doc_busy(pf.id):
        raise ApiError(409, "file_busy",
                       "Su questo file c'è già un'elaborazione "
                       "in corso (colonna o rielaborazione con "
                       "OCR): attendi che finisca, o annullala, "
                       "prima di fare modifiche.")


def _ensure_destructive_allowed(session, project):
    """Le modifiche DISTRUTTIVE (deanonimizza, rimozione sigilli, colonne in
    chiaro) sono ammesse solo finché nessuna chat del progetto ha inviato
    messaggi: dopo, i placeholder sono nel contesto del modello e togliere
    protezione riscriverebbe il patto del registro."""
    if project_files.project_has_messages(session, project.id):
        raise ApiError(
            409, "project_additive_only",
            "Le chat di questo progetto hanno già inviato messaggi: da "
            "qui in poi sono ammesse solo modifiche additive "
            "(anonimizzare altro testo, sigillare aree, caricare nuovi "
            "file).")


def _ensure_no_turn_running(session, project):
    convs = (session.query(Conversation.id)
             .filter_by(project_id=project.id).all())
    if any(cid in _running for (cid,) in convs):
        raise ApiError(409, "project_turn_running",
                       "C'è una risposta in corso in una chat del "
                       "progetto: riprova a turno finito.")


def _project_payload(session, project):
    opts = chat_anon.anon_options(session, project)
    n_files = (session.query(ProjectFile)
               .filter_by(project_id=project.id).count())
    n_chats = (session.query(Conversation)
               .filter_by(project_id=project.id).count())
    return {**project.descriptor(),
            "n_files": n_files, "n_chats": n_chats,
            "has_messages": project_files.project_has_messages(session,
                                                               project.id),
            "anon_options": {
                "excluded_tags": opts["excluded_tags"],
                # annotati con la provenienza: sono i termini globali PIÙ
                # quelli personali del proprietario del progetto
                "custom_terms": chat_anon.anon_terms_view(session, project),
                "inherited": chat_anon._own_excluded(project) is None}}


def _file_row(pf, leaks=()):
    """Riga leggera per l'elenco (il descrittore completo, con mappa e box,
    viaggia solo sulla GET del singolo file).

    `leaks` è l'esito di project_files.stale_leaks: lista vuota = allineato
    alla mappa corrente, None = non verificato (file mai controllato e troppo
    grosso per farlo dentro la GET). I valori servono a dire all'utente COSA
    è rimasto in chiaro: sono suoi e li vede già aprendo il file."""
    return {"id": pf.id, "filename": pf.filename,
            "stale": None if leaks is None else bool(leaks),
            "stale_values": [v for _ph, v in (leaks or ())][:3],
            "model_filename": pf.model_filename,
            "size": pf.size, "n_pages": pf.n_pages,
            "confirmed": bool(pf.confirmed),
            "n_images": pf.n_images or 0,
            "mapping_version": pf.mapping_version or 0,
            "by_label": json.loads(pf.by_label_json or "{}"),
            "briefing": pf.briefing(),
            "busy": jobs.doc_busy(pf.id),
            "created_at": iso_utc(pf.created_at)}


# --- CRUD progetto -----------------------------------------------------------

@router.post("")
def create_project(body: ProjectIn, user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    """Il modo si sceglie QUI e non si cambia più (i file e le chat del
    progetto nascono tutti nello stesso modo). Con la policy obbligatoria
    ogni progetto nasce anonimizzato, come le chat."""
    anonymized = body.anonymized
    if settings_store.chat_anonymization_policy(session) == "required":
        anonymized = True
    project = Project(owner_id=user.id,
                      name=body.name.strip()[:200] or "Nuovo progetto",
                      anonymized=1 if anonymized else 0)
    if body.anon_options is not None and anonymized:
        try:
            chat_anon.set_anon_options(project,
                                       body.anon_options.get("excluded_tags"))
        except ValueError as e:
            raise from_internal(e)
    session.add(project)
    session.commit()
    return _project_payload(session, project)


@router.get("")
def list_projects(user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    """I PROPRI progetti (come per le chat: la lista è personale, l'accesso
    puntuale per id resta possibile all'admin per assistenza)."""
    out = []
    for p in (session.query(Project).filter_by(owner_id=user.id)
              .order_by(Project.updated_at.desc()).limit(200)):
        n_files = (session.query(ProjectFile)
                   .filter_by(project_id=p.id).count())
        n_chats = (session.query(Conversation)
                   .filter_by(project_id=p.id).count())
        out.append({**p.descriptor(), "n_files": n_files, "n_chats": n_chats})
    return out


@router.get("/{project_id}")
def get_project(project_id: str, user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """Il progetto con i suoi file e le sue chat.

    Ogni file porta il verdetto di allineamento al registro corrente,
    ricalcolato qui: un altro file o una chat possono avere insegnato al
    registro superfici che questa copia protetta contiene ancora in chiaro.
    Su un file con un job in corso il verdetto è `null` — «non lo so» — e non
    un falso negativo."""
    project = _get_project(project_id, user, session)
    files = (session.query(ProjectFile).filter_by(project_id=project.id)
             .order_by(ProjectFile.created_at).all())
    chats = (session.query(Conversation).filter_by(project_id=project.id)
             .order_by(Conversation.updated_at.desc()).all())
    # allineamento alla mappa corrente: quasi sempre è un confronto tra
    # interi (vedi stale_leaks), e il verdetto resta sulla riga — un file su
    # cui sta girando un job non si controlla (sta per cambiare comunque):
    # None = «non lo so», che è la verità finché il worker non ha finito
    rows = []
    for pf in files:
        leaks = (None if jobs.doc_busy(pf.id)
                 else project_files.stale_leaks(session, project, pf))
        rows.append(_file_row(pf, leaks))
    session.commit()               # i verdetti appena calcolati
    return {**_project_payload(session, project),
            "chat_anonymization_policy":
                settings_store.chat_anonymization_policy(session),
            "files": rows,
            "chats": [c.descriptor() for c in chats]}


@router.patch("/{project_id}")
def patch_project(project_id: str, body: ProjectPatch,
                  user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    """Modifica nome, categorie da anonimizzare e preferenza sull'avviso dei
    file da confermare.

    Le categorie esistono solo nei progetti anonimizzati (422
    `anon_only_categories`) e non si toccano con un turno in corso in una
    delle chat del progetto. Cambiarle scarta l'anteprima pre-invio di tutte
    quelle chat, che è stata calcolata con le regole vecchie."""
    project = _get_project(project_id, user, session)
    if body.name is not None:
        project.name = body.name.strip()[:200] or project.name
    if body.skip_unconfirmed_warning is not None:
        project.skip_unconfirmed_warning = 1 if body.skip_unconfirmed_warning else 0
    if body.anon_options is not None:
        if not project.anonymized:
            raise ApiError(422, "anon_only_categories",
                           "Le categorie di anonimizzazione "
                           "esistono solo nei progetti "
                           "anonimizzati.")
        # stessa guardia delle chat: cambiare le regole mentre un turno è in
        # volo darebbe contenuti protetti con una regola e verificati con
        # un'altra; in più rende stantia ogni anteprima pre-invio
        _ensure_no_turn_running(session, project)
        try:
            chat_anon.set_anon_options(project,
                                       body.anon_options.get("excluded_tags"))
        except ValueError as e:
            raise from_internal(e)
        for (cid,) in (session.query(Conversation.id)
                       .filter_by(project_id=project.id)):
            chat_staging.discard(cid)
    session.commit()
    return _project_payload(session, project)


@router.delete("/{project_id}")
def delete_project(project_id: str, user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    """Cascata completa: chat (messaggi, allegati, sandbox), registro del
    progetto, file su disco. Il come sta in app/purge.py (la stessa cascata
    serve alla cancellazione di un utente)."""
    project = _get_project(project_id, user, session)
    purge.purge_project(session, project)
    return {"ok": True}


# --- Upload file ---------------------------------------------------------------

@router.post("/probe")
def probe(file: UploadFile, user: User = Depends(current_user),
          session: Session = Depends(get_session)):
    """Sonda PRE-upload per il popup OCR: quante immagini contiene il file,
    e se il PDF è senza layer testuale (probabile scansione).

    I byte ricevuti NON si buttano: restano da parte con un biglietto
    (`probe_id`, vedi probe_store.py) che l'upload può citare invece di
    rispedire il file. Così il documento attraversa la rete una volta sola."""
    max_upload = settings_store.get_int(session, "max_upload_mb") * 1024 * 1024
    data = file.file.read(max_upload + 1)
    if len(data) > max_upload:
        raise ApiError(413, "file_too_large_plain", "File troppo grande.")
    name = (file.filename or "").strip().lower()
    ext = ".pdf" if (name.endswith(".pdf") or data[:5] == b"%PDF-") else \
        next((e for e in (".docx", ".pptx", ".xlsx") + image_ocr.IMAGE_EXTS
              if name.endswith(e)), "")
    n_images = image_ocr.count_images(data, ext) if ext else 0
    pdf_no_text = False
    if ext == ".pdf":
        try:
            text, _ = pdf_engine.extract_text(data, allow_empty=True)
            pdf_no_text = not text.strip()
        except pdf_engine.PdfError:
            pass
    probe_id = probe_store.put(user.id, (file.filename or "").strip(),
                               file.content_type, data)
    return {"probe_id": probe_id, "images": n_images,
            "pdf_no_text": pdf_no_text,
            "ocr_available": image_ocr.available()}


@router.delete("/probe/{probe_id}")
def drop_probe(probe_id: str, user: User = Depends(current_user)):
    """Sonda abbandonata (popup OCR annullato): i byte messi da parte se ne
    vanno subito invece di aspettare la scadenza."""
    probe_store.drop(probe_id, user.id)
    return {"ok": True}


@router.post("/{project_id}/files", status_code=202)
def upload_file(project_id: str, file: UploadFile | None = File(None),
                probe_id: str = Form(None), options: str = Form(None),
                user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """Progetto anonimizzato: JOB in coda (jobs.py, risposta 202 col
    descrittore del job, avanzamento via SSE). Progetto in chiaro:
    salvataggio sincrono, risposta col descrittore del file (status 202
    comunque: il frontend distingue dal payload).

    Il file arriva come multipart oppure — se è già passato dalla sonda —
    come `probe_id`: i byte sono già sul server e non si ritrasferiscono.
    Biglietto scaduto o già usato = 410, e il frontend ripiega mandando il
    file."""
    project = _get_project(project_id, user, session)
    max_upload = settings_store.get_int(session, "max_upload_mb") * 1024 * 1024
    if probe_id:
        probed = probe_store.take(probe_id, user.id)
        if probed is None:
            raise ApiError(410, "probe_expired",
                           "Il file preparato non è più "
                           "disponibile: ricaricalo.")
        data = probed["data"]
        name = (probed["filename"] or "").strip()
        mime = probed["mime"]
    elif file is not None:
        data = file.file.read(max_upload + 1)
        name = (file.filename or "").strip()
        mime = file.content_type
    else:
        raise ApiError(422, "no_file", "Nessun file da caricare.")
    if len(data) > max_upload:
        raise ApiError(413, "file_too_large",
                       f"File troppo grande (max "
                       f"{max_upload // (1024 * 1024)} MB).",
                       max=max_upload // (1024 * 1024))
    # sanificazione come in chat: caratteri riservati + taglio a 200 con
    # estensione conservata (ProjectFile.filename e Job.filename sono
    # VARCHAR(256): SQLite non controlla, Postgres sì)
    name = project_files.safe_filename(name) if name else "documento.pdf"

    if not project.anonymized:
        pf = project_files.save_clear(session, project, data, name, mime)
        return {"file": _file_row(pf)}

    if not (name.lower().endswith(_ACCEPTED) or data[:5] == b"%PDF-"):
        raise ApiError(400, "format_not_accepted",
                       "Accetto PDF, Word (.doc/.docx/.odt), "
                       "PowerPoint (.ppt/.pptx/.odp), Excel "
                       "(.xls/.xlsx/.xlsm/.ods), testo "
                       "(.txt/.md/.csv/.tsv/.json/.xml) e immagini "
                       "(.png/.jpg/.bmp/.gif/.tiff/.webp, solo con "
                       "OCR).")
    if jobs.pending_count() >= settings_store.get_int(session, "max_queue"):
        raise ApiError(429, "queue_full",
                       "Coda di elaborazione piena: riprova tra "
                       "qualche minuto.")
    opts = {}
    if options:
        try:
            opts = json.loads(options)
            if not isinstance(opts, dict):
                raise ValueError
        except ValueError:
            raise ApiError(422, "options_invalid", "Opzioni non valide.")
    ocr = bool(opts.get("ocr")) and image_ocr.available()
    job = Job(owner_id=user.id, filename=name, kind="project_upload",
              project_id=project.id)
    session.add(job)
    session.commit()                 # prima di submit: il worker legge la riga
    jobs.submit_project(job.id, user.id, project.id, data, name,
                        {"ocr": ocr})
    return job.descriptor(position=jobs.position(job))


# --- File: lettura ---------------------------------------------------------------

@router.get("/{project_id}/files/{file_id}")
def get_file(project_id: str, file_id: str,
             user: User = Depends(current_user),
             session: Session = Depends(get_session)):
    """Il descrittore completo di un file: stato, report della redazione,
    pagine, sigilli, colonne.

    `busy` dice se ci sta girando sopra un job (una rielaborazione in coda
    riscriverà tutto), `has_messages` se il progetto ha già chat avviate —
    da lì in poi sul file sono ammesse solo operazioni additive."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    d = project_files.descriptor(session, project, pf)
    d["busy"] = jobs.doc_busy(pf.id)
    d["has_messages"] = project_files.project_has_messages(session,
                                                           project.id)
    return d


@router.get("/{project_id}/files/{file_id}/pages/{source}/{n}.png")
def file_page(project_id: str, file_id: str, source: str, n: int,
              user: User = Depends(current_user),
              session: Session = Depends(get_session)):
    """Una pagina del file renderizzata in PNG, per il visualizzatore.

    `source` è `original` o `anonymized`: sono i due lati affiancati. A
    differenza dell'anteprima delle chat questa risposta è cacheabile per dieci
    minuti — l'URL contiene l'id del file, che è stabile — ma solo `private`:
    la pagina di un documento non passa per le cache condivise."""
    if source not in ("original", "anonymized"):
        raise ApiError(404, "unknown_source", "Sorgente sconosciuta.")
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    entry = project_files.page_png(pf, source, n)
    if entry is None:
        raise ApiError(404, "page_unavailable", "Pagina non disponibile.")
    resp = Response(content=entry["png"], media_type="image/png")
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


@router.get("/{project_id}/files/{file_id}/download")
def download_file(project_id: str, file_id: str,
                  user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    """La copia che il modello vede: protetta nei progetti anonimizzati,
    l'originale in quelli in chiaro."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    path = project_files.model_path(project, pf)
    if not path.is_file():
        raise ApiError(410, "file_gone",
                       "File non più disponibile sul server.")
    if project.anonymized:
        stem = pf.filename.rsplit(".", 1)[0] or "documento"
        name = f"{stem}_anonimizzato{path.suffix}"
    else:
        name = pf.filename
    return FileResponse(path, filename=name,
                        media_type=pf.mime or "application/octet-stream")


@router.get("/{project_id}/files/{file_id}/download-original")
def download_file_original(project_id: str, file_id: str,
                           user: User = Depends(current_user),
                           session: Session = Depends(get_session)):
    """L'originale come è stato caricato, senza redazione.

    Resta sul server anche nei progetti anonimizzati: è la copia che permette
    di ripristinare il documento a lavoro finito. Byte spariti dal disco: 410
    `original_gone`."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    path = project_files.original_path(pf)
    if not path.is_file():
        raise ApiError(410, "original_gone",
                       "Originale non più disponibile sul server.")
    return FileResponse(path, filename=pf.filename,
                        media_type=pf.mime or "application/octet-stream")


# --- File: conferma, riallineamento, eliminazione ---------------------------------

@router.post("/{project_id}/files/{file_id}/confirm")
def confirm_file(project_id: str, file_id: str,
                 user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """Conferma il file dopo la revisione: da qui in poi le chat del progetto
    lo vedono.

    Prima di confermare c'è il controllo di uscita: se il registro ha imparato
    altrove valori che questa copia protetta contiene ancora in chiaro, la
    conferma si blocca e si propone il riallineamento. Il file non è mai stato
    esposto, quindi ri-redigerlo non riscrive niente che il modello abbia già
    letto. Confermare due volte non fa nulla."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    _ensure_file_not_busy(pf)
    with chat_anon.conversation_lock(project.id):
        session.expire_all()
        try:
            project_files.confirm(session, project, pf)
        except ProjectFileError as e:
            raise from_internal(e)
        session.commit()
    return {"confirmed": True, "file": _file_row(pf)}


@router.post("/{project_id}/files/{file_id}/realign")
def realign_file(project_id: str, file_id: str,
                 user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """Ri-redige il file col registro CORRENTE del progetto. Solo per file
    NON confermati: nessuna chat li ha mai visti, quindi aggiornarli non
    riscrive niente che il modello abbia letto."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    if not project.anonymized:
        raise ApiError(422, "anon_only_realign",
                       "Il riallineamento esiste solo nei progetti "
                       "anonimizzati.")
    if pf.confirmed:
        raise ApiError(409, "file_already_confirmed",
                       "Il file è già confermato: le chat "
                       "potrebbero averlo già letto e non viene "
                       "riscritto.")
    _ensure_file_not_busy(pf)
    with chat_anon.conversation_lock(project.id):
        session.expire_all()
        try:
            project_files.rebuild_file(session, project, pf)
        except ProjectFileError as e:
            raise from_internal(e)
        session.commit()
    return project_files.descriptor(session, project, pf)


@router.post("/{project_id}/files/{file_id}/stale-check")
def stale_check(project_id: str, file_id: str,
                user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """Il controllo di allineamento SENZA il tetto sull'estrazione: è la
    verifica esplicita per i file che la GET del progetto lascia in sospeso
    perché troppo grossi da leggere in linea. Dopo, il testo resta in cache e
    i controlli successivi tornano immediati."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    if not project.anonymized:
        raise ApiError(422, "anon_only_map_check",
                       "L'allineamento alla mappa esiste solo nei "
                       "progetti anonimizzati.")
    _ensure_file_not_busy(pf)
    leaks = project_files.stale_leaks(session, project, pf, inline=False)
    session.commit()
    return {"file": _file_row(pf, leaks)}


@router.post("/{project_id}/files/{file_id}/rebuild", status_code=202)
def rebuild_file_job(project_id: str, file_id: str,
                     user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    """Ri-redige il file col registro CORRENTE (job in coda, come l'upload:
    su un file grosso la redazione dura uguale). È l'azione del warning di
    disallineamento e vale anche sui file GIÀ CONFERMATI: aggiungere
    protezione è additivo, e il punto è proprio che da qui in avanti il
    modello veda protetto un valore che nei turni passati leggeva in chiaro.

    Il gemello sincrono `/realign` resta per la revisione pre-conferma, dove
    il modal aspetta indietro il descrittore aggiornato."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    if not project.anonymized:
        raise ApiError(422, "anon_only_realign",
                       "Il riallineamento esiste solo nei progetti "
                       "anonimizzati.")
    _ensure_file_not_busy(pf)
    if jobs.pending_count() >= settings_store.get_int(session, "max_queue"):
        raise ApiError(429, "queue_full",
                       "Coda di elaborazione piena: riprova tra "
                       "qualche minuto.")
    job = Job(owner_id=user.id, filename=pf.filename, doc_id=pf.id,
              kind="project_realign", project_id=project.id)
    session.add(job)
    session.commit()                 # prima di submit: il worker legge la riga
    try:
        jobs.submit_project_realign(job.id, user.id, project.id, pf.id)
    except ValueError as e:
        raise from_internal(e, 409)
    return job.descriptor(position=jobs.position(job))


@router.post("/{project_id}/files/{file_id}/reprocess-ocr", status_code=202)
def reprocess_ocr(project_id: str, file_id: str,
                  user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    """Rielabora il file con l'OCR ATTIVO (job in coda, come l'upload): il
    rilevamento si rifà dall'originale leggendo anche le immagini. Additiva
    (il registro può solo imparare), quindi ammessa anche dopo il primo
    messaggio e sui file confermati."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    if not project.anonymized:
        raise ApiError(422, "anon_only_ocr",
                       "L'OCR esiste solo nei progetti "
                       "anonimizzati.")
    if not image_ocr.available():
        raise ApiError(503, "ocr_unavailable",
                       "OCR non disponibile su questo server: "
                       "pacchetti rapidocr/onnxruntime non "
                       "installati.")
    _ensure_file_not_busy(pf)
    if jobs.pending_count() >= settings_store.get_int(session, "max_queue"):
        raise ApiError(429, "queue_full",
                       "Coda di elaborazione piena: riprova tra "
                       "qualche minuto.")
    job = Job(owner_id=user.id, filename=pf.filename,
              kind="project_reprocess", project_id=project.id)
    session.add(job)
    session.commit()                 # prima di submit: il worker legge la riga
    try:
        jobs.submit_project_reprocess(job.id, user.id, project.id, pf.id)
    except ValueError as e:
        raise from_internal(e, 409)
    return job.descriptor(position=jobs.position(job))


@router.delete("/{project_id}/files/{file_id}")
def delete_file(project_id: str, file_id: str,
                user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """Elimina il file dal progetto (i turni futuri non lo vedranno più).
    Il registro non si tocca: le sue entità possono essere già partite
    nelle chat e devono continuare a decodificarsi."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    _ensure_file_not_busy(pf)
    with chat_anon.conversation_lock(project.id):
        session.delete(pf)
        session.commit()
    project_files.drop_file(pf)
    return {"ok": True}


# --- Registro delle entità del progetto ------------------------------------------
# Gemelli degli endpoint /api/chats/{id}/entities, con lo scope del PROGETTO:
# le entità hanno conv_id = project.id e appartengono a tutti i suoi file e a
# tutte le sue chat. Servono alla revisione di un file (ultimo step del modal),
# che è l'unico posto in cui si conferma un file: le fusioni si chiedono lì,
# ricalcolate al momento dell'apertura.

def _entities_payload(session, project):
    return {"entities": chat_anon.registry_view(session, project.id),
            "suggestions": chat_anon.merge_suggestions(session, project.id)}


@router.get("/{project_id}/entities")
def list_entities(project_id: str, user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    """Il registro condiviso del progetto + le fusioni che il resolver non si
    sente di fare da solo (un cognome da solo e il nome intero restano entità
    diverse finché non è l'utente a dirlo)."""
    project = _get_project(project_id, user, session)
    return _entities_payload(session, project)


@router.post("/{project_id}/entities/{entity_id}/merge")
def merge_entity(project_id: str, entity_id: str, body: MergeIn,
                 user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """La sorgente viene assorbita dalla destinazione. NIENTE viene riscritto
    all'indietro: i file già redatti conservano il loro segnaposto, ed è il
    system prompt delle chat (merge_notes) a dire al modello che i due tag
    indicano la stessa entità."""
    project = _get_project(project_id, user, session)
    with chat_anon.conversation_lock(project.id):
        session.expire_all()
        source = session.get(ConversationEntity, entity_id)
        target = session.get(ConversationEntity, body.into)
        if (source is None or target is None
                or source.conv_id != project.id
                or target.conv_id != project.id):
            raise ApiError(404, "entity_not_found", "Entità non trovata.")
        try:
            chat_anon.merge_entities(session, project, source, target)
        except ValueError as e:
            raise from_internal(e, 409)
        session.commit()
    return _entities_payload(session, project)


@router.post("/{project_id}/entities/{entity_id}/keep-separate")
def keep_entity_separate(project_id: str, entity_id: str,
                         user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
    """Rifiuto della fusione proposta (`anon.review.keepSeparate`): la coppia
    non viene più proposta."""
    project = _get_project(project_id, user, session)
    entity = session.get(ConversationEntity, entity_id)
    if entity is None or entity.conv_id != project.id:
        raise ApiError(404, "entity_not_found", "Entità non trovata.")
    entity.merge_checked = 1
    session.commit()
    return _entities_payload(session, project)


# --- File: modifiche dalla preview -------------------------------------------------

@router.post("/{project_id}/files/{file_id}/deanonymize")
def deanonymize(project_id: str, file_id: str, body: DeanonymizeIn,
                user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """DISTRUTTIVA: l'entità diventa `excluded` in tutto il PROGETTO (qui e
    nei turni futuri di ogni chat) e il file viene ri-redatto. Ammessa solo
    finché il progetto non ha messaggi."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    _ensure_file_not_busy(pf)
    _ensure_destructive_allowed(session, project)
    with chat_anon.conversation_lock(project.id):
        session.expire_all()
        try:
            removed = project_files.deanonymize(session, project, pf,
                                                body.placeholder, body.label)
        except ProjectFileError as e:
            raise from_internal(e)
        session.commit()
    desc = project_files.descriptor(session, project, pf)
    desc["removed"] = removed
    return desc


@router.post("/{project_id}/files/{file_id}/anonymize-text")
def anonymize_text(project_id: str, file_id: str, body: AnonymizeTextIn,
                   user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    """ADDITIVA: nuovo [TAG_n] nel registro del progetto (vale anche per
    gli altri file futuri e per le chat) e ri-redazione di questo file. Il tag
    è quello scelto nel popup quando si salva anche il termine fisso, CUSTOM
    altrimenti: è lo STESSO tag nel registro e nei default, altrimenti il
    valore uscirebbe come [CUSTOM_n] qui e col tag scelto nei file dopo."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    _ensure_file_not_busy(pf)
    term_tag = "CUSTOM"
    if body.save_term:
        try:
            term_tag = settings_store.clean_tag(body.term_tag or "CUSTOM")
        except ValueError as e:
            raise from_internal(e)
    with chat_anon.conversation_lock(project.id):
        session.expire_all()
        try:
            added = project_files.anonymize_text(session, project, pf,
                                                 body.text, term_tag)
        except ProjectFileError as e:
            raise from_internal(e)
        session.commit()
    desc = project_files.descriptor(session, project, pf)
    desc["added"] = added
    if body.save_term:
        # sulla lista personale di CHI CLICCA: la checkbox è nella sua
        # interfaccia e parla dei suoi documenti futuri. Il termine vale
        # comunque subito su questo progetto, che l'ha appena messo a registro.
        desc["saved_term"] = settings_store.add_custom_term(session, user,
                                                            body.text,
                                                            term_tag)
    return desc


@router.post("/{project_id}/files/{file_id}/extract")
def extract(project_id: str, file_id: str, body: ExtractIn,
            user: User = Depends(current_user),
            session: Session = Depends(get_session)):
    """Il testo dentro un rettangolo tracciato a mano su una pagina.

    Serve alla selezione manuale del visualizzatore: l'utente cerchia
    qualcosa che il rilevatore non ha preso e da qui ottiene il testo da
    anonimizzare o da sigillare. Se sotto il rettangolo non c'è un layer
    testuale si ripiega sull'OCR del ritaglio. Non modifica il file."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    if len(body.rect) != 4:
        raise ApiError(422, "rect_invalid", "Rettangolo non valido.")
    if body.source not in ("original", "anonymized"):
        raise ApiError(404, "unknown_source", "Sorgente sconosciuta.")
    try:
        return project_files.extract(pf, body.source, body.page, body.rect)
    except ProjectFileError as e:
        raise from_internal(e)


@router.post("/{project_id}/files/{file_id}/seal-area")
def seal_area(project_id: str, file_id: str, body: SealIn,
              user: User = Depends(current_user),
              session: Session = Depends(get_session)):
    """ADDITIVA (rimuove contenuto): sigilla un'area della copia protetta."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    _ensure_file_not_busy(pf)
    with chat_anon.conversation_lock(project.id):
        session.expire_all()
        try:
            n = project_files.seal_area(session, project, pf, body.page,
                                        body.rect, body.all_pages)
        except ProjectFileError as e:
            raise from_internal(e)
        session.commit()
    desc = project_files.descriptor(session, project, pf)
    desc["sealed_added"] = n
    return desc


@router.delete("/{project_id}/files/{file_id}/seal-area/{n}")
def remove_seal(project_id: str, file_id: str, n: int,
                user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """DISTRUTTIVA: il contenuto sotto il sigillo torna nella copia protetta."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    _ensure_file_not_busy(pf)
    _ensure_destructive_allowed(session, project)
    with chat_anon.conversation_lock(project.id):
        session.expire_all()
        try:
            project_files.remove_seal(session, project, pf, n)
        except ProjectFileError as e:
            raise from_internal(e)
        session.commit()
    desc = project_files.descriptor(session, project, pf)
    desc["sealed_removed"] = n
    return desc


# --- File: colonne xlsx -------------------------------------------------------------

@router.get("/{project_id}/files/{file_id}/columns")
def columns_layout(project_id: str, file_id: str,
                   user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    """Le colonne cliccabili delle due anteprime di un file xlsx, con la loro
    posizione sulla pagina renderizzata. Vuoto sugli altri formati."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    return project_files.columns_layout(pf)


@router.post("/{project_id}/files/{file_id}/column-info")
def column_info(project_id: str, file_id: str, body: ColumnIn,
                user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """Conteggi di una colonna per il popover che precede la scelta: valori
    distinti, quanti sono trattabili e quanti sono già nel registro.

    Letti dal file intero, non dall'anteprima troncata. `busy` avverte che una
    rielaborazione in corso sta per cambiare quei numeri."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    try:
        info = project_files.column_info(session, project, pf, body.sheet,
                                         body.column)
    except ProjectFileError as e:
        raise from_internal(e)
    info["busy"] = jobs.doc_busy(pf.id)
    return info


@router.post("/{project_id}/files/{file_id}/anonymize-column",
             status_code=202)
def anonymize_column(project_id: str, file_id: str, body: ColumnIn,
                     user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    """ADDITIVA, in coda come job (come gli upload)."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    if project_files.file_ext(pf) != ".xlsx":
        raise ApiError(422, "column_xlsx_only",
                       "L'anonimizzazione per colonna vale solo "
                       "per i fogli di calcolo (.xlsx).")
    if not re.fullmatch(r"[A-Za-z]{1,3}", body.column or ""):
        raise ApiError(422, "column_invalid", "Colonna non valida.")
    _ensure_file_not_busy(pf)
    if not project_files.original_path(pf).is_file():
        raise ApiError(410, "original_gone_reupload",
                       "Originale non disponibile: ricarica "
                       "il file.")
    if jobs.pending_count() >= settings_store.get_int(session, "max_queue"):
        raise ApiError(429, "queue_full",
                       "Coda di elaborazione piena: riprova tra "
                       "qualche minuto.")
    job = Job(owner_id=user.id, kind="project_column", doc_id=pf.id,
              project_id=project.id,
              # etichetta solo informativa: il taglio a 256 (la colonna è
              # VARCHAR(256)) protegge da nomi file + fogli lunghi
              filename=(f"{pf.filename} — colonna {body.column.upper()} "
                        f"({body.sheet})")[:256])
    session.add(job)
    session.commit()
    try:
        jobs.submit_project_column(job.id, user.id, project.id, pf.id,
                                   body.sheet, body.column.upper())
    except ValueError as e:
        job.status, job.error = "failed", str(e)
        session.commit()
        raise from_internal(e, 409)
    return job.descriptor(position=jobs.position(job))


@router.post("/{project_id}/files/{file_id}/deanonymize-column")
def deanonymize_column(project_id: str, file_id: str, body: ColumnIn,
                       user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    """DISTRUTTIVA: esclude dal registro i valori della colonna."""
    project = _get_project(project_id, user, session)
    pf = _get_file(project, file_id, session)
    _ensure_file_not_busy(pf)
    _ensure_destructive_allowed(session, project)
    with chat_anon.conversation_lock(project.id):
        session.expire_all()
        try:
            removed = project_files.deanonymize_column(session, project, pf,
                                                       body.sheet,
                                                       body.column)
        except ProjectFileError as e:
            raise from_internal(e)
        session.commit()
    desc = project_files.descriptor(session, project, pf)
    desc["removed"] = removed
    return desc

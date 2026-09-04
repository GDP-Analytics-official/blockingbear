"""File di PROGETTO: elaborazione, anteprima, modifiche e regole.

Un file di un progetto ANONIMIZZATO passa da un job asincrono (jobs.py) che
produce anteprima con i box e ammette modifiche dalla preview; il rilevamento
non usa contatori locali per-documento ma il
REGISTRO DEL PROGETTO (ConversationEntity con conv_id = project_id), lo
stesso delle chat del progetto. Così lo stesso valore ha lo stesso
placeholder ovunque — file, prompt e risposte — e la decodifica è unica.

Conseguenze del registro condiviso (le stesse della chat):
  - i placeholder non si RIMUOVONO mai: "deanonimizza" marca l'entità
    `excluded` (resta nel dizionario canonico per decodificare ciò che è
    già partito) e ri-redige il file;
  - ogni scrittura fotografa `mapping_version`: un file scritto alla
    versione V non viene mai condannato da superfici note DOPO V;
  - tutte le operazioni che toccano registro o file protetti girano sotto
    ca.conversation_lock(project_id), lo stesso lock dei turni delle chat.

Un progetto IN CHIARO salva il file com'è (nessun job, nessuna preview).

`confirmed`: il file diventa visibile alle chat solo alla conferma esplicita
dell'utente (dopo la revisione dell'anteprima). Alla conferma si fa un
controllo di uscita sul testo della copia protetta: se nel frattempo il
registro ha imparato superfici che il file contiene ancora in chiaro, si
chiede di riallineare (rebuild col registro corrente) prima di esporre.

Layout su disco (DATA_DIR/projects/{project_id}/):
  {fid}_original{ext}           originale (post-conversione .doc->.docx ...)
  {fid}_protected{ext}          copia redatta (solo progetti anonimizzati)
  {fid}_original_preview.pdf    PDF renderizzabile del lato originale
  {fid}_preview.pdf             PDF renderizzabile del lato anonimizzato
  {fid}_ocr.json                cache OCR (righe lette + piano dei box)
"""

import json
import mimetypes
import re
import shutil
from pathlib import Path

from . import chat_staging, png_cache, settings_store
from . import chat_anonymization as ca
from .config import DATA_DIR
from .db import (ChatMessage, Conversation, ConversationEntity,
                 ConversationEntityAlias, Project, ProjectFile)
from .engine import image_ocr
from .engine.core import _norm
from .engine.pdf import render_page_png, spreadsheet_columns, text_in_rect
from .engine.pdf_export import PdfError
from .engine.xlsx import XlsxError, anonymize_xlsx_column, column_values, \
    sheet_names
from .openrouter import briefing

PROJECTS_DIR = DATA_DIR / "projects"


class ProjectFileError(Exception):
    """Errore user-facing con lo status HTTP suggerito (come StagedEditError).

    `code` è la chiave di traduzione (app/errors.py): `from_internal` la
    porta nel corpo della risposta accanto al `detail` italiano, e il
    frontend scrive la frase nella lingua dell'utente. Resta OPZIONALE — un
    errore senza codice esce leggibile comunque — ma quando il testo esiste
    già in `common.json` sotto `error.` va messo, o la stessa condizione
    risulta tradotta se la intercetta la route e non tradotta se la
    intercetta questo strato."""

    def __init__(self, status, msg, code=None):
        super().__init__(msg)
        self.status = status
        self.code = code


def safe_filename(filename, limit=200):
    """Nome file sicuro per disco E database: via i caratteri riservati e
    taglio a `limit` (le colonne filename sono VARCHAR(256), che Postgres —
    a differenza di SQLite — applica davvero; 200 lascia spazio ai suffissi
    che i job compongono). Il taglio conserva l'estensione: è quella che
    decide formato accettato e pipeline di redazione."""
    name = re.sub(r'[\\/:*?"<>|]+', "_", (filename or "file")).strip(". ")
    if len(name) > limit:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            name = stem[:limit - len(ext) - 1].rstrip(". ") + "." + ext
        else:
            name = name[:limit]
    return name or "file"


# --- Percorsi ---------------------------------------------------------------

def project_dir(project_id):
    return PROJECTS_DIR / project_id


def file_ext(pf):
    """L'estensione EFFETTIVA del file (il filename è già post-conversione:
    la pipeline rinomina .doc -> .docx e simili all'ingresso)."""
    return Path(pf.filename).suffix.lower()


def original_path(pf):
    return project_dir(pf.project_id) / f"{pf.id}_original{file_ext(pf)}"


def protected_path(pf):
    return project_dir(pf.project_id) / f"{pf.id}_protected{file_ext(pf)}"


def preview_path(pf, source):
    name = ("_original_preview.pdf" if source == "original"
            else "_preview.pdf")
    return project_dir(pf.project_id) / f"{pf.id}{name}"


def ocr_cache_path(pf):
    return project_dir(pf.project_id) / f"{pf.id}_ocr.json"


def text_cache_path(pf):
    """Cache del testo redigibile dell'originale (vedi `stale_leaks`). Sta
    accanto all'originale, che quel testo lo contiene già tutto: non è
    un'esposizione nuova, è la stessa cartella."""
    return project_dir(pf.project_id) / f"{pf.id}_text.json"


def model_path(project, pf):
    """Il file che il MODELLO vede nelle chat del progetto: la copia protetta
    nei progetti anonimizzati, l'originale in quelli in chiaro."""
    return protected_path(pf) if project.anonymized else original_path(pf)


def _png_key(pf):
    # chiave nella cache PNG (png_cache): il prefisso evita collisioni con
    # le chiavi delle altre fonti (es. "stage:" dell'anteprima chat)
    return f"proj:{pf.id}"


def _load_ocr_cache(pf):
    p = ocr_cache_path(pf)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- Regole (chi può fare cosa, quando) --------------------------------------

def project_has_messages(session, project_id):
    """True se QUALCHE chat del progetto ha già inviato messaggi: da quel
    momento i placeholder sono partiti verso il modello e le modifiche
    DISTRUTTIVE (deanonimizza, rimozione sigilli) non sono più ammesse —
    restano solo le operazioni additive."""
    return (session.query(ChatMessage.id)
            .join(Conversation, ChatMessage.conv_id == Conversation.id)
            .filter(Conversation.project_id == project_id)
            .first() is not None)


# --- Descrittori ---------------------------------------------------------------

def _file_placeholders(pf):
    """I placeholder che compaiono in QUESTO file (dai box e dal report):
    servono a filtrare il registro del progetto nella mappa del viewer."""
    phs = set()
    for raw in (pf.original_boxes_json, pf.anonymized_boxes_json):
        try:
            pages = json.loads(raw or "{}")
        except ValueError:
            pages = {}
        for blist in pages.values():
            phs.update(b.get("ph") for b in blist if b.get("ph"))
    try:
        report = json.loads(pf.report_json or "{}")
    except ValueError:
        report = {}
    phs.update(ph for ph, n in (report.get("by_placeholder") or {}).items()
               if n)
    phs.discard(None)
    return phs


def descriptor(session, project, pf):
    """Il descrittore del file nella forma che DocumentViewer consuma, con la
    mappa = registro del progetto filtrato ai placeholder di questo file."""
    mapping = {}
    if project.anonymized:
        canonical = ca.conversation_mapping(session, project.id)
        phs = _file_placeholders(pf)
        # + voci locali della cache OCR ([UNREADABLE_n]/[SIGNATURE_n]), che
        # il registro non ospita: il popup del box ne mostra il valore
        local = (image_ocr.unreadable_mapping(_load_ocr_cache(pf))
                 if any(image_ocr.is_local(ph) for ph in phs) else {})
        mapping = {ph: (canonical[ph] if ph in canonical else local[ph])
                   for ph in sorted(phs) if ph in canonical or ph in local}
    d = pf.descriptor(mapping=mapping)
    d["original_available"] = original_path(pf).is_file()
    return d


# --- Elaborazione (progetti anonimizzati: dal worker dei job) -----------------

def _next_model_name(session, project_id, ext):
    """Nome neutro progressivo verso modello e sandbox: `progetto_NN.ext`.
    Il prefisso è diverso da quello degli allegati chat (`allegato_NN`),
    quindi nessuna collisione nel dizionario file della sandbox."""
    n = (session.query(ProjectFile)
         .filter_by(project_id=project_id).count()) + 1
    return f"progetto_{n:02d}{ext}"


def _write_previews(pf, preview_orig, preview_anon):
    d = project_dir(pf.project_id)
    d.mkdir(parents=True, exist_ok=True)
    preview_path(pf, "original").write_bytes(preview_orig)
    preview_path(pf, "anonymized").write_bytes(preview_anon)
    png_cache.drop_pngs(_png_key(pf))


def _apply_result(session, project, pf, ext, original_data, out, report,
                  stats, mapping, exact_phs, ocr_cache):
    """Persiste l'esito di una (ri)redazione: file protetto, cache OCR,
    anteprime, box, report e schede. Fattorizzato tra prima elaborazione e
    rebuild: i due percorsi devono produrre ESATTAMENTE le stesse cose."""
    pdir = project_dir(project.id)
    pdir.mkdir(parents=True, exist_ok=True)
    protected_path(pf).write_bytes(out)
    cache_path = ocr_cache_path(pf)
    if ocr_cache:
        cache_path.write_text(json.dumps(ocr_cache, ensure_ascii=False),
                              encoding="utf-8")
    else:
        cache_path.unlink(missing_ok=True)

    # anteprime + box (stessa pipeline dell'anteprima pre-invio della chat)
    report_boxes = (report.pop("boxes", None)
                    if ext == ".pdf" or ext in ca._IMAGE_EXTS else None)
    text = _redigible_text(original_data, ext, ocr_cache)
    preview_orig, preview_anon, info = chat_staging.build_preview(
        ext, original_data, out, mapping.for_text(text).items(),
        dict(mapping), exact_phs, report_boxes, ocr_cache)
    _write_previews(pf, preview_orig, preview_anon)

    turn_report = ca._turn_report(
        {"report": report, "stats": stats}, mapping)
    turn_report.pop("boxes", None)
    turn_report["preview_truncated"] = info["preview_truncated"]

    # scheda della copia protetta: è quella che vedrà il modello
    model_name = str(Path(pf.model_filename
                          or f"progetto{ext}").with_suffix(ext))
    card = briefing.describe(str(protected_path(pf)), model_name,
                             mimetypes.guess_type(model_name)[0])

    pf.model_filename = model_name
    pf.model_briefing_json = json.dumps(card, ensure_ascii=False)
    pf.report_json = json.dumps(turn_report, ensure_ascii=False)
    pf.by_label_json = json.dumps(stats.get("by_label") or {},
                                  ensure_ascii=False)
    pf.original_boxes_json = json.dumps(info["original_boxes"],
                                        ensure_ascii=False)
    pf.anonymized_boxes_json = json.dumps(info["anonymized_boxes"],
                                          ensure_ascii=False)
    pf.page_sizes_json = json.dumps(info["page_sizes"])
    pf.n_pages = len(info["page_sizes"]["original"])
    pf.mapping_version = project.mapping_version or 0
    pf.rev = (pf.rev or 0) + 1
    # scritto col registro corrente: allineato per definizione, e il verdetto
    # vecchio non vale più. Il testo per i controlli futuri è quello che
    # abbiamo già qui: la cache costa una write invece di una riapertura.
    pf.stale_version = -1
    pf.stale_json = "[]"
    _write_text_cache(pf, text)


def _redigible_text(data, ext, ocr_cache):
    """Tutto il testo redigibile del file + il corpus OCR: è ciò che
    mapping.for_text deve vedere per decidere le varianti dedotte. La regola
    è UNA sola per progetti e chat (chat_staging.redigible_text): quando le
    due copie sono divergute, la chat rifiutava di anonimizzare un valore
    letto nelle immagini che il progetto accettava."""
    return chat_staging.redigible_text(data, ext, ocr_cache)


def _write_text_cache(pf, text):
    """Fotografa il testo redigibile alla `rev` corrente. La scrive chi ha
    già il testo in mano (una (ri)redazione lo calcola comunque), così il
    controllo di allineamento non deve riaprire il file."""
    try:
        text_cache_path(pf).write_text(
            json.dumps({"rev": pf.rev or 0, "text": text},
                       ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass          # la cache è un'ottimizzazione: si ricalcola


def _read_text_cache(pf):
    """Il testo della `rev` corrente, o None se manca / è di una rev vecchia."""
    p = text_cache_path(pf)
    if not p.is_file():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict) or blob.get("rev") != (pf.rev or 0):
        return None
    return blob.get("text") or ""


def process_upload(data, name, project_id, engine, session, ctl=None,
                   ocr=False):
    """Elabora un file per un progetto ANONIMIZZATO (dal worker dei job):
    rilevamento sul registro del progetto (pass A) + redazione col registro
    finale (pass B), sotto il lock del progetto — un turno di chat non può
    intrecciarsi con questa allocazione. Ritorna il ProjectFile committato."""
    with ca.conversation_lock(project_id):
        project = session.get(Project, project_id)
        if project is None:
            raise ProjectFileError(410, "Progetto eliminato nel frattempo.")
        defaults = ca.anon_options(session, project)
        skip = ca.excluded_groups(defaults["excluded_tags"])
        conv_engine = ca.ConversationEngine(engine, session, project)
        try:
            data, ext = ca._converted_input(data, name)
        except Exception as exc:
            raise ProjectFileError(422, f"{name}: {exc}")
        if not name.lower().endswith(ext):
            # l'output è nel formato convertito e il nome lo dichiara
            name = name.rsplit(".", 1)[0] + ext

        try:
            found = ca._detect(
                data, ext, conv_engine, defaults, ctl=ctl,
                xlsx_max_chunks=settings_store.get_int(session,
                                                       "xlsx_max_chunks"),
                ocr=ocr)
        except (ValueError, PdfError, NotImplementedError) as exc:
            session.rollback()
            raise ProjectFileError(422, f"{name}: {exc}")
        session.flush()
        mapping = ca.conversation_mapping(session, project.id,
                                          include_aliases=True, exclude=skip)

        if found["file"] is not None:
            # xlsx: già redatto nel pass A col registro di adesso (sotto lock
            # non è cambiato niente da allora): rifarlo darebbe gli stessi byte
            out, report = found["file"], found["report"]
        else:
            out, report = ca._redact_with(
                data, ext, mapping.for_text(found["text"]),
                exact_phs=found["exact_phs"], ctl=ctl,
                ocr_cache=found.get("ocr_cache"))
        if report.get("residual"):
            session.rollback()
            raise ProjectFileError(
                422, f"{name}: verifica dei residui fallita, il file non "
                     "viene dichiarato protetto.")

        pf = ProjectFile(
            project_id=project.id, filename=name,
            model_filename=_next_model_name(session, project.id, ext),
            mime=mimetypes.guess_type(name)[0], size=len(data),
            n_images=image_ocr.count_images(data, ext))
        session.add(pf)
        session.flush()
        pdir = project_dir(project.id)
        pdir.mkdir(parents=True, exist_ok=True)
        original_path(pf).write_bytes(data)
        card = briefing.describe(str(original_path(pf)), name, pf.mime)
        pf.briefing_json = json.dumps(card, ensure_ascii=False)
        _apply_result(session, project, pf, ext, data, out, report,
                      found["stats"], mapping, found["exact_phs"],
                      found.get("ocr_cache"))
        session.commit()
        return pf


def save_clear(session, project, data, filename, mime):
    """Progetto IN CHIARO: il file si salva com'è. Niente registro, niente
    anteprima; visibile alle chat da subito (non c'è nulla da rivedere)."""
    ext = Path(filename).suffix.lower()
    pf = ProjectFile(
        project_id=project.id, filename=filename,
        model_filename=_next_model_name(session, project.id, ext),
        mime=mime or mimetypes.guess_type(filename)[0], size=len(data),
        n_images=image_ocr.count_images(data, ext), confirmed=1)
    session.add(pf)
    session.flush()
    pdir = project_dir(project.id)
    pdir.mkdir(parents=True, exist_ok=True)
    original_path(pf).write_bytes(data)
    card = briefing.describe(str(original_path(pf)), filename, pf.mime)
    pf.briefing_json = json.dumps(card, ensure_ascii=False)
    session.commit()
    return pf


# --- Ri-redazione (rebuild) ---------------------------------------------------

def rebuild_file(session, project, pf, ctl=None):
    """Ri-redige QUESTO file dall'originale col registro CORRENTE (sigilli e
    cache OCR riapplicati) e rigenera anteprime, box e schede. Da chiamare
    sotto ca.conversation_lock(project.id). Non tocca gli altri file: ognuno
    resta fotografato alla versione a cui è stato scritto."""
    orig_p = original_path(pf)
    if not orig_p.is_file():
        raise ProjectFileError(410, f"{pf.filename}: l'originale non è più "
                                    "disponibile sul server.")
    data = orig_p.read_bytes()
    ext = file_ext(pf)
    defaults = ca.anon_options(session, project)
    skip = ca.excluded_groups(defaults["excluded_tags"])
    mapping = ca.conversation_mapping(session, project.id,
                                      include_aliases=True, exclude=skip)
    ocr_cache = _load_ocr_cache(pf)
    try:
        report = json.loads(pf.report_json or "{}")
    except ValueError:
        report = {}
    exact_phs = set(report.get("table_phs") or ())
    text = _redigible_text(data, ext, ocr_cache)
    try:
        sealed = json.loads(pf.sealed_json or "[]")
    except ValueError:
        sealed = []
    try:
        out, new_report = ca._redact_with(
            data, ext, mapping.for_text(text), exact_phs=exact_phs, ctl=ctl,
            ocr_cache=ocr_cache, sealed=sealed)
    except Exception as exc:
        raise ProjectFileError(422, f"{pf.filename}: "
                                    + (str(exc) or exc.__class__.__name__))
    if new_report.get("residual"):
        raise ProjectFileError(422, f"{pf.filename}: verifica dei residui "
                                    "fallita, la modifica non è stata "
                                    "applicata.")
    # le informazioni tabellari (colonne anonimizzate per intero) vivono nel
    # report e devono sopravvivere alla ri-redazione
    for key in ("table_phs", "table_columns", "header_kept"):
        if report.get(key) and not new_report.get(key):
            new_report[key] = report[key]
    _apply_result(session, project, pf, ext, data, out, new_report,
                  chat_staging._report_stats(new_report), mapping, exact_phs,
                  ocr_cache)


def reprocess_ocr(params, engine, session, ctl=None):
    """Rielabora un file CON l'OCR attivo (job kind=project_reprocess, dal
    worker): rifà il rilevamento dall'originale leggendo anche le immagini
    (stesso pass A dell'upload, stavolta con ocr=True) e ri-redige col
    registro aggiornato. Operazione ADDITIVA: il registro può solo imparare;
    sigilli e colonne tabellari sopravvivono. È il rimedio per i file
    caricati con l'OCR spento, dove la selezione manuale legge testo nelle
    immagini ma non c'è una cache su cui disegnare i box."""
    with ca.conversation_lock(params["project_id"]):
        project = session.get(Project, params["project_id"])
        pf = session.get(ProjectFile, params["file_id"])
        if project is None or pf is None or pf.project_id != project.id:
            raise ProjectFileError(410, "File eliminato nel frattempo.")
        if not project.anonymized:
            raise ProjectFileError(422, "L'OCR esiste solo nei progetti "
                                        "anonimizzati.",
                                   "anon_only_ocr")
        orig_p = original_path(pf)
        if not orig_p.is_file():
            raise ProjectFileError(410, f"{pf.filename}: l'originale non è "
                                        "più disponibile sul server.")
        data = orig_p.read_bytes()
        ext = file_ext(pf)
        defaults = ca.anon_options(session, project)
        skip = ca.excluded_groups(defaults["excluded_tags"])
        conv_engine = ca.ConversationEngine(engine, session, project)
        try:
            found = ca._detect(
                data, ext, conv_engine, defaults, ctl=ctl,
                xlsx_max_chunks=settings_store.get_int(session,
                                                       "xlsx_max_chunks"),
                ocr=True)
        except (ValueError, PdfError, NotImplementedError) as exc:
            session.rollback()
            raise ProjectFileError(422, f"{pf.filename}: {exc}")
        session.flush()
        mapping = ca.conversation_mapping(session, project.id,
                                          include_aliases=True, exclude=skip)
        try:
            old_report = json.loads(pf.report_json or "{}")
        except ValueError:
            old_report = {}
        try:
            sealed = json.loads(pf.sealed_json or "[]")
        except ValueError:
            sealed = []
        exact_phs = set(found["exact_phs"]) \
            | set(old_report.get("table_phs") or ())
        if found["file"] is not None:
            # xlsx: già redatto nel pass A col registro di adesso (i sigilli
            # su xlsx non esistono: seal_area li rifiuta)
            out, report = found["file"], found["report"]
        else:
            text = found["text"]
            if text is None:
                text = _redigible_text(data, ext, found.get("ocr_cache"))
            out, report = ca._redact_with(
                data, ext, mapping.for_text(text), exact_phs=exact_phs,
                ctl=ctl, ocr_cache=found.get("ocr_cache"), sealed=sealed)
        if report.get("residual"):
            session.rollback()
            raise ProjectFileError(
                422, f"{pf.filename}: verifica dei residui fallita, il file "
                     "non viene dichiarato protetto.")
        for key in ("table_phs", "table_columns", "header_kept"):
            if old_report.get(key) and not report.get(key):
                report[key] = old_report[key]
        _apply_result(session, project, pf, ext, data, out, report,
                      found["stats"], mapping, exact_phs,
                      found.get("ocr_cache"))
        session.commit()
        return pf


# --- Modifiche dalla preview ----------------------------------------------------

def deanonymize(session, project, pf, placeholder=None, label=None):
    """Lascia in chiaro un valore (o una categoria di QUESTO file): l'entità
    resta nel registro come `excluded` — i placeholder già usati altrove si
    decodificano per sempre — e il file viene ri-redatto. NOTA dichiarata in
    UI: l'esclusione vale per tutto il PROGETTO (registro condiviso), qui e
    nei turni futuri di ogni chat."""
    if image_ocr.is_local(placeholder) or image_ocr.local_label(label):
        # [UNREADABLE_n]/[SIGNATURE_n]: voci della cache OCR di QUESTO file,
        # non del registro — l'esclusione si scrive nella cache
        cache = _load_ocr_cache(pf)
        removed = image_ocr.exclude_local(cache, placeholder=placeholder,
                                          label=label) if cache else []
        if not removed:
            raise ProjectFileError(404, "Segnaposto non presente nella mappa.")
        ocr_cache_path(pf).write_text(json.dumps(cache, ensure_ascii=False),
                                      encoding="utf-8")
        rebuild_file(session, project, pf)
        return removed
    entities = (session.query(ConversationEntity)
                .filter_by(conv_id=project.id).all())
    if placeholder:
        targets = [e for e in entities
                   if e.placeholder == placeholder and not e.excluded]
    elif label:
        group = ca._label_group(label)
        phs = _file_placeholders(pf)
        targets = [e for e in entities
                   if not e.excluded and not e.merged_into
                   and ca._label_group(e.label) == group
                   and e.placeholder in phs]
    else:
        raise ProjectFileError(422, "Indica un segnaposto (placeholder) o "
                                    "una categoria (label).")
    if not targets:
        raise ProjectFileError(404, "Segnaposto non presente nella mappa.")
    for e in targets:
        e.excluded = 1
    session.flush()
    rebuild_file(session, project, pf)
    return [e.placeholder for e in targets]


def anonymize_text(session, project, pf, text, tag="CUSTOM"):
    """Anonimizza un testo scelto dall'utente come [TAG_n] nel REGISTRO del
    progetto (vale per tutti i file futuri e per le chat) e ri-redige questo
    file. Se il valore era stato escluso, lo RI-include.

    `tag` è il nome scelto nel popup di selezione manuale quando si spunta la
    casella `viewer.selection.saveTerm` (già normalizzato da
    settings_store.clean_tag), CUSTOM altrimenti: il segnaposto
    che l'utente legge nell'anteprima deve essere quello che ha scritto lui, o
    chiedergli un tag non avrebbe senso. I contatori sono per prefisso, come
    quelli del motore (ConversationEngine.analyze), quindi [TAG_n] non collide
    con i placeholder allocati dal rilevatore."""
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        raise ProjectFileError(422, "Selezione vuota.")
    canonical = ca.conversation_mapping(session, project.id)
    if any(ph in value for ph in canonical):
        raise ProjectFileError(422, "La selezione contiene un segnaposto: "
                                    "seleziona solo testo in chiaro.")
    if not ca._searchable(value):
        raise ProjectFileError(
            422, "Testo troppo corto o ambiguo per essere anonimizzato in "
                 "modo sicuro in un progetto (minimo 4 caratteri "
                 "alfanumerici, e niente frammenti che compaiono dentro "
                 "altre parole).")
    entities, aliases = ca._registry_rows(session, project.id)
    hit = next((e for e in entities
                if _norm(e.canonical_value) == _norm(value)), None)
    if hit is None:
        hit = next((e for a, e in aliases
                    if _norm(a.original_surface) == _norm(value)), None)
    if hit is not None and not hit.excluded:
        raise ProjectFileError(409, f"Questo valore è già anonimizzato "
                                    f"come {hit.placeholder}.")
    cache = _load_ocr_cache(pf) if hit is None else None
    local_ph = (image_ocr.include_local(cache, value)
                if cache and cache.get("excluded") else None)
    if hit is not None:
        hit.excluded = 0
        new_ph = hit.placeholder
    elif local_ph is not None:
        # riga [UNREADABLE_n] deanonimizzata e riselezionata: si ricopre,
        # senza allocare un [CUSTOM_n] con la lettura storpiata nel registro
        ocr_cache_path(pf).write_text(json.dumps(cache, ensure_ascii=False),
                                      encoding="utf-8")
        new_ph = local_ph
    else:
        orig_p = original_path(pf)
        if not orig_p.is_file():
            raise ProjectFileError(410, f"{pf.filename}: l'originale non è "
                                        "più disponibile sul server.")
        doc_text = _redigible_text(orig_p.read_bytes(), file_ext(pf),
                                   _load_ocr_cache(pf))
        pattern = ca._pattern(value)
        if not (pattern and pattern.search(doc_text)):
            raise ProjectFileError(422, "Il testo selezionato non risulta "
                                        "nel file (prova a selezionare "
                                        "parole intere).")
        label = tag or "CUSTOM"
        nums = [int(m.group(1)) for e in entities
                if (m := re.fullmatch(rf"\[{re.escape(label)}_(\d+)\]",
                                      e.placeholder))]
        new_ph = f"[{label}_{max(nums, default=0) + 1}]"
        project.mapping_version = (project.mapping_version or 0) + 1
        entity = ConversationEntity(
            conv_id=project.id, placeholder=new_ph, label=label,
            canonical_value=value, mapping_version=project.mapping_version)
        session.add(entity)
        session.flush()
        project.mapping_version = (project.mapping_version or 0) + 1
        session.add(ConversationEntityAlias(
            entity_id=entity.id, original_surface=value,
            normalized_key=ca._surface_key(label, value),
            source="user", confidence="exact",
            mapping_version=project.mapping_version))
    session.flush()
    rebuild_file(session, project, pf)
    return new_ph


def seal_area(session, project, pf, page, rect, all_pages=False):
    """SIGILLA un'area della preview anonimizzata (solo PDF/immagini): il
    contenuto sotto il rettangolo viene RIMOSSO dalla copia protetta."""
    ext = file_ext(pf)
    if ext != ".pdf" and ext not in ca._IMAGE_EXTS:
        raise ProjectFileError(422, "Il sigillo di un'area vale solo per PDF "
                                    "e immagini: negli altri formati "
                                    "l'anteprima non corrisponde "
                                    "geometricamente al file.")
    if len(rect) != 4:
        raise ProjectFileError(422, "Rettangolo non valido.", "rect_invalid")
    sizes = json.loads(pf.page_sizes_json or "[]")
    if isinstance(sizes, dict):
        sizes = sizes.get("anonymized") or []
    if not (0 <= page < len(sizes)):
        raise ProjectFileError(422, "Pagina fuori range.")
    x0, x1 = sorted((float(rect[0]), float(rect[2])))
    y0, y1 = sorted((float(rect[1]), float(rect[3])))
    x0, x1 = max(0.0, x0), min(float(sizes[page]["width"]), x1)
    y0, y1 = max(0.0, y0), min(float(sizes[page]["height"]), y1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise ProjectFileError(422, "Area troppo piccola per essere "
                                    "sigillata.")
    sealed = json.loads(pf.sealed_json or "[]")
    n = max((s["n"] for s in sealed), default=0) + 1
    sealed.append({"n": n, "page": page,
                   "all": bool(all_pages) and ext == ".pdf",
                   "rect": [x0, y0, x1, y1]})
    pf.sealed_json = json.dumps(sealed)
    session.flush()
    rebuild_file(session, project, pf)
    return n


def remove_seal(session, project, pf, n):
    sealed = json.loads(pf.sealed_json or "[]")
    kept = [s for s in sealed if s["n"] != n]
    if len(kept) == len(sealed):
        raise ProjectFileError(404, "Area sigillata non trovata.")
    pf.sealed_json = json.dumps(kept)
    session.flush()
    rebuild_file(session, project, pf)
    return n


def extract(pf, source, page, rect):
    """Testo sotto un rettangolo della preview (selezione manuale): layer
    testuale, poi cache OCR del file, poi OCR del ritaglio — stesso contratto
    e stessa funzione di chat_staging.extract (vedi lì per i flag `ocr`,
    `ocr_redactable`, `ocr_cache`)."""
    p = preview_path(pf, source)
    if not p.is_file():
        raise ProjectFileError(404, "Anteprima non disponibile.")
    data = p.read_bytes()
    text = text_in_rect(data, page, rect)
    if text:
        return {"text": text, "ocr": False, "ocr_tried": False}
    return chat_staging.ocr_extract(data, page, rect, _load_ocr_cache(pf))


def page_png(pf, source, n):
    key = _png_key(pf)
    entry = png_cache.get_png(key, source, n)
    if entry is not None:
        return entry
    p = preview_path(pf, source)
    if not p.is_file():
        return None
    entry = render_page_png(p.read_bytes(), n)
    if entry is not None:
        png_cache.cache_png(key, source, n, entry)
    return entry


# --- Colonne xlsx (layout cliccabile + azioni di colonna) ----------------------

def _sheet_names(pf):
    data = original_path(pf).read_bytes() if original_path(pf).is_file() \
        else None
    if data is None:
        return []
    try:
        return sheet_names(data)
    except XlsxError:
        return []


def columns_layout(pf):
    out = {"original": {}, "anonymized": {}}
    if file_ext(pf) != ".xlsx":
        return out
    names = _sheet_names(pf)
    if not names:
        return out
    for source in ("original", "anonymized"):
        p = preview_path(pf, source)
        if p.is_file():
            out[source] = spreadsheet_columns(p.read_bytes(), names)
    return out


def column_info(session, project, pf, sheet, column):
    from .engine.xlsx_table import usable_value
    raw_p = original_path(pf)
    if not raw_p.is_file():
        raise ProjectFileError(410, "Originale non disponibile: ricarica "
                                    "il file.",
                               "original_gone_reupload")
    try:
        vals, label = column_values(raw_p.read_bytes(), sheet, column)
    except XlsxError as e:
        raise ProjectFileError(422, str(e))
    canonical = ca.conversation_mapping(session, project.id)
    colset = {_norm(v) for v in vals}
    return {"values": len(vals),
            "usable": sum(1 for v in vals if usable_value(v)),
            "mapped": sum(1 for v in canonical.values() if _norm(v) in colset),
            "header": label}


def process_column(params, session, ctl=None):
    """Anonimizzazione manuale di una colonna xlsx di un file di progetto
    (job kind=project_column): deterministica (ogni valore distinto usabile
    entra in mappa, nessuna inferenza) e i valori nuovi entrano nel REGISTRO
    del progetto. Sotto il lock del progetto per tutta la durata."""
    with ca.conversation_lock(params["project_id"]):
        project = session.get(Project, params["project_id"])
        pf = session.get(ProjectFile, params["file_id"])
        if project is None or pf is None or pf.project_id != project.id:
            raise XlsxError("File eliminato nel frattempo.")
        raw_p = original_path(pf)
        if not raw_p.is_file():
            raise XlsxError("Originale non disponibile: ricarica il file.")
        mapping = dict(ca.conversation_mapping(session, project.id))
        res = anonymize_xlsx_column(raw_p.read_bytes(), params["sheet"],
                                    params["column"], mapping, ctl=ctl)
        if not res["new_placeholders"]:
            if res["existing"]:
                raise XlsxError(f"La colonna risulta già anonimizzata: "
                                f"{res['existing']} valori sono già in mappa "
                                f"e non c'è nulla di nuovo da coprire.")
            raise XlsxError(f"Nessun valore anonimizzabile nella colonna: "
                            f"{res['skipped']} valori sono troppo corti o "
                            f"ambigui per essere redatti in modo sicuro.")
        new_pairs = {ph: mapping[ph] for ph in res["new_placeholders"]
                     if ph in mapping}
        ca.sync_allocated_mapping(session, project, new_pairs)
        session.flush()
        # colonne e placeholder "exact" del percorso tabellare: nel report,
        # così le ri-redazioni successive li trattano da sostituzione esatta
        try:
            report = json.loads(pf.report_json or "{}")
        except ValueError:
            report = {}
        report["table_phs"] = sorted(set(report.get("table_phs") or ())
                                     | set(res["exact_new"] or ()))
        if res.get("table_column"):
            cols = list(report.get("table_columns") or ())
            if res["table_column"] not in cols:
                cols.append(res["table_column"])
            report["table_columns"] = cols
        pf.report_json = json.dumps(report, ensure_ascii=False)
        session.flush()
        try:
            rebuild_file(session, project, pf, ctl=ctl)
        except ProjectFileError as e:
            raise XlsxError(str(e))
        session.commit()
        return pf


def deanonymize_column(session, project, pf, sheet, column):
    """Esclude dal registro tutti i valori (come cella intera) della colonna:
    tornano in chiaro in questo file (dopo il rebuild) e nei turni futuri.
    Come per i documenti: un valore condiviso con altre colonne torna in
    chiaro OVUNQUE."""
    raw_p = original_path(pf)
    if not raw_p.is_file():
        raise ProjectFileError(410, "Originale non disponibile: ricarica "
                                    "il file.",
                               "original_gone_reupload")
    try:
        vals, _label = column_values(raw_p.read_bytes(), sheet, column)
    except XlsxError as e:
        raise ProjectFileError(422, str(e))
    colset = {_norm(v) for v in vals}
    entities = (session.query(ConversationEntity)
                .filter_by(conv_id=project.id).all())
    targets = [e for e in entities
               if not e.excluded and _norm(e.canonical_value) in colset]
    if not targets:
        raise ProjectFileError(404, "Nessun valore di questa colonna è in "
                                    "mappa: non c'è nulla da deanonimizzare.")
    for e in targets:
        e.excluded = 1
    session.flush()
    rebuild_file(session, project, pf)
    return [e.placeholder for e in targets]


# --- Allineamento alla mappa corrente ------------------------------------------

# Oltre questa taglia il testo di un file MAI verificato non si estrae dentro
# una GET: il progetto si aprirebbe dopo secondi (un xlsx da 4 MB produce ~8 MB
# di testo in 11 s). Quei file restano senza verifica (`stale_json` NULL) e li
# controlla la route dedicata, dove l'utente sa di stare aspettando.
_INLINE_EXTRACT_MAX = 4 * 1024 * 1024


def _load_stale(pf):
    try:
        rows = json.loads(pf.stale_json or "[]")
    except ValueError:
        return []
    return [(r[0], r[1]) for r in rows
            if isinstance(r, (list, tuple)) and len(r) == 2]


def _text_for_check(pf, inline=True):
    """Il testo su cui cercare le superfici nuove: dalla cache se c'è, dal
    file altrimenti (e allora la cache si scrive). `inline` = siamo dentro una
    richiesta che deve restare svelta, quindi vale il tetto; None in uscita =
    non si può rispondere adesso, il file è troppo grosso da leggere."""
    text = _read_text_cache(pf)
    if text is not None:
        return text
    raw_p = original_path(pf)
    if not raw_p.is_file():
        return ""
    if inline and raw_p.stat().st_size > _INLINE_EXTRACT_MAX:
        return None
    text = _redigible_text(raw_p.read_bytes(), file_ext(pf),
                           _load_ocr_cache(pf))
    _write_text_cache(pf, text)
    return text


def stale_leaks(session, project, pf, inline=True):
    """Le superfici che il registro ha imparato DOPO la scrittura di questo
    file e che il file contiene ancora in chiaro. È il caso del file A
    caricato ieri, dove "Pippo" era una parola qualunque, e del file B
    caricato oggi, che ha insegnato al registro che "Pippo" è un nome: A
    resta indietro, e finché non lo si ri-redige le chat vedono lo stesso
    valore protetto in un file e in chiaro nell'altro.

    Costa quasi niente perché si ferma alla prima domanda utile:
      - versione del file >= versione del progetto: allineato, zero lavoro;
      - verdetto già calcolato a questa versione: si rilegge dalla riga;
      - altrimenti si cercano SOLO le superfici imparate dall'ultimo controllo
        in qua (due o tre), sul testo già in cache.

    Si guarda l'ORIGINALE, non la copia protetta: una superficie imparata dopo
    la scrittura non può essere stata redatta qui, quindi la risposta è la
    stessa, il testo è quello che ogni redazione calcola comunque, e con
    l'OCR è l'unico modo di leggere le scansioni (nella copia protetta sono
    pixel). Il prezzo è un possibile falso positivo su un valore finito sotto
    un'area sigillata: costa una ri-redazione inutile, dopo la quale il file
    risulta allineato.

    Ritorna [(placeholder, superficie)] — vuota se allineato — oppure None se
    il controllo non è stato fatto (`inline` e file troppo grosso). NON
    committa: lo fa il chiamante."""
    cur = project.mapping_version or 0
    written = pf.mapping_version or 0
    if not project.anonymized or written >= cur:
        return []
    checked = pf.stale_version if pf.stale_version is not None else -1
    known = _load_stale(pf)
    if checked == cur:
        return known
    text = _text_for_check(pf, inline=inline)
    if text is None:
        return None
    # dall'ultimo controllo in qua: le superfici già scartate non cambiano
    # idea, e il costo resta proporzionale a ciò che è cambiato davvero
    since = max(written, checked) if checked >= 0 else written
    defaults = ca.anon_options(session, project)
    skip = ca.excluded_groups(defaults["excluded_tags"])
    fresh = ca.known_surface_leaks(session, project.id, text, exclude=skip,
                                   since_version=since)
    leaks = sorted({**dict(known), **dict(fresh)}.items())
    pf.stale_version = cur
    pf.stale_json = json.dumps(leaks, ensure_ascii=False)
    return leaks


def realign(params, session, ctl=None):
    """Ri-redige il file col registro corrente (job kind=project_realign, dal
    worker). È la risposta all'avviso prodotto da `stale_leaks`, ed è
    un job perché su un file grosso la redazione dura quanto quella
    dell'upload. Operazione ADDITIVA (il registro può solo aver imparato,
    mai dimenticato), quindi ammessa anche sui file già confermati e dopo il
    primo messaggio: da qui in avanti il modello vedrà protetto un valore che
    nei turni passati aveva letto in chiaro — che è esattamente lo scopo."""
    with ca.conversation_lock(params["project_id"]):
        project = session.get(Project, params["project_id"])
        pf = session.get(ProjectFile, params["file_id"])
        if project is None or pf is None or pf.project_id != project.id:
            raise ProjectFileError(410, "File eliminato nel frattempo.")
        if not project.anonymized:
            raise ProjectFileError(422, "Il riallineamento esiste solo nei "
                                        "progetti anonimizzati.",
                                   "anon_only_realign")
        rebuild_file(session, project, pf, ctl=ctl)
        session.commit()
        return pf


# --- Conferma (il file diventa visibile alle chat) -----------------------------

def confirm(session, project, pf):
    """Controllo di uscita + conferma. Il file non è MAI stato esposto prima
    d'ora, quindi qui si può (e si deve) essere severi: se il registro ha
    imparato — da altri file o dalle chat — superfici che questa copia
    protetta contiene ancora in chiaro, si blocca e si suggerisce il
    riallineamento, che per un file non confermato è sempre lecito."""
    if pf.confirmed:
        return
    if project.anonymized:
        ppath = protected_path(pf)
        if not ppath.is_file():
            raise ProjectFileError(409, "Copia protetta non disponibile: "
                                        "rielabora il file.")
        defaults = ca.anon_options(session, project)
        skip = ca.excluded_groups(defaults["excluded_tags"])
        try:
            text = chat_staging._full_text(ppath.read_bytes(), file_ext(pf))
        except Exception:
            text = ""
        card = pf.model_briefing_json or ""
        # la copia protetta (testo estraibile) contro TUTTO il registro, più
        # il controllo di allineamento: il secondo è l'unico che vede dentro
        # le scansioni, dove il testo della copia protetta è fatto di pixel e
        # il primo non trova mai niente. Qui l'utente aspetta un'azione che ha
        # chiesto lui: si estrae senza tetto.
        leaks = (ca.known_surface_leaks(session, project.id, text,
                                        exclude=skip)
                 or ca.known_surface_leaks(session, project.id, card,
                                           exclude=skip)
                 or stale_leaks(session, project, pf, inline=False) or [])
        if leaks:
            names = ", ".join(f'"{v}" ({ph})' for ph, v in leaks[:3])
            raise ProjectFileError(
                409, "Il registro del progetto ha imparato valori che questo "
                     f"file contiene ancora in chiaro ({names}): usa "
                     "«Riallinea al registro» e poi conferma.")
    pf.confirmed = 1


# --- Esposizione alle chat del progetto ----------------------------------------

def confirmed_files(session, project_id):
    return (session.query(ProjectFile)
            .filter_by(project_id=project_id, confirmed=1)
            .order_by(ProjectFile.created_at).all())


def chat_files(session, project):
    """{nome neutro: percorso host} dei file CONFERMATI, per la sandbox.
    Un file mancante su disco viene segnalato dal chiamante (stesso errore
    fatale degli allegati)."""
    return {(pf.model_filename or pf.filename): model_path(project, pf)
            for pf in confirmed_files(session, project.id)}


def model_parts(session, project, modalities, max_bytes):
    """I file confermati come parti multimodali (immagini e PDF, le stesse
    regole degli allegati chat: briefing.as_model_part) più l'insieme dei
    nomi allegati davvero. Vanno nel messaggio sintetico subito dopo il
    system prompt: posizione e contenuto stabili finché l'elenco dei file
    confermati non cambia, quindi il prompt caching regge."""
    parts, shown = [], set()
    for pf in confirmed_files(session, project.id):
        name = pf.model_filename or pf.filename
        part = briefing.as_model_part(model_path(project, pf), name,
                                      pf.mime, modalities, max_bytes)
        if part is not None:
            parts.append(part)
            shown.add(name)
    return parts, shown


def files_block(session, project, shown=frozenset()):
    """Le schede dei file del progetto per il SYSTEM PROMPT delle sue chat.
    Cambia solo quando l'elenco dei file confermati cambia (a parità di
    modello: `shown` dipende dalle sue modalità): il prompt caching paga il
    cache-bust una volta per modifica, non a ogni turno."""
    rows = confirmed_files(session, project.id)
    if not rows:
        return None
    lines = ["<file di progetto>",
             "File già caricati nel progetto, disponibili in OGNI turno. "
             "Nella sandbox sono in /workspace/inputs (sola lettura) col "
             "nome esatto qui sotto."]
    for pf in rows:
        name = pf.model_filename or pf.filename
        card = pf.model_briefing() if project.anonymized else pf.briefing()
        path = model_path(project, pf)
        size = path.stat().st_size if path.is_file() else (pf.size or 0)
        line = briefing.as_prompt(name, size, card)
        # stessa riga degli allegati chat (_attachment_block): senza, il
        # modello assume che il file non contenga immagini
        if (pf.n_images or 0) and not (pf.mime or "").startswith("image/"):
            line += f"\n    immagini nel file: {pf.n_images}"
        if name in shown:
            line += ("\n    (allegato anche nel primo messaggio: puoi "
                     "guardarlo direttamente)")
        lines.append(line)
    lines.append("</file di progetto>")
    return "\n".join(lines)


# --- Cancellazioni --------------------------------------------------------------

def drop_file(pf):
    """Elimina i byte e le pagine renderizzate di un file (la riga la
    elimina il chiamante). Il registro NON si tocca: le entità possono
    essere già partite nelle chat."""
    for p in (original_path(pf), protected_path(pf),
              preview_path(pf, "original"), preview_path(pf, "anonymized"),
              ocr_cache_path(pf), text_cache_path(pf)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    png_cache.drop_pngs(_png_key(pf))


def drop_project_dir(project_id):
    shutil.rmtree(project_dir(project_id), ignore_errors=True)

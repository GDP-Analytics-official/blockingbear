"""Anteprima PRE-INVIO di un turno anonimizzato (chat).

"Anonimizza e mostra prima di inviare": il turno viene anonimizzato come
sempre (chat_anonymization.anonymize_turn — file protetti scritti, registro
aggiornato) ma NIENTE parte. In più si costruiscono, per ogni pezzo del turno
(ogni allegato + il messaggio), le anteprime confrontabili lato-a-lato:
due PDF renderizzabili (originale / anonimizzato) e i box delle entità,
calcolati con le stesse funzioni del motore (convert.to_pdf, pdf._value_boxes,
pdf._placeholder_boxes, truncate_for_preview).

Dall'anteprima l'utente può intervenire con la stessa grammatica dei
documenti:
  - "anonimizza in più" -> una entità [CUSTOM_n] nel REGISTRO della
    conversazione (vale anche per i turni futuri), o [TAG_n] se nel popup si
    è scelto un tag salvandolo tra i termini fissi;
  - "deanonimizza"       -> l'entità viene marcata `excluded` nel registro:
    da qui in poi resta in chiaro, ma il suo placeholder continua a
    decodificarsi per sempre (stesso principio delle categorie escluse).
Ogni modifica ri-redige TUTTO il turno con la mappa aggiornata (pass B di
anonymize_turn, stessa `_redact_with`) e rigenera le anteprime: ciò che si
vede è sempre esattamente ciò che partirà.

Lo stato è in RAM (un riavvio lo perde: l'invio da anteprima risponde 409 e
si ripete la preparazione); i PDF di anteprima stanno su disco in
data/chats/{conv}/staged/ e si buttano al discard o all'invio.
"""

import json
import mimetypes
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, jobs, png_cache, settings_store
from . import chat_anonymization as ca
from .config import DATA_DIR
from .db import Attachment, Conversation, ConversationEntity, \
    ConversationEntityAlias
from .engine import image_ocr
from .engine import pdf as pdf_mod
from .engine.convert import to_pdf
from .engine.core import _norm
from .engine.docx import extract_text as extract_docx
from .engine.pdf import extract_text as extract_pdf, render_page_png, \
    spreadsheet_columns, text_in_rect
from .engine.pdf_export import PdfError
from .engine.pptx import extract_text as extract_pptx
from .engine.txt import TEXT_EXTS, _preview_bytes as _txt_preview_bytes
from .engine.txt import extract_text as extract_txt
from .engine.xlsx import XlsxError, _norm_value, anonymize_xlsx_column, \
    column_values, sheet_names, truncate_for_preview
from .engine.xlsx import extract_text as extract_xlsx
from .openrouter import briefing

CHATS_DIR = DATA_DIR / "chats"

PROMPT_ITEM = "prompt"
PROMPT_LABEL = "Il tuo messaggio"

_staged = {}                 # conv_id -> stato del turno in anteprima
_guard = threading.Lock()    # protegge il dizionario, non i contenuti


class StagedEditError(Exception):
    """Errore user-facing con lo status HTTP suggerito (come RebuildError).

    `code` è la chiave di traduzione (app/errors.py), opzionale ma da mettere
    quando il testo è già in `common.json`: vedi ProjectFileError."""

    def __init__(self, status, msg, code=None):
        super().__init__(msg)
        self.status = status
        self.code = code


def state(conv_id):
    return _staged.get(conv_id)


def _dir(conv_id):
    return CHATS_DIR / conv_id / "staged"


def _pdf_path(conv_id, item_id, source):
    return _dir(conv_id) / f"{item_id}_{source}.pdf"


def _cache_key(conv_id, item_id):
    # chiave nella cache PNG (png_cache): il prefisso evita collisioni con
    # le chiavi delle altre fonti (es. "proj:" dei file di progetto)
    return f"stage:{conv_id}:{item_id}"


def discard(conv_id):
    """Butta l'anteprima (stato + PDF su disco + cache PNG). I file PROTETTI
    degli allegati restano: sono roba del turno, non dell'anteprima."""
    with _guard:
        st = _staged.pop(conv_id, None)
    if st is not None:
        for item in st["items"]:
            png_cache.drop_pngs(_cache_key(conv_id, item["id"]))
    shutil.rmtree(_dir(conv_id), ignore_errors=True)
    return st is not None


# --- Anteprime (PDF renderizzabili + box, condivise coi file di progetto) ---

class _Pairs:
    """Espone .items() e l'appartenenza su una lista di coppie (ph,
    superficie): è tutto ciò che pdf._value_boxes e image_ocr.boxes_for
    chiedono alla mappa, e una lista conserva gli alias multipli dello stesso
    placeholder (un dict li perderebbe)."""

    def __init__(self, pairs):
        self._pairs = list(pairs)
        self._phs = {ph for ph, _v in self._pairs}

    def items(self):
        return list(self._pairs)

    def __contains__(self, ph):
        return ph in self._phs


def _pdf_pair(original_data, redacted_data, suffix):
    """I due lati dell'anteprima convertiti in PARALLELO: convert ha un pool
    di profili LibreOffice (uno per slot), quindi due conversioni convivono e
    l'attesa è quella della più lenta, non la somma. Un errore su un lato
    propaga la stessa ConvertError del percorso seriale."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        orig = ex.submit(to_pdf, original_data, suffix=suffix)
        anon = ex.submit(to_pdf, redacted_data, suffix=suffix)
        return orig.result(), anon.result()


def _label_boxes(*box_sets):
    for pages in box_sets:
        for blist in pages.values():
            for b in blist:
                b["label"] = pdf_mod._ph_label(b["ph"])


def build_preview(ext, original_data, redacted_data, repl_pairs, canonical,
                  exact_phs, report_boxes=None, ocr_cache=None):
    """La parte PURA dell'anteprima di un pezzo redatto (condivisa con i file
    di progetto, vedi project_files.py): converte originale e redatto in PDF
    renderizzabili e calcola i box delle entità sui due lati. Ritorna
    (preview_orig, preview_anon, info) dove info ha boxes/page_sizes/
    preview_truncated.

    ocr_cache: negli OOXML i box delle entità lette nelle IMMAGINI non si
    possono cercare nel testo del PDF convertito (stanno nei pixel) e vanno
    ritrovati a parte — vedi image_ocr.ooxml_media_overlay."""
    truncated = False
    ooxml_src = None            # i bytes DAVVERO convertiti, per lato
    if ext == ".pdf":
        preview_orig, preview_anon = original_data, redacted_data
    elif ext in ca._IMAGE_EXTS:
        # allegato immagine: la "pagina" è l'immagine stessa, incapsulata in
        # un PDF w x h PUNTI (1 px = 1 pt) — i box della redazione nei pixel
        # (report["boxes"], coordinate px) si sovrappongono così come sono
        preview_orig = image_ocr.image_to_pdf(original_data)
        preview_anon = image_ocr.image_to_pdf(redacted_data)
    elif ext == ".xlsx":
        trunc_orig, _ = truncate_for_preview(original_data)
        trunc_anon, truncated = truncate_for_preview(redacted_data)
        preview_orig, preview_anon = _pdf_pair(trunc_orig, trunc_anon, ".xlsx")
        ooxml_src = (trunc_orig, trunc_anon)
    elif ext in TEXT_EXTS:
        preview_orig, preview_anon = _pdf_pair(
            _txt_preview_bytes(original_data),
            _txt_preview_bytes(redacted_data), ".txt")
    else:                                   # .docx / .pptx
        preview_orig, preview_anon = _pdf_pair(original_data, redacted_data,
                                               ext)
        ooxml_src = (original_data, redacted_data)

    pairs = list(repl_pairs)
    if ext == ".xlsx" and exact_phs:
        # stesso filtro di rebuild_xlsx: ai placeholder "exact" del percorso
        # tabellare (mappe potenzialmente enormi) si chiede di comparire nelle
        # righe TRONCATE dell'anteprima prima di cercarli nel PDF
        exact_set = set(exact_phs)
        try:
            trunc_orig, _ = truncate_for_preview(original_data)
            segments = {_norm_value(seg)
                        for line in extract_xlsx(trunc_orig,
                                                 full=True).split("\n")
                        for seg in line.split(" | ")}
        except XlsxError:
            segments = set()
        pairs = [(ph, v) for ph, v in pairs
                 if ph not in exact_set or _norm_value(v) in segments]

    original_boxes = pdf_mod._value_boxes(preview_orig, _Pairs(pairs))
    if report_boxes is not None:
        anonymized_boxes = report_boxes
    else:
        anonymized_boxes = pdf_mod._placeholder_boxes(preview_anon, canonical)
    if ext in ca._IMAGE_EXTS and report_boxes:
        # niente layer testuale nell'immagine: a sinistra si mostrano gli
        # stessi rettangoli (il valore stava lì, l'immagine non si sposta).
        # I box SIGILLATI no: appartengono solo al lato anonimizzato
        original_boxes = {p: [dict(b) for b in bl if not b.get("sealed")]
                          for p, bl in report_boxes.items()}
        original_boxes = {p: bl for p, bl in original_boxes.items() if bl}
    if ocr_cache and ooxml_src:
        # le entità lette nelle immagini: i loro placeholder sono DIPINTI nei
        # pixel, quindi non stanno nel testo del PDF di anteprima. La mappa da
        # dare a boxes_for è esattamente quella della redazione — alias
        # compresi (li perderebbe un dict) e voci locali della cache
        # ([UNREADABLE_n], [SIGNATURE_n]), che il registro non ospita e che
        # _redact_plain rimette dentro allo stesso modo.
        ocr_map = _Pairs(list(repl_pairs)
                         + list(image_ocr.unreadable_mapping(ocr_cache).items()))
        image_ocr.merge_ooxml_overlays(
            ocr_cache, ocr_map,
            [(original_boxes, preview_orig, ooxml_src[0]),
             (anonymized_boxes, preview_anon, ooxml_src[1])])
    _label_boxes(original_boxes, anonymized_boxes)

    info = {"original_boxes": original_boxes,
            "anonymized_boxes": anonymized_boxes,
            "page_sizes": {"original": pdf_mod.page_sizes(preview_orig),
                           "anonymized": pdf_mod.page_sizes(preview_anon)},
            "preview_truncated": truncated}
    return preview_orig, preview_anon, info


def _build_item_preview(conv_id, item_id, ext, original_data, redacted_data,
                        repl_pairs, canonical, exact_phs, report_boxes=None,
                        ocr_cache=None):
    """I due PDF di anteprima + i box, per UN pezzo del turno. Scrive i PDF
    su disco e invalida la cache PNG; ritorna box e dimensioni pagina."""
    preview_orig, preview_anon, info = build_preview(
        ext, original_data, redacted_data, repl_pairs, canonical, exact_phs,
        report_boxes, ocr_cache)
    out_dir = _dir(conv_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    _pdf_path(conv_id, item_id, "original").write_bytes(preview_orig)
    _pdf_path(conv_id, item_id, "anonymized").write_bytes(preview_anon)
    png_cache.drop_pngs(_cache_key(conv_id, item_id))
    return info


def _progress(cb, index, total, filename):
    if cb is not None:
        cb({"index": index, "total": total, "filename": filename,
            "stage": "preview", "phase": None,
            "phase_index": None, "phase_total": None,
            "units_done": None, "units_total": None})


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise ca.TurnAnonymizationError("Invio annullato.", canceled=True)


def _report_stats(report):
    """Statistiche per la scheda dell'allegato dopo una RI-redazione: qui non
    c'è una nuova analisi, quindi si ricavano dalle occorrenze redatte."""
    by_ph = {ph: n for ph, n in (report.get("by_placeholder") or {}).items()
             if n}
    by_label = {}
    for ph, n in by_ph.items():
        lab = pdf_mod._ph_label(ph)
        by_label[lab] = by_label.get(lab, 0) + n
    return {"n_entities": report.get("occurrences", 0),
            "n_unique": len(by_ph),
            "by_label": dict(sorted(by_label.items(), key=lambda x: -x[1]))}


def _prompt_report(spans):
    by_ph = {}
    for s in spans:
        by_ph[s["ph"]] = by_ph.get(s["ph"], 0) + 1
    return {"n_entities": len(spans), "by_placeholder": by_ph}


# --- Preparazione del turno (stage) ------------------------------------------

def stage_turn(conv_id, content, att_ids, cancel=None, on_progress=None,
               ocr=False):
    """Anonimizza il turno (identico a un invio normale) e costruisce le
    anteprime di ogni pezzo. Ritorna lo stato staged; NIENTE viene inviato."""
    discard(conv_id)                # un'anteprima precedente è comunque stantia
    capture = {}
    model_content, version, att_descriptors = ca.anonymize_turn(
        conv_id, content, att_ids, cancel, on_progress, capture=capture,
        ocr=ocr)

    mapping = capture["mapping"]                # ReplacementMapping del turno
    canonical = dict(mapping)                   # ph -> valore canonico
    items = []
    total = len(capture["items"]) + 1
    try:
        with db.SessionLocal() as session:
            for i, meta in enumerate(capture["items"], start=1):
                _check_cancel(cancel)
                att = session.get(Attachment, meta["att_id"])
                name = att.display_filename or att.filename
                _progress(on_progress, i, total, name)
                data, ext = ca._converted_input(
                    Path(att.original_path).read_bytes(), name)
                report = json.loads(att.anonymization_report_json or "{}")
                report_boxes = (report.pop("boxes", None)
                                if ext == ".pdf" or ext in ca._IMAGE_EXTS
                                else None)
                text = meta.get("text")
                repl = (mapping.for_text(text) if text else mapping).items()
                ocr_cache = ca.load_att_ocr_cache(att)
                built = _build_item_preview(
                    conv_id, att.id, ext, data,
                    Path(att.protected_path).read_bytes(), repl, canonical,
                    meta["exact_phs"], report_boxes, ocr_cache)
                items.append({"id": att.id, "kind": "attachment",
                              "att_id": att.id, "filename": name, "ext": ext,
                              "local_map": image_ocr.unreadable_mapping(ocr_cache),
                              # il testo del RILEVAMENTO (corpus OCR incluso):
                              # è già in mano, ricalcolarlo a ogni modifica
                              # vorrebbe dire riconvertire il file. None per
                              # l'xlsx, che lo ricompone da sé
                              "text": meta.get("text"),
                              "exact_phs": meta["exact_phs"],
                              "sealed": ca.att_sealed(att),
                              "report": report, **built})
        _check_cancel(cancel)
        _progress(on_progress, total, total, PROMPT_LABEL)
        built = _build_item_preview(
            conv_id, PROMPT_ITEM, ".txt", content.encode("utf-8"),
            model_content.encode("utf-8"),
            mapping.for_text(content).items(), canonical, ())
        items.append({"id": PROMPT_ITEM, "kind": "prompt", "att_id": None,
                      "filename": PROMPT_LABEL, "ext": ".txt",
                      "exact_phs": [], "sealed": [],
                      "report": _prompt_report(capture["prompt_entities"]),
                      **built})
    except ca.TurnAnonymizationError:
        raise
    except Exception as exc:
        # l'anonimizzazione è RIUSCITA (file protetti scritti): è l'anteprima
        # che non si riesce a costruire. Niente parte, l'utente può riprovare.
        raise ca.TurnAnonymizationError(
            "Anteprima non disponibile: "
            + (str(exc) or exc.__class__.__name__)) from exc

    st = {"content": content, "model_content": model_content,
          "prompt_entities": capture["prompt_entities"],
          "mapping_version": version, "rev": 0, "canonical": canonical,
          "att_ids": list(att_ids), "items": items,
          "attachments": att_descriptors}
    with _guard:
        _staged[conv_id] = st
    return st


def _suggestions(conv_id):
    """Le fusioni da proporre, RICALCOLATE ora. Non si fotografano alla
    preparazione del turno: durante la revisione l'utente può ri-includere un
    valore che aveva lasciato in chiaro (e la coppia ricompare) o escluderne
    uno (e la coppia sparisce), quindi l'ultimo step del modal deve leggere il
    registro com'è in questo istante."""
    with db.SessionLocal() as session:
        return ca.merge_suggestions(session, ca.registry_scope(conv_id))


def descriptor(conv_id):
    """Lo stato dell'anteprima nel formato che il frontend consuma: ogni item
    ha la stessa forma del descrittore di un Documento (il viewer è lo
    stesso), con la mappa filtrata ai placeholder di QUEL pezzo."""
    st = _staged.get(conv_id)
    if st is None:
        return None
    canonical = st["canonical"]
    out = []
    for item in st["items"]:
        phs = _item_placeholders(item)
        report = {k: v for k, v in (item["report"] or {}).items()
                  if k != "boxes"}
        report["preview_truncated"] = item.get("preview_truncated", False)
        out.append({
            "id": item["id"], "kind": item["kind"],
            "filename": item["filename"], "rev": st["rev"],
            # l'estensione EFFETTIVA (post-conversione .xls -> .xlsx): il
            # viewer decide da qui se il pezzo ha le colonne cliccabili, e il
            # nome del file da solo mentirebbe
            "ext": item["ext"],
            # rielaborazione con OCR in coda o in corso su questo allegato
            "busy": bool(item["att_id"]) and jobs.doc_busy(item["att_id"]),
            "n_pages": len(item["page_sizes"]["original"]),
            "n_entities": (item["report"] or {}).get("n_entities", 0),
            "report": report,
            # + le voci locali della cache OCR ([UNREADABLE_n]/[SIGNATURE_n]),
            # che il registro non ospita: il popup del box deve mostrarne il
            # valore e poterle deanonimizzare come le altre
            "mapping": {ph: (canonical.get(ph) if ph in canonical
                             else (item.get("local_map") or {}).get(ph))
                        for ph in sorted(phs)
                        if ph in canonical
                        or ph in (item.get("local_map") or {})},
            "original_boxes": item["original_boxes"],
            "anonymized_boxes": item["anonymized_boxes"],
            "page_sizes": item["page_sizes"],
            "sealed": item.get("sealed") or [],
            "original_available": True,
        })
    return {"total": len(out), "rev": st["rev"], "content": st["content"],
            "mapping_version": st["mapping_version"],
            "attachments": st["attachments"], "items": out,
            "suggestions": _suggestions(conv_id)}


def _item_placeholders(item):
    phs = set()
    for side in ("original_boxes", "anonymized_boxes"):
        for blist in (item.get(side) or {}).values():
            phs.update(b["ph"] for b in blist)
    phs.update(ph for ph, n in ((item.get("report") or {})
                                .get("by_placeholder") or {}).items() if n)
    return phs


def preview_pdf(conv_id, item_id, source):
    """Bytes del PDF di anteprima di un pezzo (per PNG di pagina/estrazione)."""
    st = _staged.get(conv_id)
    if st is None or not any(i["id"] == item_id for i in st["items"]):
        return None
    p = _pdf_path(conv_id, item_id, source)
    return p.read_bytes() if p.is_file() else None


def page_png(conv_id, item_id, source, n):
    key = _cache_key(conv_id, item_id)
    entry = png_cache.get_png(key, source, n)
    if entry is not None:
        return entry
    data = preview_pdf(conv_id, item_id, source)
    if data is None:
        return None
    entry = render_page_png(data, n)
    if entry is not None:
        png_cache.cache_png(key, source, n, entry)
    return entry


# --- Modifiche dall'anteprima -------------------------------------------------

def _full_text(data, ext):
    """Il testo del LAYER TESTUALE del file, e SOLO quello: dentro le
    immagini non vede niente. Per misurare ciò che l'utente ha davanti
    nell'anteprima serve `redigible_text` (questo + il corpus OCR); da solo va
    bene dove le immagini le guarda già qualcun altro — il controllo di
    uscita di project_files.confirm, che si appoggia a stale_leaks."""
    if ext in ca._IMAGE_EXTS:
        return ""       # il testo di un'immagine è tutto nel corpus OCR
    if ext == ".pdf":
        return extract_pdf(data, allow_empty=True)[0]
    if ext == ".docx":
        return extract_docx(data)
    if ext == ".pptx":
        return extract_pptx(data, full=True)
    if ext == ".xlsx":
        return extract_xlsx(data, full=True)
    return extract_txt(data)


def redigible_text(data, ext, ocr_cache):
    """TUTTO il testo redigibile del file: layer testuale + corpus OCR delle
    immagini. È ciò che l'utente VEDE nell'anteprima, quindi è l'unica
    misura giusta sia per le varianti dedotte di mapping.for_text sia per il
    controllo "il valore selezionato esiste davvero".

    Punto unico apposta: quando la chat controllava il solo layer testuale, un
    valore letto dall'OCR (e magari letto male: è l'immagine a comandare)
    veniva estratto dalla selezione ma poi rifiutato dall'anonimizza-in-più
    come "non presente nel turno". Lo condivide project_files."""
    try:
        text = _full_text(data, ext)
    except Exception:
        text = ""
    if ocr_cache:
        ctext = image_ocr.corpus(ocr_cache)[0]
        text = (text + "\n" + ctext) if text else ctext
    return text


def _rebuild_turn(session, conv, st):
    """Ri-redige TUTTO il turno con il registro corrente (stessa logica del
    pass B di anonymize_turn: prima si calcola tutto, poi si scrive) e
    rigenera le anteprime. Alza StagedEditError senza scrivere niente se una
    redazione fallisce."""
    defaults = ca.anon_options(session, conv)
    skip = ca.excluded_groups(defaults["excluded_tags"])
    holder = ca.registry_holder(session, conv)
    mapping = ca.conversation_mapping(session, holder.id, include_aliases=True,
                                      exclude=skip)
    canonical = dict(mapping)
    version = holder.mapping_version or 0

    # prompt: si riparte dagli span dell'analisi (identico al percorso di
    # analyze: splice degli span tenuti + difesa deterministica sul resto).
    # Gli span conservano il placeholder di QUANDO l'analisi è girata: se
    # l'entità è stata fusa nel frattempo va tradotto, o il messaggio
    # userebbe un tag che gli allegati (ri-redatti dalla mappa corrente) non
    # usano più.
    content = st["content"]
    merged = ca.merged_placeholders(session, holder.id)
    spans = []
    for span in st["prompt_entities"]:
        ph = merged.get(span["ph"], span["ph"])
        if mapping._is_excluded(ph):
            continue
        spans.append({**span, "ph": ph})
    pieces, pos = [], 0
    for s in sorted(spans, key=lambda x: x["start"]):
        pieces.extend((content[pos:s["start"]], s["ph"]))
        pos = s["end"]
    pieces.append(content[pos:])
    model_content = ca.apply_known_surfaces("".join(pieces),
                                            mapping.for_text(content))

    # allegati: prima si calcola tutto in memoria...
    computed = []
    for item in st["items"]:
        if item["kind"] != "attachment":
            continue
        att = session.get(Attachment, item["att_id"])
        if att is None or not att.original_path:
            raise StagedEditError(410, f"{item['filename']}: il file non è "
                                       "più disponibile sul server.")
        data, ext = ca._converted_input(Path(att.original_path).read_bytes(),
                                        item["filename"])
        ocr_cache = ca.load_att_ocr_cache(att)
        text = item.get("text")
        if text is None:
            # col corpus OCR in coda: mapping.for_text deve vedere anche le
            # superfici lette nelle immagini
            text = redigible_text(data, ext, ocr_cache)
        item["text"] = text
        try:
            out, report = ca._redact_with(data, ext, mapping.for_text(text),
                                          exact_phs=set(item["exact_phs"]),
                                          ocr_cache=ocr_cache,
                                          sealed=ca.att_sealed(att))
        except Exception as exc:
            raise StagedEditError(422, f"{item['filename']}: "
                                       + (str(exc) or exc.__class__.__name__))
        if report.get("residual"):
            raise StagedEditError(422, f"{item['filename']}: verifica dei "
                                       "residui fallita, la modifica non è "
                                       "stata applicata.")
        computed.append((item, att, ext, data, out, report, ocr_cache))

    # ...poi si scrive (file protetti, schede, anteprime)
    for item, att, ext, data, out, report, ocr_cache in computed:
        target = Path(att.original_path).parent / f"{att.id}_protected{ext}"
        target.write_bytes(out)
        model_name = att.model_filename or f"allegato{ext}"
        card = briefing.describe(str(target), model_name,
                                 mimetypes.guess_type(model_name)[0])
        att.protected_path = str(target)
        att.model_briefing_json = json.dumps(card, ensure_ascii=False)
        turn_report = ca._turn_report(
            {"report": report, "stats": _report_stats(report)}, mapping)
        # le informazioni tabellari (colonne anonimizzate per intero e loro
        # sostituzioni per cella) le produce solo il RILEVAMENTO: senza questo
        # riporto sparirebbero alla prima modifica dall'anteprima
        old_report = item.get("report") or {}
        for key in ("table_phs", "table_columns", "header_kept"):
            if old_report.get(key) and not turn_report.get(key):
                turn_report[key] = old_report[key]
        att.anonymization_report_json = json.dumps(turn_report,
                                                   ensure_ascii=False)
        att.anonymization_status = "protected"
        att.mapping_version = version
        report_boxes = (report.pop("boxes", None)
                        if ext == ".pdf" or ext in ca._IMAGE_EXTS else None)
        built = _build_item_preview(
            conv.id, item["id"], ext, data, out,
            mapping.for_text(item["text"]).items(), canonical,
            item["exact_phs"], report_boxes, ocr_cache)
        item["sealed"] = ca.att_sealed(att)
        item["local_map"] = image_ocr.unreadable_mapping(ocr_cache)
        item.update(built)
        item["report"] = {k: v for k, v in turn_report.items() if k != "boxes"}

    built = _build_item_preview(
        conv.id, PROMPT_ITEM, ".txt", content.encode("utf-8"),
        model_content.encode("utf-8"), mapping.for_text(content).items(),
        canonical, ())
    for item in st["items"]:
        if item["kind"] == "prompt":
            item.update(built)
            item["report"] = _prompt_report(spans)

    st["model_content"] = model_content
    st["mapping_version"] = version
    st["canonical"] = canonical
    st["attachments"] = [att.descriptor()
                         for _i, att, *_rest in computed]
    st["rev"] += 1


def _require_staged(conv_id):
    st = _staged.get(conv_id)
    if st is None:
        raise StagedEditError(409, "Nessuna anteprima in corso per questa "
                                   "conversazione: ripeti l'invio.",
                              "no_preview_resend")
    return st


def deanonymize(conv_id, placeholder=None, label=None):
    """L'utente lascia in chiaro un valore ("[FULLNAME_1]") o tutti quelli di
    una categoria VISIBILI in questo turno. L'entità resta nel registro
    (i placeholder già partiti si decodificano per sempre) ma da ora è
    `excluded`: non si sostituisce più, in questo turno e nei prossimi."""
    scope = ca.registry_scope(conv_id)
    with ca.conversation_lock(scope), db.SessionLocal() as session:
        conv = session.get(Conversation, conv_id)
        if conv is None:
            raise StagedEditError(404, "Conversazione non trovata.",
                                  "conversation_not_found")
        st = _require_staged(conv_id)
        if image_ocr.is_local(placeholder) or image_ocr.local_label(label):
            # [UNREADABLE_n]/[SIGNATURE_n]: voci della cache OCR, non del
            # registro — l'esclusione si scrive nella cache dell'allegato
            removed = _deanonymize_local(session, st, placeholder, label)
            if not removed:
                raise StagedEditError(404, "Segnaposto non presente nella mappa.")
            _rebuild_turn(session, conv, st)
            session.commit()
            desc = descriptor(conv_id)
            desc["removed"] = removed
            return desc
        entities = (session.query(ConversationEntity)
                    .filter_by(conv_id=scope).all())
        if placeholder:
            targets = [e for e in entities if e.placeholder == placeholder
                       and not e.excluded]
        elif label:
            group = ca._label_group(label)
            turn_phs = set()
            for item in st["items"]:
                turn_phs |= _item_placeholders(item)
            targets = [e for e in entities
                       if not e.excluded and not e.merged_into
                       and ca._label_group(e.label) == group
                       and e.placeholder in turn_phs]
        else:
            raise StagedEditError(422, "Indica un segnaposto (placeholder) "
                                       "o una categoria (label).")
        if not targets:
            raise StagedEditError(404, "Segnaposto non presente nella mappa.")
        for e in targets:
            e.excluded = 1
        session.flush()
        _rebuild_turn(session, conv, st)
        session.commit()
    desc = descriptor(conv_id)
    desc["removed"] = [e.placeholder for e in targets]
    return desc


def _deanonymize_local(session, st, placeholder, label):
    """Esclude voci locali ([UNREADABLE_n]/[SIGNATURE_n]) dalle cache OCR
    degli allegati del turno; ritorna i placeholder esclusi."""
    removed = []
    for item in st["items"]:
        if item["kind"] != "attachment":
            continue
        att = session.get(Attachment, item["att_id"])
        cache = ca.load_att_ocr_cache(att) if att is not None else None
        if not cache:
            continue
        hit = image_ocr.exclude_local(cache, placeholder=placeholder,
                                      label=label)
        if hit:
            ca.save_att_ocr_cache(att, cache)
            item["local_map"] = image_ocr.unreadable_mapping(cache)
            removed.extend(hit)
    return removed


def _reinclude_local(session, st, value):
    """Inverso di _deanonymize_local per una riga illeggibile: se `value` è
    la lettura di una [UNREADABLE_n] esclusa la si ricopre e si ritorna il
    placeholder, senza allocare un [CUSTOM_n] col testo storpiato nel
    registro. None se nessuna riga esclusa corrisponde."""
    for item in st["items"]:
        if item["kind"] != "attachment":
            continue
        att = session.get(Attachment, item["att_id"])
        cache = ca.load_att_ocr_cache(att) if att is not None else None
        if not cache or not cache.get("excluded"):
            continue
        ph = image_ocr.include_local(cache, value)
        if ph:
            ca.save_att_ocr_cache(att, cache)
            item["local_map"] = image_ocr.unreadable_mapping(cache)
            return ph
    return None


def anonymize_text(conv_id, text, tag="CUSTOM"):
    """Anonimizza un testo scelto dall'utente come [TAG_n] nel REGISTRO
    della conversazione (tutte le occorrenze, in tutto il turno — e nei turni
    futuri). Se il valore corrisponde a un'entità già esclusa, la
    RI-include: è l'inverso esatto di deanonymize.

    `tag` è il nome scelto nel popup di selezione manuale quando si spunta la
    casella `viewer.selection.saveTerm` (già normalizzato da
    settings_store.clean_tag), CUSTOM altrimenti: il segnaposto
    che compare nell'anteprima è quello scritto dall'utente. Contatori per
    prefisso, come nel motore: [TAG_n] non collide col rilevatore."""
    scope = ca.registry_scope(conv_id)
    with ca.conversation_lock(scope), db.SessionLocal() as session:
        conv = session.get(Conversation, conv_id)
        if conv is None:
            raise StagedEditError(404, "Conversazione non trovata.",
                                  "conversation_not_found")
        holder = ca.registry_holder(session, conv)
        st = _require_staged(conv_id)
        value = re.sub(r"\s+", " ", text or "").strip()
        if not value:
            raise StagedEditError(422, "Selezione vuota.")
        if any(ph in value for ph in st["canonical"]):
            raise StagedEditError(422, "La selezione contiene un segnaposto: "
                                       "seleziona solo testo in chiaro.")
        if not ca._searchable(value):
            raise StagedEditError(
                422, "Testo troppo corto o ambiguo per essere anonimizzato in "
                     "modo sicuro in una conversazione (minimo 4 caratteri "
                     "alfanumerici, e niente frammenti che compaiono dentro "
                     "altre parole).")

        # valore già nel registro? (canonico o alias, qualunque label)
        entities = (session.query(ConversationEntity)
                    .filter_by(conv_id=scope).all())
        aliases = (session.query(ConversationEntityAlias, ConversationEntity)
                   .join(ConversationEntity, ConversationEntityAlias
                         .entity_id == ConversationEntity.id)
                   .filter(ConversationEntity.conv_id == scope).all())
        hit = next((e for e in entities
                    if _norm(e.canonical_value) == _norm(value)), None)
        if hit is None:
            hit = next((e for a, e in aliases
                        if _norm(a.original_surface) == _norm(value)), None)
        if hit is not None and not hit.excluded:
            raise StagedEditError(
                409, f"Questo valore è già anonimizzato come "
                     f"{hit.placeholder}.")
        local_ph = None if hit is not None else _reinclude_local(session, st,
                                                                  value)
        if hit is not None:
            hit.excluded = 0            # ri-inclusione: inverso di deanonymize
            new_ph = hit.placeholder
        elif local_ph is not None:
            new_ph = local_ph           # riga illeggibile ricoperta
        else:
            # il valore deve esistere davvero in almeno un pezzo del turno
            pattern = ca._pattern(value)
            found = bool(pattern and pattern.search(st["content"]))
            if not found:
                for item in st["items"]:
                    if item["kind"] != "attachment":
                        continue
                    att = session.get(Attachment, item["att_id"])
                    if att is None or not att.original_path:
                        continue
                    if item.get("text") is None:
                        data, ext = ca._converted_input(
                            Path(att.original_path).read_bytes(),
                            item["filename"])
                        item["text"] = redigible_text(
                            data, ext, ca.load_att_ocr_cache(att))
                    if pattern and pattern.search(item["text"]):
                        found = True
                        break
            if not found:
                raise StagedEditError(
                    422, "Il testo selezionato non risulta né nel messaggio "
                         "né negli allegati di questo invio (prova a "
                         "selezionare parole intere).")
            label = tag or "CUSTOM"
            nums = [int(m.group(1)) for e in entities
                    if (m := re.fullmatch(rf"\[{re.escape(label)}_(\d+)\]",
                                          e.placeholder))]
            new_ph = f"[{label}_{max(nums, default=0) + 1}]"
            holder.mapping_version = (holder.mapping_version or 0) + 1
            entity = ConversationEntity(
                conv_id=scope, placeholder=new_ph, label=label,
                canonical_value=value, mapping_version=holder.mapping_version)
            session.add(entity)
            session.flush()
            holder.mapping_version = (holder.mapping_version or 0) + 1
            session.add(ConversationEntityAlias(
                entity_id=entity.id, original_surface=value,
                normalized_key=ca._surface_key(label, value),
                source="user", confidence="exact",
                mapping_version=holder.mapping_version))
        session.flush()
        _rebuild_turn(session, conv, st)
        session.commit()
    desc = descriptor(conv_id)
    desc["added"] = new_ph
    return desc


def _item_ocr_cache(conv_id, item_id):
    """Cache OCR dell'allegato dietro un item dell'anteprima (None per il
    prompt, per un item sconosciuto o per un turno senza OCR)."""
    st = _staged.get(conv_id)
    item = next((i for i in (st["items"] if st else ())
                 if i["id"] == item_id), None)
    if item is None or item["kind"] != "attachment":
        return None
    with db.SessionLocal() as session:
        att = session.get(Attachment, item["att_id"])
        return ca.load_att_ocr_cache(att) if att is not None else None


def extract(conv_id, item_id, source, page, rect):
    """Testo sotto un rettangolo dell'anteprima. Tre vie, in ordine:
      1. layer testuale della pagina;
      2. CACHE OCR dell'allegato (image_ocr.text_in_rect_cache): la stessa
         lettura che il controllo di anonymize_text e la redazione nei pixel
         useranno, quindi ciò che si mostra è per costruzione anonimizzabile;
      3. OCR del ritaglio renderizzato, solo se la cache non copre l'area
         (turno senza OCR, immagine scartata dal detect) o non ha parole lì.
    `ocr_redactable` dice se una successiva anonimizzazione potrà coprire il
    valore nei pixel: vero per la via 2; per la via 3 solo se la lettura
    compare nel corpus della cache. `ocr_cache` dice se l'allegato HA una
    cache: senza, la strada è ripetere l'invio con l'OCR attivo; con la
    cache ma senza il testo, la selezione va allargata."""
    data = preview_pdf(conv_id, item_id, source)
    if data is None:
        raise StagedEditError(404, "Anteprima non trovata: ripeti l'invio.",
                              "preview_item_not_found")
    text = text_in_rect(data, page, rect)
    if text:
        return {"text": text, "ocr": False, "ocr_tried": False}
    cache = _item_ocr_cache(conv_id, item_id)
    return ocr_extract(data, page, rect, cache)


def ocr_extract(data, page, rect, cache):
    """Vie 2 e 3 di `extract` (condivise con project_files): cache OCR, poi
    ritaglio."""
    read = image_ocr.text_in_rect_cache(data, page, rect, cache)
    if read:
        return {"text": read, "ocr": True, "ocr_tried": True,
                "ocr_redactable": True, "ocr_cache": True,
                "ocr_source": "cache"}
    if not image_ocr.available():
        return {"text": "", "ocr": False, "ocr_tried": False,
                "ocr_cache": bool(cache)}
    try:
        read = image_ocr.text_in_rect_ocr(data, page, rect)
    except image_ocr.OcrError:
        return {"text": "", "ocr": False, "ocr_tried": False,
                "ocr_cache": bool(cache)}
    return {"text": read, "ocr": bool(read), "ocr_tried": True,
            "ocr_redactable": image_ocr.in_cache(read, cache),
            "ocr_cache": bool(cache), "ocr_source": "clip"}


def _sealable_item(st, item_id, session):
    """L'item dell'anteprima + il suo allegato, se sigillabile (PDF/immagine:
    i formati in cui la preview coincide geometricamente col file esportato).
    StagedEditError altrimenti."""
    item = next((i for i in st["items"] if i["id"] == item_id), None)
    if item is None:
        raise StagedEditError(404, "Anteprima non trovata: ripeti l'invio.",
                              "preview_item_not_found")
    if item["kind"] != "attachment" or (
            item["ext"] != ".pdf" and item["ext"] not in ca._IMAGE_EXTS):
        raise StagedEditError(422, "Il sigillo di un'area vale solo per "
                                   "allegati PDF e immagini: negli altri "
                                   "formati l'anteprima non corrisponde "
                                   "geometricamente al file inviato.")
    att = session.get(Attachment, item["att_id"])
    if att is None or not att.original_path:
        raise StagedEditError(410, f"{item['filename']}: il file non è più "
                                   "disponibile sul server.")
    return item, att


def seal_area(conv_id, item_id, page, rect, all_pages=False):
    """SIGILLA un'area dell'anteprima anonimizzata di un allegato PDF/immagine:
    il contenuto sotto il rettangolo viene RIMOSSO dalla copia protetta che
    partirà verso il modello (nero, scritta SEALED, nessuna voce nel
    registro). Persiste sull'allegato: vale anche per l'invio diretto e per i
    reinvii, finché non si rimuove con remove_seal."""
    with ca.conversation_lock(ca.registry_scope(conv_id)), \
            db.SessionLocal() as session:
        conv = session.get(Conversation, conv_id)
        if conv is None:
            raise StagedEditError(404, "Conversazione non trovata.",
                                  "conversation_not_found")
        st = _require_staged(conv_id)
        item, att = _sealable_item(st, item_id, session)
        if len(rect) != 4:
            raise StagedEditError(422, "Rettangolo non valido.",
                                  "rect_invalid")
        sizes = item["page_sizes"]["anonymized"]
        if not (0 <= page < len(sizes)):
            raise StagedEditError(422, "Pagina fuori range.")
        x0, x1 = sorted((float(rect[0]), float(rect[2])))
        y0, y1 = sorted((float(rect[1]), float(rect[3])))
        x0, x1 = max(0.0, x0), min(float(sizes[page]["width"]), x1)
        y0, y1 = max(0.0, y0), min(float(sizes[page]["height"]), y1)
        if x1 - x0 < 2 or y1 - y0 < 2:
            raise StagedEditError(422, "Area troppo piccola per essere "
                                       "sigillata.")
        sealed = ca.att_sealed(att)
        n = max((s["n"] for s in sealed), default=0) + 1
        sealed.append({"n": n, "page": page,
                       "all": bool(all_pages) and item["ext"] == ".pdf",
                       "rect": [x0, y0, x1, y1]})
        att.sealed_json = json.dumps(sealed)
        session.flush()
        _rebuild_turn(session, conv, st)
        session.commit()
    desc = descriptor(conv_id)
    desc["sealed_added"] = n
    return desc


def _attachment_item(st, item_id):
    """L'item dell'anteprima, se è un ALLEGATO: il messaggio non è un file e
    non ha né colonne né immagini da rileggere."""
    item = next((i for i in st["items"] if i["id"] == item_id), None)
    if item is None:
        raise StagedEditError(404, "Anteprima non trovata: ripeti l'invio.",
                              "preview_item_not_found")
    if item["kind"] != "attachment":
        raise StagedEditError(422, "Operazione valida solo sugli allegati "
                                   "dell'invio.")
    return item


def _xlsx_item(st, item_id):
    item = _attachment_item(st, item_id)
    if item["ext"] != ".xlsx":
        raise StagedEditError(422, "L'anonimizzazione per colonna vale solo "
                                   "per i fogli di calcolo (.xlsx).",
                              "column_xlsx_only")
    return item


def _item_bytes(session, item):
    """(allegato, byte, estensione) di un item dell'anteprima, già passati
    dalla conversione d'ingresso: in chat l'originale su disco è il file
    caricato (.xls, .doc...), mentre l'anteprima e la redazione lavorano sul
    formato convertito."""
    att = session.get(Attachment, item["att_id"])
    if att is None or not att.original_path \
            or not Path(att.original_path).is_file():
        raise StagedEditError(410, f"{item['filename']}: il file non è più "
                                   "disponibile sul server.")
    data, ext = ca._converted_input(Path(att.original_path).read_bytes(),
                                    item["filename"])
    return att, data, ext


def remove_seal(conv_id, item_id, n):
    """Rimuove un'area sigillata dall'allegato: la ri-redazione riparte
    dall'originale, quindi il contenuto torna nella copia protetta."""
    with ca.conversation_lock(ca.registry_scope(conv_id)), \
            db.SessionLocal() as session:
        conv = session.get(Conversation, conv_id)
        if conv is None:
            raise StagedEditError(404, "Conversazione non trovata.",
                                  "conversation_not_found")
        st = _require_staged(conv_id)
        item, att = _sealable_item(st, item_id, session)
        sealed = ca.att_sealed(att)
        kept = [s for s in sealed if s["n"] != n]
        if len(kept) == len(sealed):
            raise StagedEditError(404, "Area sigillata non trovata.")
        att.sealed_json = json.dumps(kept)
        session.flush()
        _rebuild_turn(session, conv, st)
        session.commit()
    desc = descriptor(conv_id)
    desc["sealed_removed"] = n
    return desc


# --- Colonne xlsx (layout cliccabile + azioni di colonna) --------------------
# Gemelle delle azioni di colonna dei file di progetto (project_files.py), con
# una sola differenza di fondo: lì l'unità è il file e l'anonimizzazione
# della colonna va in coda come job, qui l'unità è il TURNO e la
# ri-redazione tocca comunque tutti i pezzi — esattamente come
# `anonymize_text` e `deanonymize` qui sopra, che sono sincrone. Un job che
# riscrivesse un'anteprima che l'utente può buttare in qualunque momento
# aggiungerebbe corse senza togliere attesa.

def columns_layout(conv_id, item_id):
    """Le colonne cliccabili sulle due anteprime di un allegato xlsx:
    {sorgente: {pagina: {sheet, cols}}}. Vuoto su tutto il resto (altri
    formati, il messaggio): il viewer semplicemente non disegna nulla."""
    out = {"original": {}, "anonymized": {}}
    st = _staged.get(conv_id)
    if st is None:
        return out
    item = next((i for i in st["items"] if i["id"] == item_id), None)
    if item is None or item["kind"] != "attachment" or item["ext"] != ".xlsx":
        return out
    with db.SessionLocal() as session:
        try:
            _att, data, _ext = _item_bytes(session, item)
        except StagedEditError:
            return out
    try:
        names = sheet_names(data)
    except XlsxError:
        return out
    if not names:
        return out
    for source in ("original", "anonymized"):
        pdf = preview_pdf(conv_id, item_id, source)
        if pdf:
            out[source] = spreadsheet_columns(pdf, names)
    return out


def column_info(conv_id, item_id, sheet, column):
    """Conteggi della colonna per il popover: valori distinti, quanti sono
    trattabili in sicurezza e quanti sono GIÀ nel registro. Si leggono dal
    file completo, non dall'anteprima troncata."""
    from .engine.xlsx_table import usable_value
    st = _require_staged(conv_id)
    item = _xlsx_item(st, item_id)
    with db.SessionLocal() as session:
        _att, data, _ext = _item_bytes(session, item)
        try:
            vals, label = column_values(data, sheet, column)
        except XlsxError as e:
            raise StagedEditError(422, str(e))
        canonical = ca.conversation_mapping(session, ca.registry_scope(conv_id))
    colset = {_norm(v) for v in vals}
    return {"values": len(vals),
            "usable": sum(1 for v in vals if usable_value(v)),
            "mapped": sum(1 for v in canonical.values() if _norm(v) in colset),
            "header": label,
            "busy": jobs.doc_busy(item["att_id"])}


def _note_table_column(item, res):
    """Registra sull'item l'esito tabellare della colonna: i placeholder
    "exact" (sostituzione per cella intera) servono a ogni ri-redazione
    successiva, l'elenco delle colonne all'avviso in anteprima. Vivono
    sull'item e nel suo report perché il rilevamento non li riprodurrà più
    (vedi il riporto in _rebuild_turn)."""
    exact = set(res.get("exact_new") or ())
    item["exact_phs"] = sorted(set(item.get("exact_phs") or ()) | exact)
    report = item.setdefault("report", {})
    report["table_phs"] = sorted(set(report.get("table_phs") or ()) | exact)
    cols = list(report.get("table_columns") or ())
    if res.get("table_column") and res["table_column"] not in cols:
        cols.append(res["table_column"])
    report["table_columns"] = cols


def anonymize_column(conv_id, item_id, sheet, column):
    """Anonimizza l'INTERA colonna di un allegato xlsx del turno: ogni valore
    distinto trattabile entra nel registro della conversazione (o del
    progetto), deterministico e senza modello — chi è già in mappa riusa il
    suo segnaposto — e tutto il turno viene ri-redatto."""
    scope = ca.registry_scope(conv_id)
    with ca.conversation_lock(scope), db.SessionLocal() as session:
        conv = session.get(Conversation, conv_id)
        if conv is None:
            raise StagedEditError(404, "Conversazione non trovata.",
                                  "conversation_not_found")
        holder = ca.registry_holder(session, conv)
        st = _require_staged(conv_id)
        item = _xlsx_item(st, item_id)
        _att, data, _ext = _item_bytes(session, item)
        mapping = dict(ca.conversation_mapping(session, scope))
        try:
            res = anonymize_xlsx_column(data, sheet, column, mapping)
        except XlsxError as e:
            raise StagedEditError(422, str(e))
        if not res["new_placeholders"]:
            if res["existing"]:
                raise StagedEditError(
                    409, f"La colonna risulta già anonimizzata: "
                         f"{res['existing']} valori sono già in mappa e non "
                         f"c'è nulla di nuovo da coprire.")
            raise StagedEditError(
                422, f"Nessun valore anonimizzabile nella colonna: "
                     f"{res['skipped']} valori sono troppo corti o ambigui "
                     f"per essere redatti in modo sicuro.")
        new_pairs = {ph: mapping[ph] for ph in res["new_placeholders"]
                     if ph in mapping}
        ca.sync_allocated_mapping(session, holder, new_pairs)
        _note_table_column(item, res)
        session.flush()
        _rebuild_turn(session, conv, st)
        session.commit()
        added = len(res["new_placeholders"])
    desc = descriptor(conv_id)
    desc["added_column"] = added
    return desc


def deanonymize_column(conv_id, item_id, sheet, column):
    """Esclude dal registro tutti i valori (come cella intera) della colonna:
    tornano in chiaro in questo turno e nei prossimi. Come per i segnaposto
    singoli, un valore condiviso con altre colonne torna in chiaro OVUNQUE."""
    scope = ca.registry_scope(conv_id)
    with ca.conversation_lock(scope), db.SessionLocal() as session:
        conv = session.get(Conversation, conv_id)
        if conv is None:
            raise StagedEditError(404, "Conversazione non trovata.",
                                  "conversation_not_found")
        st = _require_staged(conv_id)
        item = _xlsx_item(st, item_id)
        _att, data, _ext = _item_bytes(session, item)
        try:
            vals, _label = column_values(data, sheet, column)
        except XlsxError as e:
            raise StagedEditError(422, str(e))
        colset = {_norm(v) for v in vals}
        entities = (session.query(ConversationEntity)
                    .filter_by(conv_id=scope).all())
        targets = [e for e in entities
                   if not e.excluded and _norm(e.canonical_value) in colset]
        if not targets:
            raise StagedEditError(404, "Nessun valore di questa colonna è in "
                                       "mappa: non c'è nulla da "
                                       "deanonimizzare.")
        for e in targets:
            e.excluded = 1
        session.flush()
        _rebuild_turn(session, conv, st)
        session.commit()
        removed = [e.placeholder for e in targets]
    desc = descriptor(conv_id)
    desc["removed"] = removed
    return desc


# --- Rielaborazione con OCR di un allegato del turno ------------------------

def reprocess_ocr(params, engine, session, ctl=None):
    """Rifà il rilevamento di UN allegato dell'anteprima con l'OCR attivo
    (job kind=chat_reprocess, dal worker) e ri-redige il turno col registro
    aggiornato. Gemella di project_files.reprocess_ocr, e come quella è
    ADDITIVA: il registro può solo imparare, sigilli e colonne tabellari
    sopravvivono. È il rimedio per un allegato inviato con l'OCR spento, dove
    la selezione manuale legge testo dentro un'immagine ma non c'è nessuna
    cache su cui disegnare i box.

    Va in coda come i caricamenti (e non è sincrona come le altre modifiche
    dell'anteprima) perché qui si rilegge davvero il file: OCR di tutte le
    immagini più un giro completo del modello, decine di secondi."""
    conv_id = params["conv_id"]
    with ca.conversation_lock(ca.registry_scope(conv_id)):
        conv = session.get(Conversation, conv_id)
        if conv is None:
            raise StagedEditError(404, "Conversazione non trovata.",
                                  "conversation_not_found")
        st = _require_staged(conv_id)
        item = _attachment_item(st, params["item_id"])
        att, data, ext = _item_bytes(session, item)
        if not image_ocr.available():
            raise StagedEditError(503, "OCR non disponibile su questo server: "
                                       "pacchetti rapidocr/onnxruntime non "
                                       "installati.",
                                  "ocr_unavailable")
        defaults = ca.anon_options(session, conv)
        holder = ca.registry_holder(session, conv)
        conv_engine = ca.ConversationEngine(engine, session, holder)
        try:
            found = ca._detect(
                data, ext, conv_engine, defaults, ctl=ctl,
                xlsx_max_chunks=settings_store.get_int(session,
                                                       "xlsx_max_chunks"),
                ocr=True)
        except (ValueError, PdfError, NotImplementedError) as exc:
            session.rollback()
            raise StagedEditError(422, f"{item['filename']}: {exc}")
        session.flush()
        # la cache OCR va su disco PRIMA della ri-redazione: è da lì che
        # _rebuild_turn la rilegge, per questo allegato e per i suoi box
        cache_path = ca._ocr_cache_path(att)
        if found.get("ocr_cache"):
            cache_path.write_text(
                json.dumps(found["ocr_cache"], ensure_ascii=False),
                encoding="utf-8")
        elif cache_path is not None:
            cache_path.unlink(missing_ok=True)
        # il testo del rilevamento porta già in coda il corpus OCR (None per
        # l'xlsx: la ri-redazione lo ricompone da sola)
        item["text"] = found["text"]
        item["exact_phs"] = sorted(set(item.get("exact_phs") or ())
                                   | set(found["exact_phs"] or ()))
        _rebuild_turn(session, conv, st)
        session.commit()
        return att

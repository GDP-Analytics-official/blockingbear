"""
Redazione delle presentazioni PowerPoint (.pptx): il fratello DrawingML di docx.py.

Un .pptx è uno ZIP di XML come il .docx, ma il testo sta nei nodi <a:t> dentro
i run <a:r> (DrawingML), e PowerPoint spezza i valori su più run esattamente
come Word. La strategia è la stessa: testo linearizzato char-preciso per part,
match con le regex ancorate di pdf_export, sostituzione attraverso i run con un
run NUOVO evidenziato in giallo (a:highlight, l'equivalente DrawingML di
w:highlight).

Dove vive il testo (e quindi dove si analizza/redige):
  - slide (ppt/slides/slideN.xml): corpo, tabelle (a:tbl), titoli, footer;
  - note del relatore (ppt/notesSlides/notesSlideN.xml);
  - COMMENTI: sia legacy (<p:text>, testo semplice senza run) sia moderni
    (txBody DrawingML dentro ppt/comments/*.xml);
  - SmartArt (ppt/diagrams/dataN.xml + drawingN.xml: il testo è in entrambi).
Layout e master (slideLayouts/slideMasters/notesMasters/handoutMasters) vengono
REDATTI ma esclusi dall'analisi: contengono boilerplate ("Fare clic per
modificare...") che sporcherebbe il modello, però un valore mappato che vi
compare (footer aziendale) va comunque coperto.

Oltre al testo si ripuliscono i posti in cui la PII sopravviverebbe:
  - docProps/core.xml (autore, titolo, ...) come per il docx;
  - docProps/app.xml: TitlesOfParts contiene i TITOLI delle slide (leak reale),
    più Company/Manager;
  - autori dei commenti (ppt/commentAuthors.xml legacy, ppt/authors.xml moderni):
    nome/iniziali/account svuotati;
  - target esterni degli hyperlink nei .rels (riuso di docx._scrub_rels).

Rete di sicurezza finale (contratto comune, vedi engine/__init__.py):
report["residual"] rilegge l'output completo — testo di TUTTE le part redatte,
metadati, rels, autori — e deve essere vuota.

Limiti noti (gli stessi del docx): il testo alternativo e i grafici incorporati
(il chart XML usa c:v, non a:t) non vengono redatti; il testo dentro le
immagini è coperto solo con ocr=True (vedi anonymize_pptx). Questo modulo
tratta i soli .pptx: i .ppt binari si convertono in .pptx all'ingresso
(engine/convert.py), come i .doc.

Solo stdlib, e le trappole di riserializzazione OOXML sono quelle risolte in
docx._serialize_part (vedi engine/__init__.py): qui si riusa quella, insieme a
scrub e pattern condivisi.
"""

import copy
import io
import re
import zipfile
import xml.etree.ElementTree as ET

from . import image_ocr
from .docx import (_alt_text_surfaces, _meta_surfaces, _part_namespaces,
                   _RelsValues, _scrub_alt_text, _scrub_app_props,
                   _scrub_core_props, _scrub_rels, _serialize_part)
from .pdf_export import _too_noisy, _value_pattern
from .text_patterns import contains_literal

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "{%s}" % A_NS
P = "{%s}" % P_NS

# part in cui il testo va REDATTO (tutte quelle che contengono a:t o p:text)
_REDACT_PART_RE = re.compile(
    r"^ppt/(slides/slide\d+|notesSlides/notesSlide\d+"
    r"|slideLayouts/slideLayout\d+|slideMasters/slideMaster\d+"
    r"|notesMasters/notesMaster\d+|handoutMasters/handoutMaster\d+"
    r"|comments/[^/]+|diagrams/(?:data|drawing)\d+)\.xml$")

# sottoinsieme da ANALIZZARE col modello: solo contenuto vero, niente
# boilerplate dei layout/master
_ANALYZE_PART_RE = re.compile(
    r"^ppt/(slides/slide\d+|notesSlides/notesSlide\d+"
    r"|comments/[^/]+|diagrams/(?:data|drawing)\d+)\.xml$")

# part con gli autori dei commenti: i nomi sono attributi, non testo
_AUTHOR_PARTS = ("ppt/commentAuthors.xml", "ppt/authors.xml")

# ordine di lettura: prima le slide, poi note e commenti (la numerazione dei
# placeholder segue questo ordine)
_FOLDER_ORDER = {"slides": 0, "notesSlides": 1, "comments": 2, "diagrams": 3}


class PptxError(ValueError):
    """Errore d'uso (file non valido, niente testo, dizionario vuoto...)."""


# --------------------------------------------------------------------------- #
# Caricamento + linearizzazione char-precisa
# --------------------------------------------------------------------------- #
def _sort_key(name):
    folder = name.split("/")[1] if name.count("/") >= 2 else ""
    m = re.search(r"(\d+)\.xml$", name)
    return (_FOLDER_ORDER.get(folder, 9),
            name[:m.start(1)] if m else name,
            int(m.group(1)) if m else 0)


def _walk_part(root):
    """(testo linearizzato, [[start, end, nodo_testo], ...]) di una part.

    Nodi di testo: <a:t> (run DrawingML, anche dentro i campi <a:fld>) e
    <p:text> (commenti legacy, testo semplice). Fine paragrafo e break
    diventano \\n "virtuali" che non appartengono a nessun nodo; anche dopo
    ogni <p:text> c'è un \\n, così un match non può scavalcare due commenti."""
    chunks, segs = [], []
    pos = 0

    def rec(el):
        nonlocal pos
        tag = el.tag
        if tag == A + "t" or tag == P + "text":
            s = el.text or ""
            if s:
                segs.append([pos, pos + len(s), el])
                chunks.append(s)
                pos += len(s)
            if tag == P + "text":
                chunks.append("\n"); pos += 1
            return
        if tag == A + "br":
            chunks.append("\n"); pos += 1
            return
        for ch in el:
            rec(ch)
        if tag == A + "p":
            chunks.append("\n"); pos += 1

    rec(root)
    return "".join(chunks), segs


def _load_parts(pptx_bytes):
    """{nome_part: (root, namespaces)} di tutte le part testuali redigibili."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(pptx_bytes))
        names = zf.namelist()
    except zipfile.BadZipFile:
        raise PptxError("File .pptx non valido o danneggiato.")
    if "ppt/presentation.xml" not in names:
        raise PptxError("Non sembra una presentazione PowerPoint (.pptx).")
    parts = {}
    for name in names:
        if _REDACT_PART_RE.match(name):
            data = zf.read(name)
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                raise PptxError(f"XML non valido dentro il pptx ({name}).")
            parts[name] = (root, _part_namespaces(data))
    return parts


def extract_text(pptx_bytes, full=False, allow_empty=False):
    """Testo della presentazione (slide + note + commenti + SmartArt).
    Con full=True include anche layout/master: serve per cercare un valore
    OVUNQUE possa essere redatto (residual check, selezione manuale), non per
    l'analisi del modello. PptxError se non c'è testo (con allow_empty=True
    il vuoto è ammesso: percorso OCR, slide di sole immagini)."""
    parts = _load_parts(pptx_bytes)
    part_re = _REDACT_PART_RE if full else _ANALYZE_PART_RE
    texts = []
    for name in sorted(parts, key=_sort_key):
        if not part_re.match(name):
            continue
        t, _ = _walk_part(parts[name][0])
        if t.strip():
            texts.append(t)
    text = "\n".join(texts)
    if not text.strip() and not allow_empty:
        raise PptxError("La presentazione non contiene testo.")
    return text


# --------------------------------------------------------------------------- #
# Sostituzione attraverso i run
# --------------------------------------------------------------------------- #
# figli di a:rPr (CT_TextCharacterProperties, sequenza obbligata) che vengono
# DOPO a:highlight: come per w:rPr, appendere in fondo produce un file che
# PowerPoint considera danneggiato
_TCPR_AFTER_HIGHLIGHT = {A + t for t in (
    "uLnTx", "uLn", "uFillTx", "uFill", "latin", "ea", "cs", "sym",
    "hlinkClick", "hlinkMouseOver", "rtl", "extLst")}


def _make_run(model_run, text, highlight=False):
    """Run <a:r> nuovo che eredita la formattazione del run colpito (deepcopy
    del suo a:rPr; il "run" può anche essere un campo <a:fld>). Con
    highlight=True aggiunge l'evidenziazione GIALLA nativa nell'ordine dello
    schema — ma un a:highlight PREESISTENTE non si sostituisce: è un segno
    dell'utente e il ripristino (che toglie solo il NOSTRO giallo dai run coi
    placeholder) deve poterlo lasciare al suo posto."""
    run = ET.Element(A + "r")
    rpr = model_run.find(A + "rPr")
    rpr = copy.deepcopy(rpr) if rpr is not None else ET.Element(A + "rPr")
    if highlight and rpr.find(A + "highlight") is None:
        hl = ET.Element(A + "highlight")
        ET.SubElement(hl, A + "srgbClr").set("val", "FFFF00")
        idx = next((i for i, ch in enumerate(rpr)
                    if ch.tag in _TCPR_AFTER_HIGHLIGHT), len(rpr))
        rpr.insert(idx, hl)
    run.append(rpr)
    t = ET.SubElement(run, A + "t")
    t.text = text
    return run


def _replace_match(segs, parents, ms, me, ph):
    """Applica UNA sostituzione [ms, me) -> ph sui nodi di testo coinvolti.
    Ordine decrescente di offset, come nel docx."""
    hit = [s for s in segs if s[0] < me and s[1] > ms]
    if not hit:
        return False
    first = hit[0]
    t1 = first[2]
    run1 = parents[t1]
    text1 = t1.text or ""
    ls = ms - first[0]
    le_first = min(me, first[1]) - first[0]

    if run1.tag not in (A + "r", A + "fld"):
        # testo semplice (p:text dei commenti legacy): non ci sono run, il
        # placeholder va inline senza evidenziazione
        tail = text1[le_first:] if me <= first[1] else ""
        t1.text = text1[:ls] + ph + tail
        for seg in hit[1:]:
            t = seg[2]
            s = t.text or ""
            cut_end = min(me, seg[1]) - seg[0]
            t.text = s[cut_end:] if me < seg[1] else ""
        return True

    holder = parents[run1]                   # a:p
    after = ""
    if me <= first[1]:
        after = text1[le_first:]
    else:
        for seg in hit[1:]:
            t = seg[2]
            s = t.text or ""
            cut_end = min(me, seg[1]) - seg[0]
            t.text = s[cut_end:] if me < seg[1] else ""
            if me < seg[1]:
                break

    t1.text = text1[:ls]
    idx = list(holder).index(run1)
    holder.insert(idx + 1, _make_run(run1, ph, highlight=True))
    if after:
        holder.insert(idx + 2, _make_run(run1, after))
    return True


def _redact_tree(root, usable, claimed_report):
    """Redige una part: match sul testo linearizzato, sostituzioni destra->sinistra."""
    text, segs = _walk_part(root)
    if not text.strip():
        return
    parents = {c: p for p in root.iter() for c in p}
    matches = []
    claimed = []
    for ph, _val, pat in usable:
        for m in pat.finditer(text):
            ms, me = m.start(), m.end()
            if any(ms < ce and me > cs for cs, ce in claimed):
                continue                     # già coperto da un valore più lungo
            claimed.append((ms, me))
            matches.append((ms, me, ph))
    for ms, me, ph in sorted(matches, key=lambda x: -x[0]):
        if _replace_match(segs, parents, ms, me, ph):
            claimed_report[ph] = claimed_report.get(ph, 0) + 1


# --------------------------------------------------------------------------- #
# Metadati
# --------------------------------------------------------------------------- #
def _scrub_authors(xml_bytes):
    """Autori dei commenti: nome, iniziali e account sono ATTRIBUTI."""
    root = ET.fromstring(xml_bytes)
    n = 0
    for el in root.iter():
        for attr in ("name", "initials", "userId", "providerId"):
            if el.get(attr):
                el.set(attr, "")
                n += 1
    if n == 0:
        return xml_bytes
    return _serialize_part(root, _part_namespaces(xml_bytes))


# --------------------------------------------------------------------------- #
# API pubblica
# --------------------------------------------------------------------------- #
def redact_pptx(pptx_bytes, mapping, ocr_cache=None):
    """Pptx originale -> pptx con placeholder evidenziati in giallo + report.
    Stesse chiavi di report di redact_docx/redact_pdf. ocr_cache: le part
    ppt/media/* pianificate vengono riscritte coi box gialli nei pixel
    (vedi image_ocr); senza cache le part media passano intatte."""
    if not isinstance(mapping, dict) or not mapping:
        raise PptxError("Dizionario vuoto: anonimizza prima il documento.")
    items = sorted(((ph, v) for ph, v in mapping.items()
                    if isinstance(ph, str) and isinstance(v, str) and v.strip()),
                   key=lambda kv: -len(kv[1]))
    if not items:
        raise PptxError("Dizionario non valido.")

    skipped, usable = [], []
    for ph, val in items:
        if _too_noisy(val, ph):
            skipped.append(ph)
            continue
        pat = _value_pattern(val, ph)
        if pat:
            usable.append((ph, val, pat))
        else:
            skipped.append(ph)

    parts = _load_parts(pptx_bytes)
    by_ph = {}
    n_attrs = 0
    for name in sorted(parts, key=_sort_key):
        root = parts[name][0]
        _redact_tree(root, usable, by_ph)
        # testo alternativo/titolo delle forme: svuotati, non sostituiti
        # (vedi docx._scrub_alt_text)
        n_attrs += _scrub_alt_text(root)
    by_ph = {ph: by_ph.get(ph, 0) for ph, _, _ in usable}

    values = _RelsValues(v for _, v, _ in usable)   # preparati una volta sola
    img_by_ph = {}
    media_redactor = image_ocr.ooxml_media_redactor(ocr_cache, mapping, img_by_ph)
    src = zipfile.ZipFile(io.BytesIO(pptx_bytes))
    out_buf = io.BytesIO()
    n_rels = 0
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename in parts:
                data = _serialize_part(*parts[info.filename])
            elif info.filename == "docProps/core.xml":
                data = _scrub_core_props(data)
            elif info.filename == "docProps/app.xml":
                data = _scrub_app_props(data)
            elif info.filename in _AUTHOR_PARTS:
                data = _scrub_authors(data)
            elif info.filename.endswith(".rels"):
                data, n = _scrub_rels(data, values)
                n_rels += n
            elif media_redactor is not None:
                data = media_redactor(info.filename, data)
            zout.writestr(info.filename, data)
    out = out_buf.getvalue()

    report = {
        "occurrences": sum(by_ph.values()),
        "by_placeholder": by_ph,
        "not_found": [ph for ph, n in by_ph.items() if n == 0],
        "skipped": skipped,
        "residual": _verify_residuals(out, [(ph, v) for ph, v, _ in usable]),
        "rels": n_rels,
        "attributes": n_attrs,
    }
    if ocr_cache:
        image_ocr.merge_image_report(report, img_by_ph)
    return out, report


def rebuild_pptx(pptx_bytes, mapping, preview_pdf_original=None, ctl=None,
                 ocr_cache=None):
    """Redazione del pptx con una mappa GIÀ DATA (niente ri-analisi) + PDF di
    anteprima (via LibreOffice) + box. Stesso contratto di rebuild_docx: è il
    cuore condiviso tra prima anonimizzazione e modifiche alla mappa."""
    from . import convert, pdf
    from .progress import NULL as _NULL_CTL
    ctl = ctl or _NULL_CTL

    if not mapping:
        raise PptxError("La mappa è vuota: niente da redigere.")
    ctl.phase("redaction")
    out, report = redact_pptx(pptx_bytes, mapping, ocr_cache=ocr_cache)
    if report["occurrences"] == 0:
        raise PptxError("Nessuna occorrenza trovata nella presentazione.")

    ctl.phase("preview")
    preview_orig = preview_pdf_original or convert.to_pdf(pptx_bytes, suffix=".pptx")
    preview_anon = convert.to_pdf(out, suffix=".pptx")

    original_boxes = pdf._value_boxes(preview_orig, mapping)
    anonymized_boxes = pdf._placeholder_boxes(preview_anon, mapping)
    # le entità lette nelle immagini non compaiono nel testo dei PDF
    # convertiti (stanno nei pixel): i loro box si ritrovano a parte
    image_ocr.merge_ooxml_overlays(
        ocr_cache, mapping,
        [(original_boxes, preview_orig, pptx_bytes),
         (anonymized_boxes, preview_anon, out)])
    for pages in (original_boxes, anonymized_boxes):
        for blist in pages.values():
            for b in blist:
                b["label"] = pdf._ph_label(b["ph"])

    return {
        "file": out,
        "report": report,
        "original_boxes": original_boxes,
        "anonymized_boxes": anonymized_boxes,
        "preview_pdf_original": preview_orig,
        "preview_pdf_anonymized": preview_anon,
        "page_sizes": {"original": pdf.page_sizes(preview_orig),
                       "anonymized": pdf.page_sizes(preview_anon)},
    }


def anonymize_pptx(pptx_bytes, engine, excluded=None, custom_terms=None, ctl=None,
                   ocr=False):
    """Pptx -> analisi PII + redazione gialla (vedi rebuild_pptx per l'output).
    Con ocr=True le immagini in ppt/media/* passano da RapidOCR allo stesso
    engine.analyze delle slide (vedi image_ocr)."""
    from .progress import NULL as _NULL_CTL
    ctl = ctl or _NULL_CTL
    text = extract_text(pptx_bytes, allow_empty=ocr)

    cache = None
    images = image_ocr.ooxml_images(pptx_bytes) if ocr else []
    ctl.phases((["image_ocr"] if images else [])
               + ["analysis", "redaction", "preview"])
    if images:
        ctl.phase("image_ocr")
        cache = image_ocr.build_cache(images, ctl=ctl)
    if not text.strip() and cache is None:
        raise PptxError("La presentazione non contiene testo e l'OCR non ha "
                        "letto testo nelle immagini.")

    ctl.phase("analysis")
    res = image_ocr.analyze_with_corpus(engine, text, cache, excluded=excluded,
                                        custom_terms=custom_terms, ctl=ctl)
    if not res["mapping"]:
        raise PptxError("Nessuna PII trovata: niente da anonimizzare in questa presentazione.")
    result = rebuild_pptx(pptx_bytes, res["mapping"], ctl=ctl, ocr_cache=cache)
    result["analysis"] = res
    result["ocr_cache"] = cache
    return result


def _verify_residuals(pptx_bytes, items):
    """Placeholder il cui valore è ANCORA leggibile nell'output (testo di
    tutte le part redatte + testo alternativo delle forme + metadati + autori
    + rels). Deve essere []."""
    try:
        text = extract_text(pptx_bytes, full=True)
    except PptxError:
        text = ""
    zf = zipfile.ZipFile(io.BytesIO(pptx_bytes))
    extra = []
    for name in zf.namelist():
        if (name in ("docProps/core.xml", "docProps/app.xml") or
                name in _AUTHOR_PARTS or name.endswith(".rels")):
            extra.append(_meta_surfaces(zf.read(name)))
        elif _REDACT_PART_RE.match(name):
            try:
                extra.extend(_alt_text_surfaces(ET.fromstring(zf.read(name))))
            except ET.ParseError:
                continue
    haystack = text + "\n" + "\n".join(extra)
    residual = []
    for ph, val in items:
        pat = _value_pattern(val, ph)
        if (pat and pat.search(haystack)) or contains_literal(haystack, val, ph):
            residual.append(ph)
    return residual

"""
Orchestrazione PDF: testo -> analisi PII -> redazione GIALLA -> box per l'overlay.

La redazione è quella vera di rizzo-pii (`pdf_export.redact_pdf`: il valore esce
dal content stream), ma con riempimento GIALLO EVIDENZIATORE e testo nero, così
l'anonimizzato mostra a colpo d'occhio cosa è stato coperto.

Oltre al PDF anonimizzato produce i BOX (coordinate PDF, in punti) per pagina:
  - `original_boxes`     dove stavano i valori nel documento originale
                         (evidenziazione gialla lato sinistro della preview);
  - `anonymized_boxes`   i rettangoli di redazione piazzati nell'output
                         (tooltip col valore reale lato destro) — TUTTI,
                         anche quelli troppo piccoli per l'etichetta.
"""

import re

import fitz  # PyMuPDF

from . import image_ocr, pdf_export, pdf_ghost
from .mupdf_lock import mupdf_serialized
from .pdf_export import PdfError  # noqa: F401  (ri-esportato per i chiamanti)

# giallo evidenziatore + testo nero: la richiesta è "evidenziato in giallo"
HIGHLIGHT_FILL = (1.0, 0.92, 0.30)
HIGHLIGHT_TEXT = (0.0, 0.0, 0.0)

# aree sigillate dall'utente: nero pieno con scritta bianca, per distinguerle
# a colpo d'occhio dalle redazioni della mappa (gialle e reversibili)
SEAL_FILL = (0.0, 0.0, 0.0)
SEAL_TEXT = (1.0, 1.0, 1.0)
SEAL_LABEL = "SEALED"


@mupdf_serialized
def extract_text(pdf_bytes, allow_empty=False):
    """Testo del PDF. PdfError se non ha layer testuale (scansione: serve OCR);
    con allow_empty=True il testo vuoto è ammesso (percorso OCR: le pagine
    scansionate si leggono dalle immagini) e si ritorna ("", n_pages).

    Il testo FANTASMA delle scansioni cercabili (trascrizione OCR di terzi
    disegnata invisibile sopra i pixel) NON entra nel risultato: è un doppione
    della lettura che l'OCR dell'app fa sugli stessi pixel, e trattarlo come
    testo del documento porta a due placeholder per la stessa frase. Vedi
    pdf_ghost. Questa è la porta d'ingresso di OGNI lettura di PDF
    dell'applicazione (analisi, sonda pre-upload, ammissibilità in chat),
    quindi il filtro vale per tutte senza doverlo ripetere."""
    stripped, ghosts = pdf_ghost.strip_ghost_text(pdf_bytes)
    try:
        with fitz.open(stream=stripped, filetype="pdf") as doc:
            if doc.needs_pass:
                raise PdfError("PDF protetto da password: rimuovi la protezione e riprova.")
            text = "\n".join(page.get_text() for page in doc)
            n_pages = doc.page_count
    except PdfError:
        raise
    except Exception:
        raise PdfError("File PDF non valido o danneggiato.")
    if not text.strip():
        if allow_empty:
            return "", n_pages
        if ghosts:
            # distinguere il caso vale la pena: l'utente vede un PDF in cui il
            # testo si seleziona e non capirebbe un "non ha testo selezionabile"
            raise PdfError(
                "Il PDF è una scansione con un testo OCR nascosto, prodotto da "
                "chi ha scansionato: è una trascrizione inaffidabile dei pixel, "
                "non il testo del documento, e usarla darebbe un'anonimizzazione "
                "solo apparente. Attiva l'OCR per leggere la scansione.")
        raise PdfError("Il PDF non ha testo selezionabile (probabile scansione): "
                       "l'anonimizzazione del layer testuale non può agire. Serve OCR.")
    return text, n_pages


@mupdf_serialized
def _value_boxes(pdf_bytes, mapping):
    """Box dei VALORI nel documento originale, per pagina: {n: [{x0,y0,x1,y1,ph,label}]}.
    Riusa l'indice char-preciso e le regex ancorate di pdf_export (stesso matching
    della redazione: quello che si evidenzia a sinistra è quello che sparisce a destra)."""
    items = []
    for ph, val in sorted(mapping.items(), key=lambda kv: -len(kv[1])):
        if pdf_export._too_noisy(val):
            continue
        pat = pdf_export._value_pattern(val, ph)
        if pat:
            items.append((ph, pat))
    boxes = {}
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        # ricerca sul testo dell'INTERO documento: un valore a cavallo di un
        # salto pagina non esiste su nessuna pagina presa da sola
        text, pages = pdf_export._doc_char_index(doc)
        if not text.strip():
            return boxes
        # il testo fantasma di una scansione cercabile sta SOPRA l'ink che
        # trascrive: un valore che vi ricade darebbe un secondo riquadro
        # giallo appiccicato al primo (vedi pdf_ghost)
        ghosts = [pdf_ghost.ghost_rects(page)[0] for page in doc]
        taken = [[] for _ in pages]
        occ = 0                         # numero d'occorrenza, vedi redact_pdf
        for ph, pat in items:
            for m in pat.finditer(text):
                found = False
                for pno, span in pdf_export._by_page(pages, m):
                    for r in pdf_export._match_rects(pages[pno][1], span):
                        if pdf_export._covered(r, taken[pno]):
                            continue
                        if pdf_ghost.on_ghost(r, ghosts[pno]):
                            continue
                        taken[pno].append(fitz.Rect(r))
                        boxes.setdefault(pno, []).append(
                            {"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1,
                             "ph": ph, "occ": occ})
                        found = True
                if found:
                    occ += 1
    return boxes


# token "a placeholder" ([FULLNAME_1], [CUSTOM_2], ...): la forma è generica,
# la conferma è l'appartenenza alla mappa (nessuna lista di tag a mano)
_PH_TOKEN_RE = re.compile(r"\[[^\[\]\s]+\]")


@mupdf_serialized
def _placeholder_boxes(pdf_bytes, mapping):
    """Box dei PLACEHOLDER nel PDF anonimizzato, per pagina. La ricerca è
    char-precisa sul testo della pagina (stesso indice di _value_boxes), NON
    per parole: nelle anteprime dei fogli di calcolo un placeholder più largo
    della sua cella sborda su quella accanto e nel layer testuale finisce
    incollato al testo vicino senza spazi ("[IBAN_1]Vistaclear" = una sola parola,
    che il match esatto perderebbe -> niente tooltip). Le quadre delimitano il
    match, quindi [FULLNAME_1] non si confonde con [FULLNAME_12], e il token
    trovato vale solo se sta nella mappa."""
    boxes = {}
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        text, pages = pdf_export._doc_char_index(doc)
        occ = 0                         # numero d'occorrenza, vedi redact_pdf
        for m in _PH_TOKEN_RE.finditer(text):
            ph = m.group(0)
            if ph not in mapping:
                continue
            for pno, span in pdf_export._by_page(pages, m):
                for r in pdf_export._match_rects(pages[pno][1], span):
                    boxes.setdefault(pno, []).append(
                        {"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1,
                         "ph": ph, "occ": occ})
            occ += 1
    return boxes


def _ph_label(ph):
    """"[FULLNAME_1]" -> "FULLNAME": inverso esatto del formato di analyze()
    (f"[{label}_{n}]"), così la categoria si deriva dal placeholder stesso e
    nessuna lista di tag va mantenuta a mano."""
    core = ph.strip("[]")
    return core.rsplit("_", 1)[0] if "_" in core else core


def rebuild_pdf(pdf_bytes, mapping, ocr_cache=None):
    """Redazione del PDF con una mappa GIÀ DATA (niente ri-analisi del modello).

    È il cuore condiviso tra la prima anonimizzazione (mappa di analyze) e le
    MODIFICHE successive: de-anonimizzare un segnaposto/una categoria o
    aggiungere un tag custom = ri-redigere l'originale con la mappa aggiornata.

    ocr_cache (opzionale): righe OCR + piano di redazione delle immagini
    (vedi image_ocr). PRIMA le immagini vengono sostituite con la versione
    redatta nei pixel, POI passa la redazione testuale: apply_redactions
    cancella i pixel sotto i rect del layer testo e deve farlo sulle immagini
    già redatte. Nessun OCR qui: la cache rende la ri-redazione deterministica.

    Il testo fantasma (pdf_ghost) esce dentro pdf_export.redact_pdf, cioè DOPO
    la redazione delle immagini: è voluto e va lasciato in quest'ordine. Lo
    strip riscrive il documento con `tobytes(garbage=3)`, che può RINUMERARE
    gli xref, e le voci della cache OCR sono indicizzate proprio per xref
    (image_ocr.pdf_images) — anticiparlo scollegherebbe la cache dalle immagini
    e la redazione dei pixel non troverebbe più nulla da coprire.
    """
    if not mapping:
        raise PdfError("La mappa è vuota: niente da redigere.")
    src, img_overlay, img_by_ph = pdf_bytes, {}, {}
    if ocr_cache:
        src, img_overlay, img_by_ph = image_ocr.redact_pdf_images(
            pdf_bytes, ocr_cache, mapping)
    out, report = pdf_export.redact_pdf(src, mapping,
                                        fill=HIGHLIGHT_FILL, text_color=HIGHLIGHT_TEXT)
    if ocr_cache:
        image_ocr.merge_image_report(report, img_by_ph)
    if report["occurrences"] == 0:
        raise PdfError("Nessuna occorrenza trovata nel PDF: se il documento è una "
                       "scansione la redazione del layer testuale non può agire.")

    original_boxes = _value_boxes(pdf_bytes, mapping)
    # i box dell'anonimizzato sono i rettangoli di redazione REALMENTE piazzati
    # (report["boxes"]), non i placeholder ritrovati nel testo: un'etichetta
    # che non ci sta nel box non viene disegnata, ma il box resta interattivo
    anonymized_boxes = report.pop("boxes")
    # i box delle immagini valgono su ENTRAMBI i lati: l'immagine non si sposta,
    # a sinistra segnano dove sta il valore, a destra il box giallo nei pixel
    for pno, blist in img_overlay.items():
        original_boxes.setdefault(pno, []).extend(dict(b) for b in blist)
        anonymized_boxes.setdefault(pno, []).extend(dict(b) for b in blist)
    for pages in (original_boxes, anonymized_boxes):
        for blist in pages.values():
            for b in blist:
                b["label"] = _ph_label(b["ph"])

    return {
        "pdf": out,
        "report": report,
        "original_boxes": original_boxes,
        "anonymized_boxes": anonymized_boxes,
    }


@mupdf_serialized
def seal_pdf(pdf_bytes, sealed):
    """Applica al PDF (già redatto) le aree SIGILLATE dall'utente: redazione
    VERA con riempimento nero e scritta bianca "SEALED". Tutto ciò che sta
    sotto il rettangolo — testo e pixel delle immagini — esce dal file
    (apply_redactions), non viene solo coperto, e NON è decodificabile.

    sealed: [{"n", "page", "all", "rect"}] con rect in punti PDF; con
    all=True lo stesso rettangolo vale su OGNI pagina del documento.
    Ritorna (bytes, boxes) con boxes = {pagina: [{x0,y0,x1,y1,ph,label,
    sealed}]} per l'overlay interattivo (sealed = il numero dell'area,
    con cui si chiede la rimozione)."""
    if not sealed:
        return pdf_bytes, {}
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        raise PdfError("File PDF non valido o danneggiato.")
    per_page = {}
    for s in sealed:
        pages = range(doc.page_count) if s.get("all") else [int(s["page"])]
        for pno in pages:
            if not (0 <= pno < doc.page_count):
                continue
            r = fitz.Rect(s["rect"])
            r.intersect(doc[pno].rect)
            if r.is_empty:
                continue
            per_page.setdefault(pno, []).append((r, s["n"]))
    boxes = {}
    for pno in sorted(per_page):
        page = doc[pno]
        items = per_page[pno]
        for r, _n in items:
            pdf_export._add_redact_annot(page, r, SEAL_FILL)
        page.apply_redactions()
        # etichetta a valle, come in redact_pdf: o ci sta tutta o niente
        for r, n in items:
            fs = pdf_export._fit_fontsize(SEAL_LABEL, r)
            if fs:
                pdf_export._draw_label(page, r, SEAL_LABEL, fs, SEAL_TEXT)
            boxes.setdefault(pno, []).append(
                {"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1,
                 "ph": f"[{SEAL_LABEL}_{n}]", "label": SEAL_LABEL, "sealed": n})
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out, boxes


def anonymize_pdf(pdf_bytes, engine, excluded=None, custom_terms=None, ctl=None,
                  ocr=False):
    """PDF -> (PDF anonimizzato giallo, mapping, report, box, dimensioni pagine).

    Con ocr=True le IMMAGINI del PDF vengono lette con RapidOCR e il testo
    letto si accoda al testo di pagina prima di engine.analyze: stessi
    placeholder, stessi contatori, stesse esclusioni. Le entità ricadute
    nelle immagini diventano box gialli nei pixel; un PDF scansionato senza
    layer testuale diventa così lavorabile, e lo stesso vale per le PAGINE
    VETTORIALI (testo stampato come curve da "Print To PDF" e simili), rese
    e lette a pagina intera — nella redazione la pagina diventa raster e i
    tracciati originali escono dal file (vedi image_ocr). Il percorso del
    testo estratto è lo STESSO con o senza OCR: l'OCR aggiunge righe, non
    cambia il modo in cui il testo nativo viene letto e redatto.

    Sulle scansioni CERCABILI l'unica fonte è l'OCR: la trascrizione
    invisibile che lo scanner ha lasciato sopra i pixel non conta come testo
    del documento (sarebbe la stessa frase una seconda volta, con una lettura
    diversa) e non arriva nell'output. Vedi pdf_ghost. Conseguenza voluta: con
    ocr=False un file così non produce più una mezza anonimizzazione guidata
    da quella trascrizione, ma si ferma dicendo di attivare l'OCR.

    Solleva PdfError su PDF illeggibile/scansione/zero occorrenze redatte
    (meglio un errore che un PDF 'anonimizzato' che non lo è)."""
    from .progress import NULL as _NULL_CTL
    ctl = ctl or _NULL_CTL
    text, _ = extract_text(pdf_bytes, allow_empty=ocr)

    cache = None
    images = image_ocr.pdf_images(pdf_bytes) if ocr else []
    ctl.phases((["image_ocr"] if images else []) + ["analysis", "redaction"])
    if images:
        ctl.phase("image_ocr")
        cache = image_ocr.build_cache(images, ctl=ctl)
    if not text.strip() and cache is None:
        raise PdfError("Il PDF non ha testo selezionabile e l'OCR non ha letto "
                       "testo nelle immagini: niente da anonimizzare.")

    ctl.phase("analysis")
    res = image_ocr.analyze_with_corpus(engine, text, cache, excluded=excluded,
                                        custom_terms=custom_terms, ctl=ctl)
    mapping = res["mapping"]
    if not mapping:
        raise PdfError("Nessuna PII trovata: niente da anonimizzare in questo documento.")
    ctl.phase("redaction")
    result = rebuild_pdf(pdf_bytes, mapping, ocr_cache=cache)
    result["analysis"] = res
    result["ocr_cache"] = cache
    return result


@mupdf_serialized
def text_in_rect(pdf_bytes, n, rect):
    """Testo in ordine di lettura dentro `rect` ([x0,y0,x1,y1] in punti PDF)
    della pagina n: una parola è inclusa se il suo box è coperto per almeno
    metà. È l'estrazione dietro la selezione manuale nella preview.

    Le parole del testo FANTASMA (trascrizione invisibile di una scansione,
    vedi pdf_ghost) non contano: altrimenti una selezione su una pagina
    scansionata tornerebbe la lettura storpiata dello scanner e il chiamante
    non attiverebbe mai il fallback OCR sul ritaglio, che è la strada giusta
    lì. Qui non si riscrive il documento — si scartano le parole, che costa
    una frazione."""
    sel = fitz.Rect(rect)
    if sel.is_empty:
        return ""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        if not (0 <= n < doc.page_count):
            return ""
        words = doc[n].get_text("words")
        ghosts, _n = pdf_ghost.ghost_rects(doc[n])
    picked = []
    for x0, y0, x1, y1, word, bno, lno, wno in words:
        r = fitz.Rect(x0, y0, x1, y1)
        if r.is_empty or (r & sel).get_area() < 0.5 * r.get_area():
            continue
        if pdf_ghost.on_ghost(r, ghosts):
            continue
        picked.append((bno, lno, wno, word))
    picked.sort()
    return re.sub(r"\s+", " ", " ".join(w for *_, w in picked)).strip()


_COL_HEADING_RE = re.compile(r"^[A-Z]{1,3}$")


def _heading_col_idx(letters):
    """"A" -> 0, "AA" -> 26 (stessa numerazione di xlsx._letter_idx)."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _group_lines(words, tol=2.0):
    """Raggruppa le parole in righe orizzontali per y0 (tolleranza in punti)."""
    lines = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        if lines and abs(w[1] - lines[-1][0][1]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return lines


def _band_boundaries(page, y0, y1):
    """Ascisse dei bordi verticali disegnati che attraversano la banda
    [y0, y1]: con printOptions gridLines/headings la conversione LibreOffice
    disegna le celle-intestazione, e questi sono i confini ESATTI delle
    colonne. Deduplicate e ordinate."""
    xs = []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.x - p2.x) < 0.5 and min(p1.y, p2.y) <= y1 \
                        and max(p1.y, p2.y) >= y0:
                    xs.append((p1.x + p2.x) / 2)
            elif it[0] == "re":
                r = it[1]
                if r.y0 <= y1 and r.y1 >= y0:
                    xs.extend((r.x0, r.x1))
    xs.sort()
    out = []
    for x in xs:
        if not out or x - out[-1] > 0.7:
            out.append(x)
    return out


@mupdf_serialized
def spreadsheet_columns(pdf_bytes, sheet_names):
    """Layout cliccabile delle colonne di una preview xlsx.

    Per ogni pagina del PDF (convertito da LibreOffice con printOptions
    headings="1" e nome foglio nell'intestazione di pagina, vedi
    xlsx._spreadsheet_look) individua: il FOGLIO di appartenenza (il nome
    stampato in testa alla pagina) e i BOX in punti PDF delle intestazioni di
    colonna A/B/C — che sono testo vero nel layer testuale, non pixel.

    Ritorna {pagina: {"sheet": nome, "cols": [{"col","x0","y0","x1","y1"}]}}.
    Le pagine in cui il riconoscimento fallisce mancano dal risultato:
    nessun overlay, degradazione morbida."""
    from .core import _norm
    by_norm = {_norm(sn): sn for sn in sheet_names or ()}
    out = {}
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for pno, page in enumerate(doc):
            words = page.get_text("words")
            if not words:
                continue
            top = page.rect.height * 0.25
            lines = _group_lines([w for w in words if w[1] < top])

            # riga delle intestazioni: la prima (dall'alto) fatta SOLO di
            # token A/AB/ABC con indici di colonna strettamente crescenti
            head, head_i = None, None
            for i, ln in enumerate(lines):
                toks = [w[4] for w in ln]
                if toks and all(_COL_HEADING_RE.match(t) for t in toks):
                    idxs = [_heading_col_idx(t) for t in toks]
                    if all(a < b for a, b in zip(idxs, idxs[1:])):
                        head, head_i = ln, i
                        break
            if head is None:
                continue
            hy0 = min(w[1] for w in head)
            hy1 = max(w[3] for w in head)

            # conferma: i numeri di riga stampati a sinistra, sotto la banda
            # (headings="1" stampa sempre entrambe le intestazioni)
            first_x0 = head[0][0]
            row_nums = [w for w in words
                        if w[1] > hy1 - 1 and w[4].isdigit() and w[2] <= first_x0 + 2]
            if not row_nums:
                continue

            # foglio: la riga di testo SOPRA le intestazioni che coincide con
            # un nome di foglio (l'intestazione di pagina &C&A)
            sheet = None
            for ln in lines[:head_i]:
                txt = _norm(" ".join(w[4] for w in ln))
                if txt in by_norm:
                    sheet = by_norm[txt]
                    break
            if sheet is None:
                continue

            # confini delle celle-intestazione: prima i bordi DISEGNATI della
            # banda (esatti), in mancanza i punti medi tra i centri dei token
            centers = [((w[0] + w[2]) / 2, w[4]) for w in head]
            bounds = _band_boundaries(page, hy0, hy1)
            cols = []
            for c, letter in centers:
                left = max((x for x in bounds if x < c), default=None)
                right = min((x for x in bounds if x > c), default=None)
                if left is None or right is None:
                    cols = None
                    break
                cols.append({"col": letter, "x0": left, "y0": hy0,
                             "x1": right, "y1": hy1})
            if cols is None or _overlapping(cols):
                mids = [(centers[i][0] + centers[i + 1][0]) / 2
                        for i in range(len(centers) - 1)]
                row_x1 = max(w[2] for w in row_nums)
                lefts = [min(row_x1, head[0][0] - 1)] + mids
                rights = mids + [2 * centers[-1][0] - mids[-1] if mids
                                 else page.rect.width]
                cols = [{"col": letter, "x0": lefts[i], "y0": hy0,
                         "x1": rights[i], "y1": hy1}
                        for i, (_c, letter) in enumerate(centers)]
            out[pno] = {"sheet": sheet, "cols": cols}
    return out


def _overlapping(cols):
    """True se due box condividono un bordo (celle non separate: il disegno
    della banda non era affidabile, meglio il fallback sui punti medi)."""
    for a, b in zip(cols, cols[1:]):
        if b["x0"] < a["x1"] - 0.5:
            return True
    return False


@mupdf_serialized
def render_page_png(pdf_bytes, n, dpi=110):
    """PNG della pagina n (0-based); None se fuori range. 110 dpi ~ 1150px su A4."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        if not (0 <= n < doc.page_count):
            return None
        page = doc.load_page(n)
        return {
            "png": page.get_pixmap(dpi=dpi).tobytes("png"),
            "width": page.rect.width,
            "height": page.rect.height,
        }


@mupdf_serialized
def page_sizes(pdf_bytes):
    """[{width, height}] in punti PDF, per mappare i box sull'immagine renderizzata."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return [{"width": p.rect.width, "height": p.rect.height} for p in doc]

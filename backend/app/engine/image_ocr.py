"""
OCR sulle IMMAGINI dentro i documenti (opzionale, scelto dall'utente all'avvio
dell'anonimizzazione): il testo letto nelle immagini passa allo STESSO
`engine.analyze` del testo di pagina, e le entità trovate vengono coperte con
box gialli disegnati NEI PIXEL (redazione distruttiva: il contenuto sotto il
box non esiste più nell'output).

Il flusso è in due tempi, come per il testo:

  detect (una volta sola, al primo job):
    1. si enumerano le immagini del contenitore (PDF: xref; OOXML: part
       word|ppt|xl/media/*) e si OCR-izzano con RapidOCR PP-OCRv5 mobile
       (ONNX, CPU, ~1s/immagine a modelli caldi). Nei PDF partecipano anche
       le PAGINE VETTORIALI (testo stampato come curve da "Print To PDF" e
       simili: niente layer testuale, niente immagini, solo tracciati),
       rese a pagina intera come immagini `page:n` — vedi _vector_page;
    2. le righe lette formano un CORPUS che il chiamante accoda al testo del
       documento prima di `engine.analyze`: placeholder, contatori, dedup ed
       esclusioni restano quelli del motore vero (nessuna euristica locale);
    3. gli offset delle entità ricadute nel corpus tornano qui e diventano il
       PIANO: (immagine, riga, intervallo char, placeholder). Le righe a bassa
       confidenza SENZA entità si coprono per intero come [UNREADABLE_n]
       (politica conservativa: ciò che non si riesce a leggere non si può
       dichiarare privo di PII);
    4. righe + piano vengono persistiti dal chiamante (cache JSON).

  redact (a ogni redazione, prima e dopo ogni modifica alla mappa):
    dal piano si disegnano i box dei placeholder ANCORA in mappa (una
    de-anonimizzazione toglie il suo box alla ri-redazione) e si ri-cercano
    nelle righe i valori aggiunti DOPO il detect (termini custom): stessa
    `_value_pattern` della redazione testuale. Niente OCR in questa fase:
    la cache rende la ri-redazione deterministica e veloce.

Al detect partecipa anche un DETECTOR di firme e timbri (YOLO11n fine-tuned,
ONNX ~10MB in models/, ~100ms/immagine su CPU): le grafie non testuali che
l'OCR non vede (ghirigori di firma, timbri tondi) diventano REGIONI della
cache e si coprono per intero come [SIGNATURE_n] — niente estrazione di
testo, solo copertura. Stesso interruttore dell'OCR: opzione spenta = niente
detector.

L'OCR non tocca MAI il percorso del testo: senza cache (opzione spenta) ogni
funzione di questo modulo è un no-op e il documento esce come se il modulo
non esistesse.
"""

import io
import os
import re
import threading

from .mupdf_lock import mupdf_serialized
from .progress import NULL as _NULL_CTL
from . import pdf_image_regions

# ext delle immagini che Pillow sa riscrivere e RapidOCR sa leggere
# (niente vettoriali: emf/wmf/svg dentro gli OOXML restano intatti)
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")

MIN_SIDE = 32          # px: sotto questa taglia (icone, bullet) niente testo leggibile
# Il rilevatore (DBNet) non chiude una regione che tocca il bordo del
# fotogramma: in un LOGO la scritta grande sta a filo dell'immagine e non
# produceva nessun box a qualunque scala, mentre la riga piccola sotto
# (con margine) si leggeva. Il ritaglio della selezione manuale (che ha
# margini per costruzione) la leggeva, e la ri-redazione dalla cache no.
# Ogni immagine si incornicia quindi PRIMA dell'OCR con il colore mediano
# del bordo (non bianco: su fondo scuro una cornice chiara è essa stessa un
# bordo netto); i box tornano in pixel dell'immagine originale.
# Misurato: il margine che serve è ~0,65 volte l'ALTEZZA del testo (51 px:
# 24 sì, 16 no; 92 px: 60 sì, 43 no). Un testo non è più alto del lato
# corto dell'immagine, quindi la cornice è quella frazione del lato corto,
# con un minimo assoluto. Il tetto tiene l'immagine incorniciata entro i
# _OCR_MAX_SIDE px oltre i quali RapidOCR la RIDURREBBE (Global.max_side_len
# — le immagini più piccole non le riduce mai: limit_type "min" le
# ingrandisce): una scansione A4 a PAGE_DPI resta alla sua risoluzione.
_OCR_FRAME = 0.65      # cornice: frazione del lato MINORE dell'immagine
_OCR_FRAME_MIN = 32    # px: sotto, anche il testo alto quanto un'icona resta tagliato
_OCR_MAX_SIDE = 2000   # px: Global.max_side_len di RapidOCR (config.yaml)

PAGE_KEY = "page:"     # chiave cache delle pagine vettoriali rese per intero
PAGE_DPI = 150         # render delle pagine vettoriali: buon OCR, ~1240px su A4
_VEC_MIN_CONTENT = 2048   # byte di content stream sotto cui una pagina senza
                          # testo è considerata vuota (niente da leggere)
_VEC_IMG_COVER = 0.5      # copertura immagini oltre cui la pagina è una
                          # scansione classica: la leggono già le sue immagini
MIN_SCORE = 0.80       # confidenza sotto cui una riga senza entità è "illeggibile"
UNREADABLE = "UNREADABLE"
SIGNATURE = "SIGNATURE"
# valore in mappa delle regioni firma/timbro: non c'è testo da restituire, e
# la frase intera (con confini di parola, vedi _value_pattern) non può
# comparire in un documento vero — la ri-ricerca nel testo non trova nulla
SIG_VALUE = "firma o timbro non testuale"

SIG_CONF = 0.25        # confidenza minima del detector: i timbri veri stanno
                       # sopra 0.45 e sotto questa soglia non ci sono falsi
                       # positivi sul testo
SIG_IOU = 0.45         # soglia NMS
_DET_SIZE = 640        # input del modello, con squish e non letterbox: il
                       # letterbox schiaccia i timbri sotto la soglia
# Alifgala/signature-detection-yolo (YOLO11n fine-tuned, classe unica
# "signature": becca firme E timbri), export ONNX opset 17 statico 640x640
_SIG_MODEL = os.path.join(os.path.dirname(__file__), "models",
                          "signature_yolo11n.onnx")

YELLOW = (255, 235, 77)     # lo stesso giallo evidenziatore di HIGHLIGHT_FILL
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)     # testo delle aree sigillate (nero pieno sotto)

_ocr = None
_ocr_lock = threading.Lock()        # protegge l'init E l'inferenza (non thread-safe)
_detector = None                    # sessione ONNX; False = provato, non disponibile
_det_lock = threading.Lock()


class OcrError(ValueError):
    """Errore user-facing del percorso OCR."""


def available():
    """True se lo stack OCR (rapidocr + onnxruntime + Pillow) è importabile.
    Le route lo usano per degradare con garbo dove i pacchetti mancano."""
    try:
        import PIL  # noqa: F401
        import rapidocr  # noqa: F401
        return True
    except Exception:
        return False


def _get_ocr():
    """Singleton RapidOCR PP-OCRv5 mobile su onnxruntime, init lazy.

    Se mancano, RapidOCR scarica i modelli (~20 MB) nella propria model dir;
    l'immagine Docker li pre-carica durante la build, prima di passare
    all'utente non privilegiato. Da caldi l'avvio costa ~1s. La v5 MOBILE è
    il compromesso misurato sui documenti di prova: qualità 0.91, spaziatura
    fra le parole corretta, ~6s/pagina su CPU d'ufficio."""
    global _ocr
    if _ocr is None:
        try:
            from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR
        except Exception:
            raise OcrError("OCR non disponibile su questo server: pacchetti "
                           "rapidocr/onnxruntime non installati.")
        _ocr = RapidOCR(params={
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Det.model_type": ModelType.MOBILE,
            "Rec.model_type": ModelType.MOBILE,
            "Global.use_cls": False,
        })
    return _ocr


def _get_detector():
    """Sessione ONNX del detector firme/timbri, init lazy. None se il modello
    manca o onnxruntime non c'è: il percorso OCR prosegue senza regioni."""
    global _detector
    with _det_lock:
        if _detector is None:
            try:
                import onnxruntime as ort
                _detector = ort.InferenceSession(
                    _SIG_MODEL, providers=["CPUExecutionProvider"])
            except Exception:
                _detector = False
    return _detector or None


def detect_regions(pil):
    """Regioni firma/timbro di un'immagine PIL RGB:
    [{"b": [x0,y0,x1,y1] px, "s": confidenza}]. Decodifica YOLO a mano
    (output (1, 5, N): cx,cy,w,h,conf su 640x640) + NMS greedy: niente
    dipendenze oltre onnxruntime/numpy già richiesti da RapidOCR."""
    sess = _get_detector()
    if sess is None:
        return []
    import numpy as np
    from PIL import Image

    w0, h0 = pil.size
    x = np.asarray(pil.resize((_DET_SIZE, _DET_SIZE), Image.BILINEAR),
                   dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
    with _det_lock:
        (out,) = sess.run(None, {sess.get_inputs()[0].name: x})
    pred = out[0].T
    pred = pred[pred[:, 4] >= SIG_CONF]
    if not len(pred):
        return []
    cx, cy, w, h = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
    boxes = np.stack([(cx - w / 2) * w0 / _DET_SIZE,
                      (cy - h / 2) * h0 / _DET_SIZE,
                      (cx + w / 2) * w0 / _DET_SIZE,
                      (cy + h / 2) * h0 / _DET_SIZE], 1)
    boxes = np.clip(boxes, 0, [w0, h0, w0, h0])
    keep = _nms(boxes, pred[:, 4])
    return [{"b": [round(float(v)) for v in boxes[i]],
             "s": round(float(pred[i, 4]), 3)} for i in keep]


def _nms(boxes, scores):
    import numpy as np
    idx = np.argsort(scores)[::-1]
    keep = []
    while idx.size:
        i = idx[0]
        keep.append(int(i))
        rest = idx[1:]
        if not rest.size:
            break
        xx0 = np.maximum(boxes[i, 0], boxes[rest, 0])
        yy0 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx1 = np.minimum(boxes[i, 2], boxes[rest, 2])
        yy1 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.clip(xx1 - xx0, 0, None) * np.clip(yy1 - yy0, 0, None)
        a_i = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        a_r = (boxes[rest, 2] - boxes[rest, 0]) * (boxes[rest, 3] - boxes[rest, 1])
        iou = inter / (a_i + a_r - inter + 1e-9)
        idx = rest[iou <= SIG_IOU]
    return keep


# --------------------------------------------------------------------------- #
# Enumerazione delle immagini nei contenitori
# --------------------------------------------------------------------------- #
def _stencil_positive(doc, xref, info):
    """extract_image su una /ImageMask (il layer testo 1-bit degli scanner
    "a due livelli") restituisce i pixel al NEGATIVO del visivo: glifi
    chiari su fondo nero. Reincorporata così com'è da replace_image
    diventerebbe un negativo opaco a tutta pagina (sfondo nero in ogni
    viewer), quindi si riporta al positivo. Ritorna (bytes, ext)."""
    if doc.xref_get_key(xref, "ImageMask") != ("bool", "true"):
        return info["image"], info["ext"].lower()
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(info["image"])) as im:
        pos = ImageOps.invert(im.convert("L"))
    buf = io.BytesIO()
    pos.save(buf, format="PNG")
    return buf.getvalue(), "png"


def _vector_page(page):
    """True se la pagina va letta con l'OCR a PAGINA INTERA: nessun testo
    estraibile, contenuto disegnato non banale e immagini che NON la coprono.
    È il caso dei PDF "ristampati" (Microsoft Print To PDF e simili) dove
    ogni glifo diventa curve vettoriali: leggibile a occhio, invisibile sia
    all'estrazione testo sia all'enumerazione delle immagini. La scansione
    classica (immagine a tutta pagina) resta sul percorso per xref."""
    if page.get_text().strip():
        return False
    try:
        if len(page.read_contents() or b"") < _VEC_MIN_CONTENT:
            return False                      # pagina bianca o quasi
        infos = page.get_image_info()
    except Exception:
        return False
    area = abs(page.rect) or 1.0
    cover = sum(max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
                for b in (i["bbox"] for i in infos))
    # Long image-placement streams are not vector text: a small scan can be
    # stored as dozens of strips without covering half the page. Require real
    # vector artwork before choosing the full-page vector rendering fallback.
    return cover / area < _VEC_IMG_COVER and bool(page.get_drawings())


@mupdf_serialized
def pdf_images(pdf_bytes, render_pages=True):
    """Raster images, composed tile regions and rendered vector-only pages.

    Adjacent tiles use region:n:k keys with pdf_region placement metadata.
    OCR sees the composed image resources only; native text stays on its own
    extraction path. Isolated images (including header logos) keep xref keys.

    Immagini raster del PDF più le pagine vettoriali rese per intero:
    [{"key": str(xref) | "page:n", "page": n, "ext", "data"}].
    Dedup per xref (la stessa immagine può comparire su più pagine);
    l'ordine segue la prima pagina d'uso, così la numerazione dei placeholder
    rispecchia l'ordine di lettura. Le immagini incorporate in una pagina
    vettoriale NON si enumerano a parte: stanno già dentro il suo render.
    render_pages=False skips rendering both regions and vector pages.
    Salta il render (costoso) delle pagine vettoriali e le
    ritorna senza "data": serve solo a contarle (count_images)."""
    import fitz
    out, seen = [], set()
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for pno in range(doc.page_count):
            page = doc[pno]
            if _vector_page(page):
                entry = {"key": f"{PAGE_KEY}{pno}", "page": pno, "ext": "png"}
                if render_pages:
                    if page.rotation:
                        page.remove_rotation()
                    entry["data"] = page.get_pixmap(dpi=PAGE_DPI).tobytes("png")
                out.append(entry)
                continue
            groups = pdf_image_regions.tile_groups(page)
            out.extend(pdf_image_regions.render_groups(
                page, groups, PAGE_DPI, _OCR_MAX_SIDE - 2 * _OCR_FRAME_MIN,
                render=render_pages))
            grouped = {i['xref'] for group in groups for i in group}
            # A shared xref can also have an isolated placement on this page.
            # Keep its standalone OCR in that case, and union all pixel masks.
            grouped_indices = {i['index'] for group in groups for i in group}
            if grouped:
                standalone = {i['xref'] for index, i in
                              enumerate(page.get_image_info(xrefs=True))
                              if index not in grouped_indices}
                grouped -= standalone
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in grouped:
                    continue
                if xref in seen:
                    continue
                seen.add(xref)
                if min(img[2], img[3]) < MIN_SIDE:
                    continue
                try:
                    info = doc.extract_image(xref)
                except Exception:
                    continue
                if "." + info["ext"].lower() not in IMAGE_EXTS:
                    continue
                data, ext = _stencil_positive(doc, xref, info)
                out.append({"key": str(xref), "page": pno,
                            "ext": ext, "data": data})
    return out


_MEDIA_RE = re.compile(r"^(word|ppt|xl)/media/[^/]+$")


def ooxml_images(zip_bytes):
    """Immagini nelle part media di un OOXML: [{"key": nome_part, "ext", "data"}].
    L'ext si legge dal nome della part; le taglie minime si filtrano dopo
    l'apertura Pillow (lo zip non conosce i pixel)."""
    import zipfile
    out = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return out
    for name in zf.namelist():
        if not _MEDIA_RE.match(name):
            continue
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in IMAGE_EXTS:
            continue
        data = zf.read(name)
        if _too_small(data):
            continue
        out.append({"key": name, "ext": ext.lstrip("."), "data": data})
    return out


def _too_small(data):
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return min(im.size) < MIN_SIDE
    except Exception:
        return True


def count_images(data, ext):
    """Quante immagini OCR-izzabili contiene il file (per il probe pre-job e
    per l'has_images degli allegati chat): nei PDF contano anche le pagine
    vettoriali, senza pagarne il render. Mai un'eccezione: 0 su file rotti."""
    try:
        low = (ext or "").lower()
        if low == ".pdf":
            return len(pdf_images(data, render_pages=False))
        if low in (".docx", ".pptx", ".xlsx", ".xlsm"):
            return len(ooxml_images(data))
        if low in IMAGE_EXTS:
            return 0 if _too_small(data) else 1
    except Exception:
        pass
    return 0


# --------------------------------------------------------------------------- #
# OCR -> cache {immagini, righe}
# --------------------------------------------------------------------------- #
def _framed(pil, pad):
    """Array BGR (convenzione cv2, come vuole RapidOCR) dell'immagine con una
    cornice di `pad` px per lato del colore MEDIANO del bordo. Chi legge i
    box sottrae `pad` per tornare ai pixel dell'immagine originale."""
    import numpy as np
    from PIL import Image
    if pad <= 0:
        return np.asarray(pil)[:, :, ::-1]
    edge = np.asarray(pil)
    edge = np.concatenate([edge[0], edge[-1], edge[:, 0], edge[:, -1]])
    framed = Image.new("RGB", (pil.width + 2 * pad, pil.height + 2 * pad),
                       tuple(int(v) for v in np.median(edge, axis=0)))
    framed.paste(pil, (pad, pad))
    return np.asarray(framed)[:, :, ::-1]


def _ocr_pad(pil):
    """Cornice (px per lato) per l'OCR dell'immagine intera: vedi _OCR_FRAME."""
    want = int(min(pil.width, pil.height) * _OCR_FRAME)
    room = (_OCR_MAX_SIDE - max(pil.width, pil.height)) // 2
    return max(_OCR_FRAME_MIN, min(want, room))


def build_cache(images, ctl=None):
    """OCR di tutte le immagini -> cache serializzabile:
      {"v": 1, "images": [{"key", "page"?, "ext", "w", "h",
                           "lines": [{"t", "s", "b": [x0,y0,x1,y1]}],
                           "regions": [{"b": [x0,y0,x1,y1], "s": conf}],
                           "plan": [{"line", "s", "e", "ph"} |
                                    {"region", "ph"}]}]}
    `regions` sono le firme/timbri del detector (grafie non testuali, da
    coprire per intero); `plan` nasce vuoto: lo riempiono
    plan_from_entities/allocate_unreadable/allocate_signatures.
    ctl: un tick per immagine + checkpoint di annullamento.
    Ritorna None se l'OCR non ha letto NULLA e il detector non ha trovato
    NULLA: per il chiamante equivale a "niente immagini"."""
    from PIL import Image

    ctl = ctl or _NULL_CTL
    ocr = _get_ocr()
    cache = {"v": 1, "images": []}
    total = len(images)
    ctl.tick(0, total)
    for i, img in enumerate(images):
        ctl.check()
        try:
            pil = Image.open(io.BytesIO(img["data"])).convert("RGB")
        except Exception:
            continue
        pad = _ocr_pad(pil)                  # vedi _OCR_FRAME
        with _ocr_lock:
            res = ocr(_framed(pil, pad))
        lines = []
        boxes = res.boxes if res.boxes is not None else []
        for box, text, score in zip(boxes, res.txts or [], res.scores or []):
            if not (text or "").strip():
                continue
            # px della cornice -> px dell'immagine, entro i suoi bordi
            xs = [min(max(float(p[0]) - pad, 0.0), float(pil.width)) for p in box]
            ys = [min(max(float(p[1]) - pad, 0.0), float(pil.height)) for p in box]
            lines.append({"t": text, "s": round(float(score), 3),
                          "b": [round(min(xs)), round(min(ys)),
                                round(max(xs)), round(max(ys))]})
        try:
            regions = detect_regions(pil)
        except Exception:
            regions = []            # detector rotto != OCR rotto: si prosegue
        entry = {"key": img["key"], "ext": img["ext"],
                 "w": pil.width, "h": pil.height, "lines": lines,
                 "regions": regions, "plan": []}
        if "page" in img:
            entry["page"] = img["page"]
        if "pdf_region" in img:
            entry["pdf_region"] = img["pdf_region"]
        cache["images"].append(entry)
        ctl.tick(i + 1, total)
    if not any(img["lines"] or img["regions"] for img in cache["images"]):
        return None
    return cache


def build_pdf_cache(pdf_bytes, images=None, ctl=None):
    """Image OCR plus one signature-only pass on each complete PDF page."""
    from . import pdf_signatures
    if images is None:
        images = pdf_images(pdf_bytes)
    cache = build_cache(images, ctl=ctl) if images else None
    return pdf_signatures.add_page_pass(pdf_bytes, cache, ctl=ctl)


def analyze_with_corpus(engine, text, cache, excluded=None, custom_terms=None,
                        ctl=None):
    """`engine.analyze` su testo documento + corpus OCR in UNA sola chiamata:
    placeholder, contatori, dedup (label, valore normalizzato) ed esclusioni
    valgono per testo e immagini insieme — un valore presente in entrambi
    riceve lo STESSO placeholder. A valle riempie il piano della cache
    (entità con offset nel corpus -> (immagine, riga, intervallo)) e copre le
    righe illeggibili. Con cache=None è esattamente engine.analyze."""
    if not cache:
        return engine.analyze(text, excluded=excluded,
                              custom_terms=custom_terms, ctl=ctl)
    corpus_text, spans = corpus(cache)
    base = len(text) + 2 if text else 0
    full = (text + "\n\n" + corpus_text) if text else corpus_text
    res = engine.analyze(full, excluded=excluded,
                         custom_terms=custom_terms, ctl=ctl)
    plan_from_entities(cache, res["entities"], base, spans)
    n_unr = allocate_unreadable(cache, res["mapping"])
    if n_unr:
        res["by_label"][UNREADABLE] = n_unr
    n_sig = allocate_signatures(cache, res["mapping"])
    if n_sig:
        res["by_label"][SIGNATURE] = n_sig
    return res


def corpus(cache):
    """(testo del corpus, spans) — le righe OCR di tutte le immagini unite da
    "\\n"; spans = [(start, end, img_idx, line_idx)] per rimappare gli offset
    delle entità sul (immagine, riga) di provenienza."""
    parts, spans, pos = [], [], 0
    for ii, img in enumerate(cache["images"]):
        for li, line in enumerate(img["lines"]):
            t = line["t"]
            spans.append((pos, pos + len(t), ii, li))
            parts.append(t)
            pos += len(t) + 1                     # il "\n" separatore
    return "\n".join(parts), spans


def plan_from_entities(cache, entities, base, spans):
    """Trasforma le entità di analyze() ricadute nel corpus (offset >= base)
    nel piano di redazione per immagine: {"line", "s", "e", "ph"} con s/e
    RELATIVI alla riga. Un'entità che scavalca il "\\n" (nome spezzato su due
    righe dal layout) genera una voce per ciascuna riga toccata."""
    for e in entities:
        if e["end"] <= base:
            continue
        rs, re_ = e["start"] - base, e["end"] - base
        for s0, s1, ii, li in spans:
            if s0 < re_ and s1 > rs:
                img = cache["images"][ii]
                img["plan"].append({"line": li,
                                    "s": max(rs, s0) - s0,
                                    "e": min(re_, s1) - s0,
                                    "ph": e["ph"]})


def allocate_unreadable(cache, mapping):
    """Politica conservativa: le righe OCR a bassa confidenza (< MIN_SCORE) su
    cui il motore non ha trovato nulla si coprono PER INTERO come
    [UNREADABLE_n] — se una riga non si riesce a leggere, non si può
    dichiararla priva di PII. Il valore in mappa è la lettura (storpiata)
    dell'OCR: così il box è de-anonimizzabile dalla preview come ogni altro
    placeholder. La numerazione prosegue quella già in mappa.
    Ritorna quante righe sono state coperte (per il by_label del chiamante)."""
    n = 0
    pat = re.compile(r"^\[" + UNREADABLE + r"_(\d+)\]$")
    for ph in mapping:
        m = pat.match(ph)
        if m:
            n = max(n, int(m.group(1)))
    added = 0
    for img in cache["images"]:
        planned = {p["line"] for p in img["plan"] if "line" in p}
        for li, line in enumerate(img["lines"]):
            if li in planned or line["s"] >= MIN_SCORE:
                continue
            if len(line["t"].strip()) < 2:
                continue
            regions = (img.get("regions") or []) + (img.get("covered_signatures") or [])
            if _in_region(line["b"], regions):
                continue            # già coperta dal box firma/timbro intero
            n += 1
            added += 1
            ph = f"[{UNREADABLE}_{n}]"
            mapping[ph] = line["t"]
            img["plan"].append({"line": li, "s": 0, "e": len(line["t"]),
                                "ph": ph})
    return added


def _in_region(b, regions):
    """True se il box riga sta (>= 60% della sua area) dentro una regione
    firma/timbro: il testo storpiato di un timbro non merita un secondo
    placeholder, lo copre già [SIGNATURE_n]."""
    area = max((b[2] - b[0]) * (b[3] - b[1]), 1)
    for reg in regions:
        r = reg["b"]
        ix = max(0, min(b[2], r[2]) - max(b[0], r[0]))
        iy = max(0, min(b[3], r[3]) - max(b[1], r[1]))
        if ix * iy / area >= 0.6:
            return True
    return False


def allocate_signatures(cache, mapping):
    """Ogni regione firma/timbro del detector diventa un [SIGNATURE_n] in
    mappa (valore fisso SIG_VALUE: non c'è testo da restituire) e una voce
    di piano {"region", "ph"}: il box copre la regione PER INTERO. La
    numerazione prosegue quella già in mappa. Ritorna quante regioni ha
    coperto (per il by_label del chiamante)."""
    n = 0
    pat = re.compile(r"^\[" + SIGNATURE + r"_(\d+)\]$")
    for ph in mapping:
        m = pat.match(ph)
        if m:
            n = max(n, int(m.group(1)))
    added = 0
    for img in cache["images"]:
        for ri, _reg in enumerate(img.get("regions") or ()):
            n += 1
            added += 1
            ph = f"[{SIGNATURE}_{n}]"
            mapping[ph] = SIG_VALUE
            img["plan"].append({"region": ri, "ph": ph})
    return added


# --------------------------------------------------------------------------- #
# Piano + mappa corrente -> box da disegnare
# --------------------------------------------------------------------------- #
def _active(mapping):
    """I placeholder da coprire ORA: le chiavi della mappa meno quelli
    esclusi. `ph in mapping` NON basta: una ReplacementMapping è un dict il
    cui dizionario canonico conserva anche le entità deanonimizzate una a
    una dall'anteprima (e le categorie lasciate in chiaro) — servono a
    decodificare ciò che è già partito — mentre le SOSTITUZIONI (`items()`)
    non le hanno più. Un box pianificato al rilevamento e confrontato con
    `in mapping` sopravviveva quindi al deanonimizza, quando quello trovato
    dalla ri-ricerca (`items()`) spariva. Non si usa `items()` al posto delle
    chiavi perché le superfici troppo corte per la ricerca testuale
    (`skipped`) restano da coprire dove il piano le ha viste."""
    try:
        phs = set(mapping)                 # dict / ReplacementMapping: chiavi
    except TypeError:
        # chat_staging._Pairs (overlay dei documenti Office): solo coppie,
        # niente iterazione sulle chiavi
        phs = {ph for ph, _v in mapping.items()}
    is_excluded = getattr(mapping, "_is_excluded", None)
    if is_excluded is None:
        return phs
    return {ph for ph in phs if not is_excluded(ph)}


def boxes_for(cache, mapping):
    """{key immagine: [(bbox_px, ph)]} dei box da disegnare ORA:
      - le voci del piano il cui placeholder è ancora in mappa (una
        de-anonimizzazione lo toglie e il box sparisce alla ri-redazione);
      - TUTTE le superfici in mappa (canonici e alias, inclusi i termini
        custom aggiunti dopo il detect): ri-cercate nelle righe OCR con la
        stessa _value_pattern della redazione testuale, come il percorso
        testo copre ogni occorrenza di ogni superficie nota. Anche i
        placeholder GIÀ pianificati partecipano: il piano copre solo le
        occorrenze che il rilevamento ha visto, un alias può comparire in
        righe che il modello non ha taggato (`taken` evita i doppi box).
        Niente OCR: solo la cache."""
    from .pdf_export import _too_noisy, _value_pattern

    extra = []
    for ph, val in mapping.items():
        if not isinstance(val, str) or _too_noisy(val, ph):
            continue
        pat = _value_pattern(val, ph)
        if pat:
            extra.append((ph, pat))

    active = _active(mapping) - local_excluded(cache)
    out = {}
    for img in cache["images"]:
        boxes = []
        taken = []                                 # intervalli già coperti per riga
        for p in sorted(img["plan"],
                        key=lambda p: (p.get("line", -1), p.get("s", 0))):
            if p["ph"] not in active:
                continue
            if "region" in p:                      # firma/timbro: box intero
                boxes.append((list(img["regions"][p["region"]]["b"]), p["ph"]))
                continue
            line = img["lines"][p["line"]]
            boxes.append((_sub_box(line, p["s"], p["e"]), p["ph"]))
            taken.append((p["line"], p["s"], p["e"]))
        for li, s, e, ph in _search_lines(img, extra, taken):
            boxes.append((_sub_box(img["lines"][li], s, e), ph))
        if boxes:
            out[img["key"]] = boxes
    return out


def _search_lines(img, extra, taken):
    """Ri-ricerca delle superfici in mappa nelle righe OCR di UN'immagine:
    [(line, s, e, ph)] con s/e relativi alla riga, saltando (e aggiornando)
    gli intervalli `taken` già coperti dal piano.

    Si cerca sul testo dell'immagine UNITO da "\\n" — la stessa stringa su cui
    lavorano corpus()/analyze_with_corpus e in_cache() — e non riga per riga:
    l'OCR restituisce spesso un box PER PAROLA (il logo "Fondazione |
    Policlinico | ..." sono righe distinte della cache), e _value_pattern
    accetta whitespace/newline tra i token. Cercando riga per riga un valore
    di più parole non poteva combaciare con nessuna riga a parola singola:
    in_cache lo dava per "già presente" (la selezione manuale rispondeva
    "già anonimizzato") e nei pixel non compariva alcun box. Un match che
    scavalca il "\\n" genera un segmento per ogni riga toccata, come
    plan_from_entities."""
    lines = img["lines"]
    if not lines or not extra:
        return []
    spans, pos = [], 0
    for li, line in enumerate(lines):
        spans.append((pos, pos + len(line["t"]), li))
        pos += len(line["t"]) + 1
    text = "\n".join(line["t"] for line in lines)
    out = []
    for ph, pat in extra:
        for m in pat.finditer(text):
            segs = [(li, max(m.start(), s0) - s0, min(m.end(), s1) - s0)
                    for s0, s1, li in spans
                    if s0 < m.end() and s1 > m.start()]
            # i segmenti già coperti dal piano si saltano; gli altri si
            # coprono comunque (il piano può aver visto solo una parte)
            segs = [(li, s, e) for li, s, e in segs
                    if e > s and not any(li == tl and s < te and e > ts
                                         for tl, ts, te in taken)]
            taken.extend(segs)
            out.extend((li, s, e, ph) for li, s, e in segs)
    return out


def _sub_box(line, s, e):
    """Box px della porzione [s, e) della riga: riparto proporzionale sul
    conteggio caratteri (l'OCR non dà i box per carattere) lungo l'asse di
    LETTURA della riga — quello lungo (_long_axis). Su un raster salvato
    "di lato" e rimesso dritto dal PDF le righe corrono lungo Y: ripartire
    sempre la larghezza copriva una strisciolina alta quanto la riga e larga
    pochi pixel, e il valore restava in chiaro."""
    a0, a1, c0, c1, horiz = _long_axis(line["b"], line.get("t", ""))
    span = a1 - a0
    n = max(len(line["t"]), 1)
    p0, p1 = a0 + span * (s / n), a0 + span * (e / n)
    if horiz:
        return [p0, c0, p1, c1]
    return [c0, p0, c1, p1]


def _px_to_page(transform, w, h, box):
    """Rettangolo pagina (punti) di un box in PIXEL dell'immagine piazzata
    con `transform` (get_image_info: quadrato unitario y-down -> pagina).
    Passa per i quattro angoli: con una rotazione di 90° nella matrice il
    box resta un rettangolo ma i lati si scambiano, e la semplice scala
    larghezza/altezza lo metterebbe altrove."""
    import fitz
    M = fitz.Matrix(*transform)
    bx0, by0, bx1, by1 = box
    pts = [fitz.Point(x / w, y / h) * M
           for x, y in ((bx0, by0), (bx1, by0), (bx0, by1), (bx1, by1))]
    return fitz.Rect(min(q.x for q in pts), min(q.y for q in pts),
                     max(q.x for q in pts), max(q.y for q in pts))


def redacted_lines(cache, mapping):
    """Le righe OCR della cache col testo COPERTO come nell'immagine redatta:
    stesse regole di boxes_for (piano ∩ mappa + ri-ricerca di TUTTE le
    superfici in mappa), ma sul testo invece che sui pixel. È la via veloce
    del tool read_document_images: niente OCR, niente inferenza.

    Le voci LOCALI [UNREADABLE_n]/[SIGNATURE_n] restano sempre coperte anche
    se il registro della conversazione non le ospita (vedi unreadable_mapping):
    una lettura storpiata non deve uscire in chiaro dalla via veloce.

    Ritorna [{"key", "page"?, "lines": [str]}], un elemento per immagine; i
    placeholder delle regioni firma/timbro chiudono l'elenco righe della loro
    immagine (non hanno una riga di testo propria)."""
    from .pdf_export import _too_noisy, _value_pattern

    extra = []
    for ph, val in mapping.items():
        if not isinstance(val, str) or _too_noisy(val, ph):
            continue
        pat = _value_pattern(val, ph)
        if pat:
            extra.append((ph, pat))

    local = ("[" + UNREADABLE + "_", "[" + SIGNATURE + "_")
    active = _active(mapping) - local_excluded(cache)
    excluded = local_excluded(cache)
    out = []
    for img in cache["images"]:
        repl = {}                                  # line idx -> [(s, e, ph)]
        tail = []                                  # regioni firma/timbro
        taken = []
        for p in sorted(img["plan"],
                        key=lambda p: (p.get("line", -1), p.get("s", 0))):
            if p["ph"] not in active and not (
                    p["ph"].startswith(local) and p["ph"] not in excluded):
                continue
            if "region" in p:
                tail.append(p["ph"])
                continue
            repl.setdefault(p["line"], []).append((p["s"], p["e"], p["ph"]))
            taken.append((p["line"], p["s"], p["e"]))
        for li, s, e, ph in _search_lines(img, extra, taken):
            repl.setdefault(li, []).append((s, e, ph))
        lines = []
        for li, line in enumerate(img["lines"]):
            t = line["t"]
            for s, e, ph in sorted(repl.get(li, ()), key=lambda r: -r[0]):
                t = t[:s] + ph + t[e:]
            lines.append(t)
        lines.extend(tail)
        entry = {"key": img["key"], "lines": lines}
        if "page" in img:
            entry["page"] = img["page"]
        out.append(entry)
    return out


# --------------------------------------------------------------------------- #
# Disegno nei pixel + reinserimento
# --------------------------------------------------------------------------- #
def _font(size):
    from PIL import ImageFont
    for name in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def redact_image_bytes(data, ext, boxes, fill=YELLOW, text=BLACK):
    """Disegna i box (+ etichetta placeholder se ci sta) nei pixel e riscrive
    l'immagine nel SUO formato (jpeg -> jpeg q90 per contenere il peso; gli
    altri nel formato d'origine). fill/text: RGB 0-255 — giallo evidenziatore
    di default (stessa convenzione dei PDF), nero per le aree sigillate.
    Ritorna i bytes nuovi."""
    from PIL import Image, ImageDraw

    with Image.open(io.BytesIO(data)) as im:
        has_alpha = im.mode in ("RGBA", "LA", "PA") or \
            (im.mode == "P" and "transparency" in im.info)
        pil = im.convert("RGBA" if has_alpha else "RGB")

    draw = ImageDraw.Draw(pil)
    for (bx0, by0, bx1, by1), ph in boxes:
        draw.rectangle([bx0 - 2, by0 - 2, bx1 + 2, by1 + 2], fill=tuple(fill))
        size = max(10, min(28, int((by1 - by0) * 0.72)))
        font = _font(size)
        tb = draw.textbbox((0, 0), ph, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        if tw <= (bx1 - bx0):
            draw.text((bx0 + ((bx1 - bx0) - tw) / 2,
                       by0 + ((by1 - by0) - th) / 2 - tb[1]),
                      ph, fill=tuple(text), font=font)

    buf = io.BytesIO()
    fmt = (ext or "png").lower().lstrip(".")
    if fmt in ("jpg", "jpeg"):
        pil.convert("RGB").save(buf, format="JPEG", quality=90)
    elif fmt in ("tif", "tiff"):
        pil.save(buf, format="TIFF")
    elif fmt == "gif":
        pil.convert("P", palette=1).save(buf, format="GIF")
    else:
        pil.save(buf, format="PNG")
    return buf.getvalue()


def seal_image(data, ext, sealed):
    """Applica a un documento IMMAGINE (già redatto) le aree SIGILLATE
    dall'utente: rettangolo nero pieno nei pixel con scritta bianca "SEALED"
    (stesso concetto di pdf.seal_pdf: il contenuto sotto viene distrutto,
    non solo coperto). I rect arrivano in punti della preview, che per le
    immagini coincidono coi pixel (image_to_pdf: 1 px = 1 pt).

    Ritorna (bytes, boxes {0: [...]}) per l'overlay interattivo."""
    from PIL import Image

    from .pdf import SEAL_LABEL   # lazy: pdf importa questo modulo al top
    if not sealed:
        return data, {}
    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
    items = []
    for s in sealed:
        x0, y0, x1, y1 = s["rect"]
        x0, x1 = max(0.0, min(x0, w)), max(0.0, min(x1, w))
        y0, y1 = max(0.0, min(y0, h)), max(0.0, min(y1, h))
        if x1 - x0 < 1 or y1 - y0 < 1:
            continue
        items.append(((x0, y0, x1, y1), s["n"]))
    if not items:
        return data, {}
    out = redact_image_bytes(data, ext, [(b, SEAL_LABEL) for b, _n in items],
                             fill=BLACK, text=WHITE)
    boxes = {0: [{"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3],
                  "ph": f"[{SEAL_LABEL}_{n}]", "label": SEAL_LABEL,
                  "sealed": n} for b, n in items]}
    return out, boxes


def merge_image_report(report, img_by_ph):
    """Fonde le occorrenze redatte NELLE IMMAGINI nel report della redazione
    testuale: contatori in by_placeholder/occurrences, e i placeholder coperti
    escono da not_found/skipped (il valore non era nel testo di pagina, ma nei
    pixel: c'è eccome). `image_occurrences` resta anche a parte, per l'UI."""
    covered = {ph for ph, n in img_by_ph.items() if n}
    for ph, n in img_by_ph.items():
        report["by_placeholder"][ph] = report["by_placeholder"].get(ph, 0) + n
    report["occurrences"] = report.get("occurrences", 0) + sum(img_by_ph.values())
    report["not_found"] = [ph for ph in report.get("not_found", ())
                           if ph not in covered]
    report["skipped"] = [ph for ph in report.get("skipped", ())
                         if ph not in covered]
    report["image_occurrences"] = sum(img_by_ph.values())


# --------------------------------------------------------------------------- #
# Applicazione ai contenitori
# --------------------------------------------------------------------------- #
@mupdf_serialized
def redact_pdf_images(pdf_bytes, cache, mapping, fill=YELLOW, text=BLACK):
    """Sostituisce nel PDF le immagini con la versione redatta e mappa i box
    da px a PUNTI pagina per l'overlay interattivo della preview.

    Le voci `page:n` (pagine vettoriali, vedi _vector_page) si redigono
    RASTERIZZANDO la pagina: si ridisegna il render del detect coi box nei
    pixel, si svuota il content stream (i tracciati vettoriali del testo
    ESCONO dal file, non vengono coperti) insieme alle Resources (gli oggetti
    immagine orfani cadono col garbage collect del salvataggio) e si
    reinserisce il render redatto a pagina intera.

    Ritorna (pdf_bytes, overlay {pagina: [{x0,y0,x1,y1,ph}]}, by_ph).
    Va chiamata PRIMA della redazione testuale: apply_redactions (images=2)
    cancella i pixel sotto i rect del layer testo, e deve farlo sulla
    versione già redatta delle immagini (l'ordine inverso ripristinerebbe
    pixel già cancellati)."""
    import fitz

    boxes_by_key = boxes_for(cache, mapping)
    by_ph = {}
    if not boxes_by_key:
        return pdf_bytes, {}, by_ph

    meta = {img["key"]: img for img in cache["images"]}
    overlay = {}
    page_signatures = {}
    for key in list(boxes_by_key):
        if meta[key].get("pdf_signature"):
            page_signatures.setdefault(meta[key]["page"], []).extend(
                boxes_by_key.pop(key))
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        # Plan every source-image mask before replacing any xref: regions can
        # share image resources with each other and with standalone images.
        source_boxes = {}
        for key, boxes in boxes_by_key.items():
            img = meta[key]
            if img.get('pdf_region'):
                for xref, pieces in pdf_image_regions.image_boxes(img, boxes).items():
                    source_boxes.setdefault(xref, []).extend(pieces)
                for member in img['pdf_region']['members']:
                    meta.setdefault(str(member['xref']), member)
            elif not key.startswith(PAGE_KEY):
                source_boxes.setdefault(key, []).extend(boxes)
        boxes_by_key = {key: boxes for key, boxes in boxes_by_key.items()
                        if key.startswith(PAGE_KEY)} | source_boxes
        for key, boxes in boxes_by_key.items():
            img = meta[key]
            if key.startswith(PAGE_KEY):
                page = doc[img["page"]]
                if page.rotation:
                    page.remove_rotation()
                png = page.get_pixmap(dpi=PAGE_DPI).tobytes("png")
                new_data = redact_image_bytes(png, "png", boxes,
                                              fill=fill, text=text)
                rect = page.rect
                page.clean_contents()
                conts = page.get_contents()
                if conts:
                    doc.update_stream(conts[0], b"")
                doc.xref_set_key(page.xref, "Resources", "<<>>")
                page.insert_image(rect, stream=new_data)
                sx, sy = rect.width / img["w"], rect.height / img["h"]
                for (bx0, by0, bx1, by1), ph in boxes:
                    overlay.setdefault(img["page"], []).append({
                        "x0": rect.x0 + bx0 * sx, "y0": rect.y0 + by0 * sy,
                        "x1": rect.x0 + bx1 * sx, "y1": rect.y0 + by1 * sy,
                        "ph": ph, "ocr": True})
                    by_ph[ph] = by_ph.get(ph, 0) + 1
                continue
            xref = int(key)
            try:
                info = doc.extract_image(xref)
            except Exception:
                continue
            data, ext = _stencil_positive(doc, xref, info)
            new_data = redact_image_bytes(data, ext, boxes,
                                          fill=fill, text=text)
            replaced = False
            for pno in range(doc.page_count):
                page = doc[pno]
                try:
                    infos = [i for i in page.get_image_info(xrefs=True)
                             if i.get("xref") == xref]
                except Exception:
                    infos = []
                if not infos:
                    continue
                if not replaced:
                    page.replace_image(xref, stream=new_data)
                    replaced = True
                for info in infos:
                    for box, ph in boxes:
                        # px -> pagina con la matrice di piazzamento (la
                        # rotazione di una scansione "di lato" è lì dentro).
                        # "ocr": True marca il box come letto dalle immagini:
                        # l'overlay della preview lo dichiara nel tooltip
                        r = _px_to_page(info["transform"], img["w"], img["h"],
                                        box)
                        overlay.setdefault(pno, []).append({
                            "x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1,
                            "ph": ph, "ocr": True})
            if replaced:
                for _b, ph in boxes:
                    by_ph[ph] = by_ph.get(ph, 0) + 1
        if page_signatures:
            from . import pdf_signatures
            sig_overlay, sig_counts = pdf_signatures.redact(
                doc, page_signatures, fill, text)
            for pno, items in sig_overlay.items():
                overlay.setdefault(pno, []).extend(items)
            for ph, count in sig_counts.items():
                by_ph[ph] = by_ph.get(ph, 0) + count
        out = doc.tobytes(garbage=3, deflate=True)
    return out, overlay, by_ph


def ooxml_media_redactor(cache, mapping, by_ph, fill=YELLOW, text=BLACK):
    """Per i loop zip di docx/pptx/xlsx: ritorna una funzione
    (nome_part, bytes) -> bytes che redige le part media pianificate e conta
    in `by_ph`; None se non c'è nulla da fare (percorso testo puro)."""
    if not cache:
        return None
    boxes_by_key = boxes_for(cache, mapping)
    if not boxes_by_key:
        return None
    meta = {img["key"]: img for img in cache["images"]}

    def redact(name, data):
        boxes = boxes_by_key.get(name)
        if not boxes:
            return data
        out = redact_image_bytes(data, meta[name]["ext"], boxes,
                                 fill=fill, text=text)
        for _b, ph in boxes:
            by_ph[ph] = by_ph.get(ph, 0) + 1
        return out

    return redact


# --- Box delle immagini sull'anteprima di un OOXML --------------------------- #
# Nei PDF l'anteprima È il file, e redact_pdf_images sa già dire dove
# finiscono i box (xref -> get_image_rects). L'anteprima di un OOXML è invece
# una RICONVERSIONE LibreOffice: i placeholder dipinti nei pixel non entrano
# nel layer testo del PDF, quindi pdf._placeholder_boxes non li trova e senza
# questo passaggio l'overlay interattivo (tooltip col valore, "Deanonimizza")
# manca PROPRIO dove ha lavorato l'OCR. Qui si ritrova ogni part media dentro
# il PDF convertito e si proiettano i suoi box px in punti pagina.
#
# L'accoppiamento non può passare dai byte: LibreOffice riscrive le immagini
# (un PNG 2000x1125 esce come JPEG, le trasparenze vengono fuse), e nemmeno
# dalle sole dimensioni, che nei deck si ripetono (due screenshot 2000x1125
# nello stesso file sono la norma). Si confronta allora il CONTENUTO ridotto a
# una firma 16x16 in grigi: la ricompressione la muove di frazioni, un'altra
# immagine di parecchio. Se il verdetto non è netto non si disegna nulla:
# meglio nessun box che un box fuori posto — i pixel gialli restano comunque
# al loro posto, è solo l'overlay a mancare.
_MATCH_SIDE = 16       # lato della firma d'immagine
_MATCH_MAX = 20.0      # distanza media oltre cui non è la stessa immagine
_MATCH_MARGIN = 2.0    # distacco minimo dal secondo classificato


def _img_sig(data):
    """Firma 16x16 in grigi, indipendente da formato e scala, con l'alfa
    appiattito su bianco (come lo appiattisce chi converte). None se Pillow
    non apre l'immagine."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as src:
            has_alpha = src.mode in ("RGBA", "LA", "PA") or \
                (src.mode == "P" and "transparency" in src.info)
            if has_alpha:
                rgba = src.convert("RGBA")
                flat = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                gray = Image.alpha_composite(flat, rgba).convert("L")
            else:
                gray = src.convert("L")
            return list(gray.resize((_MATCH_SIDE, _MATCH_SIDE),
                                    Image.BILINEAR).getdata())
    except Exception:
        return None


def _sig_dist(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _best_match(sig, group, sig_of):
    """La chiave della part media che corrisponde a questa immagine del PDF,
    o None se il verdetto non è netto (nessun candidato abbastanza vicino,
    oppure due candidati troppo simili tra loro per distinguerli)."""
    ranked = []
    for key in group:
        s = sig_of(key)
        if s is not None:
            ranked.append((_sig_dist(sig, s), key))
    if not ranked:
        return None
    ranked.sort(key=lambda t: t[0])
    if ranked[0][0] > _MATCH_MAX:
        return None
    if len(ranked) > 1 and ranked[1][0] - ranked[0][0] < _MATCH_MARGIN:
        return None
    return ranked[0][1]


_CLIP_MIN = 1.0        # pt: sotto questa taglia il residuo visibile non è un box


def _clip(rect, x0, y0, x1, y1):
    """Il box ristretto alla pagina, o None se ne resta fuori."""
    x0, y0 = max(x0, rect.x0), max(y0, rect.y0)
    x1, y1 = min(x1, rect.x1), min(y1, rect.y1)
    if x1 - x0 < _CLIP_MIN or y1 - y0 < _CLIP_MIN:
        return None
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


@mupdf_serialized
def ooxml_media_overlay(preview_pdf, ooxml_bytes, cache, mapping):
    """Box delle immagini di un OOXML proiettati sul suo PDF di ANTEPRIMA:
    {pagina: [{x0,y0,x1,y1,ph,ocr:True}]}, in punti pagina come i box del
    testo. `ooxml_bytes` è il file DAVVERO convertito (per l'xlsx la copia
    troncata, non l'originale) e va d'accordo col lato: le media originali
    per l'anteprima di sinistra, quelle ridisegnate per quella di destra.

    Vuoto — senza mai sollevare — se non c'è niente da disegnare, se
    l'immagine non compare nell'anteprima (una media in un layout che
    LibreOffice non rende) o se l'accoppiamento è ambiguo."""
    import zipfile

    import fitz

    if not cache:
        return {}
    boxes_by_key = boxes_for(cache, mapping)
    if not boxes_by_key:
        return {}
    try:
        zf = zipfile.ZipFile(io.BytesIO(ooxml_bytes))
        names = set(zf.namelist())
    except zipfile.BadZipFile:
        return {}
    # i candidati sono TUTTE le immagini della cache, non solo quelle con box:
    # una media senza entità può somigliare a una che ne ha, e chi vince
    # senza box semplicemente non fa disegnare nulla (è il verdetto giusto)
    meta = {img["key"]: img for img in cache["images"]
            if img["key"] in names and img.get("w") and img.get("h")}
    if not meta:
        return {}

    sigs = {}

    def sig_of(key):
        if key not in sigs:
            sigs[key] = _img_sig(zf.read(key))
        return sigs[key]

    overlay = {}
    with fitz.open(stream=preview_pdf, filetype="pdf") as doc:
        pdf_sigs = {}                       # xref -> firma (una decodifica sola)
        for pno in range(doc.page_count):
            page = doc[pno]
            for im in page.get_images(full=True):
                xref, w, h = im[0], im[2], im[3]
                if xref not in pdf_sigs:
                    try:
                        pdf_sigs[xref] = _img_sig(doc.extract_image(xref)["image"])
                    except Exception:
                        pdf_sigs[xref] = None
                sig = pdf_sigs[xref]
                if sig is None:
                    continue
                # prima si confrontano le sole media con le stesse dimensioni
                # in pixel (LibreOffice di norma le conserva): gruppo piccolo,
                # meno decodifiche e meno ambiguità. Se nessuna combacia
                # (immagine ricampionata) si guarda tutto il resto.
                group = [k for k, m in meta.items() if (m["w"], m["h"]) == (w, h)]
                if not group:
                    group = list(meta)
                key = _best_match(sig, group, sig_of)
                boxes = boxes_by_key.get(key) if key else None
                if not boxes:
                    continue
                img = meta[key]
                for r in page.get_image_rects(xref):
                    sx, sy = r.width / img["w"], r.height / img["h"]
                    for (bx0, by0, bx1, by1), ph in boxes:
                        # un'immagine a cavallo di un salto pagina viene
                        # ridisegnata su entrambe, sbordando: il box si taglia
                        # sulla pagina e chi resta fuori si butta (il viewer
                        # lo disegnerebbe oltre il bordo dell'anteprima)
                        box = _clip(page.rect,
                                    r.x0 + bx0 * sx, r.y0 + by0 * sy,
                                    r.x0 + bx1 * sx, r.y0 + by1 * sy)
                        if box is None:
                            continue
                        box.update(ph=ph, ocr=True)
                        overlay.setdefault(pno, []).append(box)
    return overlay


def merge_ooxml_overlays(cache, mapping, sides):
    """Aggiunge ai box di anteprima di un OOXML quelli delle entità lette
    nelle IMMAGINI. `sides`: [(box {pagina: [...]}, pdf di anteprima, bytes
    OOXML convertiti)], di norma il lato originale e quello anonimizzato.
    Modifica i box sul posto; no-op senza cache OCR."""
    if not cache:
        return
    for pages, preview, src in sides:
        for pno, blist in ooxml_media_overlay(preview, src, cache,
                                              mapping).items():
            pages.setdefault(pno, []).extend(blist)


def unreadable_mapping(cache):
    """{ph: valore} delle voci LOCALI della cache: le righe [UNREADABLE_n]
    (valore = lettura OCR) e le regioni [SIGNATURE_n] (valore = SIG_VALUE).
    Nei DOCUMENTI queste voci stanno già nella mappa del documento; nella
    CHAT il registro della conversazione non le ospita (superfici storpiate
    o non testuali che avvelenerebbero i turni successivi), quindi chi
    redige le aggiunge alla mappa DEL MOMENTO con questa funzione."""
    out = {}
    if not cache:
        return out
    unr = "[" + UNREADABLE + "_"
    sig = "[" + SIGNATURE + "_"
    excluded = local_excluded(cache)
    for img in cache["images"]:
        for p in img["plan"]:
            if p["ph"] in excluded:
                continue                    # deanonimizzata dall'anteprima
            if p["ph"].startswith(unr):
                out[p["ph"]] = img["lines"][p["line"]]["t"]
            elif p["ph"].startswith(sig):
                out[p["ph"]] = SIG_VALUE
    return out


# --- Deanonimizzazione delle voci LOCALI -------------------------------------
# [UNREADABLE_n] e [SIGNATURE_n] non entrano nel registro della conversazione
# (vedi unreadable_mapping), quindi il deanonimizza non può marcarle
# `excluded` su una ConversationEntity come fa con le altre: rispondeva 404.
# L'esclusione vive dove vive la voce: nella cache OCR dell'allegato
# (`cache["excluded"]`), che ogni ri-redazione ricarica. unreadable_mapping,
# boxes_for e redacted_lines la rispettano; una rielaborazione con OCR
# ricostruisce la cache da zero e la azzera, come azzera il piano.

_LOCAL_PREFIXES = ("[" + UNREADABLE + "_", "[" + SIGNATURE + "_")


def is_local(ph):
    """È una voce locale della cache ([UNREADABLE_n] / [SIGNATURE_n])?"""
    return str(ph or "").startswith(_LOCAL_PREFIXES)


def local_label(label):
    """UNREADABLE/SIGNATURE se `label` è una categoria locale, None altrimenti."""
    lab = str(label or "").strip().upper()
    return lab if lab in (UNREADABLE, SIGNATURE) else None


def local_excluded(cache):
    return set((cache or {}).get("excluded") or ())


def exclude_local(cache, placeholder=None, label=None):
    """Deanonimizza una voce locale (placeholder) o tutte quelle di una
    categoria locale (label) presenti nel piano della cache. Ritorna i
    placeholder appena esclusi (vuoto se non c'era nulla da escludere)."""
    if not cache:
        return []
    prefix = None
    if label:
        lab = local_label(label)
        if lab is None:
            return []
        prefix = "[" + lab + "_"
    excluded = local_excluded(cache)
    hit = set()
    for img in cache["images"]:
        for p in img["plan"]:
            ph = p["ph"]
            if not is_local(ph) or ph in excluded:
                continue
            if (placeholder and ph == placeholder) or (
                    prefix and ph.startswith(prefix)):
                hit.add(ph)
    if hit:
        cache["excluded"] = sorted(excluded | hit)
    return sorted(hit)


def _line_key(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def include_local(cache, text):
    """Inverso di exclude_local per una riga [UNREADABLE_n]: se `text` è la
    lettura di una riga illeggibile ESCLUSA, la si ricopre e si ritorna il
    suo placeholder (None altrimenti). Serve all'anonimizza-in-più: senza,
    riselezionare la riga allocherebbe un [CUSTOM_n] con la lettura storpiata
    nel registro della conversazione — proprio ciò che le voci locali
    evitano."""
    excluded = local_excluded(cache)
    if not excluded:
        return None
    key = _line_key(text)
    if not key:
        return None
    unr = "[" + UNREADABLE + "_"
    for img in cache["images"]:
        for p in img["plan"]:
            ph = p["ph"]
            if ph in excluded and ph.startswith(unr) and "line" in p and \
                    _line_key(img["lines"][p["line"]]["t"]) == key:
                excluded.discard(ph)
                cache["excluded"] = sorted(excluded)
                return ph
    return None


def redact_single_image(data, ext, cache, mapping, fill=YELLOW, text=BLACK):
    """Redazione di un ALLEGATO IMMAGINE (chat): disegna i box e produce un
    report con le stesse chiavi dei redattori di formato. `boxes` è in
    coordinate px == punti (l'anteprima incapsula l'immagine in una pagina
    PDF grande esattamente w x h punti, vedi image_to_pdf)."""
    key = cache["images"][0]["key"] if cache and cache["images"] else None
    boxes = boxes_for(cache, mapping).get(key, []) if key else []
    out = redact_image_bytes(data, ext, boxes, fill=fill, text=text) \
        if boxes else data
    by_ph = {}
    for _b, ph in boxes:
        by_ph[ph] = by_ph.get(ph, 0) + 1
    report = {
        "occurrences": sum(by_ph.values()),
        "by_placeholder": by_ph,
        "not_found": [], "skipped": [],
        "residual": [],                # i pixel coperti non sono rileggibili
        "image_occurrences": sum(by_ph.values()),
        "boxes": {0: [{"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3],
                       "ph": ph, "ocr": True} for b, ph in boxes]},
    }
    return out, report


def anonymize_image(data, ext, engine, excluded=None, custom_terms=None,
                    ctl=None, ocr=False):
    """DOCUMENTO immagine -> stesse chiavi degli
    anonymize_* di formato: file redatto, analysis, report, box, dimensioni,
    preview PDF e cache OCR. Un'immagine non ha percorso testuale: senza OCR
    non c'è nulla su cui lavorare, quindi qui l'opzione è obbligatoria."""
    from .progress import NULL as _NULL_CTL
    ctl = ctl or _NULL_CTL
    if not ocr:
        raise OcrError("Le immagini si anonimizzano solo con l'OCR: ripeti "
                       "l'upload attivandolo nel popup.")
    if _too_small(data):
        raise OcrError("Immagine illeggibile o troppo piccola per contenere "
                       "testo: niente da anonimizzare.")
    ctl.phases(["image_ocr", "analysis", "redaction"])
    ctl.phase("image_ocr")
    cache = build_cache([{"key": "img", "ext": ext.lstrip("."),
                          "data": data}], ctl=ctl)
    if cache is None:
        raise OcrError("L'OCR non ha letto testo nell'immagine e non ha "
                       "trovato firme o timbri: niente da anonimizzare.")
    ctl.phase("analysis")
    res = analyze_with_corpus(engine, "", cache, excluded=excluded,
                              custom_terms=custom_terms, ctl=ctl)
    if not res["mapping"]:
        raise OcrError("Nessuna PII trovata nell'immagine: niente da "
                       "anonimizzare.")
    ctl.phase("redaction")
    out = rebuild_image(data, ext, res["mapping"], cache)
    out["analysis"] = res
    out["ocr_cache"] = cache
    return out


def rebuild_image(data, ext, mapping, ocr_cache, sealed=None):
    """Ri-redazione di un documento immagine con mappa GIÀ decisa (stesso
    contratto dei rebuild_* di formato). La preview di entrambi i lati è il
    PDF a una pagina 1 px = 1 pt (image_to_pdf): i box px del piano si
    sovrappongono alle pagine renderizzate senza conversioni.

    sealed: aree sigillate dall'utente (vedi seal_image), applicate DOPO la
    redazione della mappa e PRIMA della preview; i loro box vanno solo sul
    lato anonimizzato (a sinistra non c'è nulla da segnare)."""
    if not ocr_cache:
        raise OcrError("Cache OCR non disponibile per questa immagine: "
                       "ricarica il file.")
    out, report = redact_single_image(data, ext, ocr_cache, mapping)
    boxes = report.pop("boxes")
    # un ph in mappa senza box (valore mai visto nelle righe OCR) va dichiarato
    report["not_found"] = [ph for ph in mapping
                           if not report["by_placeholder"].get(ph)]
    anonymized_boxes = {k: [dict(b) for b in v] for k, v in boxes.items()}
    if sealed:
        out, seal_boxes = seal_image(out, ext, sealed)
        for k, blist in seal_boxes.items():
            anonymized_boxes.setdefault(k, []).extend(blist)
    img = ocr_cache["images"][0]
    page = {"width": img["w"], "height": img["h"]}
    return {"file": out, "report": report,
            "original_boxes": boxes,
            "anonymized_boxes": anonymized_boxes,
            "page_sizes": {"original": [dict(page)], "anonymized": [dict(page)]},
            "preview_pdf_original": image_to_pdf(data),
            "preview_pdf_anonymized": image_to_pdf(out)}


@mupdf_serialized
def image_to_pdf(data):
    """Incapsula un'immagine in un PDF a UNA pagina grande w x h PUNTI
    (1 px = 1 pt): è l'anteprima degli allegati immagine in chat, e rende i
    box px della cache direttamente sovrapponibili senza conversioni."""
    import fitz
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
    doc = fitz.open()
    page = doc.new_page(width=w, height=h)
    page.insert_image(page.rect, stream=data)
    out = doc.tobytes()
    doc.close()
    return out


# --------------------------------------------------------------------------- #
# Fallback OCR della selezione manuale nella preview
# --------------------------------------------------------------------------- #
CLIP_DPI = 300      # render del ritaglio: più fitto del detect (150) — le
                    # selezioni sono piccole, il costo resta trascurabile
_CLIP_MIN = 4.0     # punti: sotto questa taglia non c'è nulla da leggere
# Il rilevatore (DBNet) non chiude una regione che tocca il bordo del
# fotogramma: un ritaglio a filo dei glifi — cioè esattamente ciò che si
# ottiene selezionando UNA parola — non produce nessun box, a qualunque dpi.
# Servono due margini, entrambi proporzionati al testo:
#   _SEL_CTX   quanto si allarga il ritaglio SULLA PAGINA (contesto vero)
#   _SEL_FRAME la cornice neutra aggiunta dopo, per le selezioni al bordo
#              pagina, dove allargare non basta più
# Il margine è necessario ma porta dentro i vicini, e su una scansione fitta
# si vedeva: il ritaglio contiene mezza riga, il rilevatore la legge tutta e
# tornavano parole accanto e monconi tagliati dal bordo. La precisione quindi
# NON si recupera stringendo il ritaglio (stringerlo taglia i glifi: 'Camera'
# letto 'Camerz'), ma scartando le PAROLE fuori selezione dopo la lettura —
# posizione stimata per proporzione dentro il box, come fa _sub_box.
_SEL_CTX = 24.0     # punti (o l'altezza della selezione, se maggiore)
_SEL_FRAME = 0.5    # cornice neutra, frazione del lato MINORE del ritaglio
_SEL_FRAME_MAX = 400   # px: oltre non serve e il render costa
_SEL_WORD = 0.5     # quota della parola che deve cadere nella selezione
_SEL_ROW = 0.3      # sovrapposizione verticale minima: sotto è un'altra riga


@mupdf_serialized
def _clip_png(pdf_bytes, n, rect):
    """(PNG del ritaglio, rettangolo davvero reso) per la selezione `rect`
    (punti PDF) della pagina n, o None se pagina o rettangolo non sono
    utilizzabili. Il ritaglio è ALLARGATO di _SEL_CTX per lato — vedi la nota
    sui margini — quindi il rettangolo reso va restituito: è il sistema di
    riferimento per riportare i box letti in punti pagina. Solo il render sta
    sotto il lock MuPDF: l'inferenza OCR resta fuori (stessa regola del
    detect)."""
    import fitz
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        if not (0 <= n < doc.page_count):
            return None
        page = doc[n]
        sel = fitz.Rect(rect)
        # la taglia minima si guarda sulla SELEZIONE, non sul ritaglio: il
        # ritaglio è allargato per costruzione e non sarebbe mai piccolo
        if sel.is_empty or sel.width < _CLIP_MIN or sel.height < _CLIP_MIN:
            return None
        grow = max(_SEL_CTX, sel.height, sel.width * 0.1)
        clip = (fitz.Rect(sel) + (-grow, -grow, grow, grow)) & page.rect
        if clip.is_empty:
            return None
        png = page.get_pixmap(clip=clip, dpi=CLIP_DPI).tobytes("png")
    return png, (clip.x0, clip.y0, clip.x1, clip.y1)


_VERT_MIN_CHARS = 3    # sotto, un box più alto che largo è una parola corta
                       # orizzontale ("Il", "1"), non una riga verticale


def _long_axis(box, text=""):
    """(a0, a1, c0, c1, orizzontale): estremi della riga lungo l'asse di
    LETTURA (quello lungo) e lungo l'asse trasversale. Il bbox della cache
    perde l'orientamento del quad RapidOCR, ma una riga di testo è più lunga
    che alta: su un raster ruotato di 90° (scansione salvata "di lato" e
    rimessa dritta dal PDF) le righe corrono lungo Y, e ripartire i
    caratteri lungo X coprirebbe una strisciolina sbagliata. Una riga di uno
    o due caratteri può avere un box più alto che largo pur essendo
    orizzontale: lì si resta su X, com'era sempre stato."""
    x0, y0, x1, y1 = box
    if (x1 - x0) >= (y1 - y0) or len((text or "").strip()) < _VERT_MIN_CHARS:
        return x0, x1, y0, y1, True
    return y0, y1, x0, x1, False


def _pick_words(lines, sel):
    """Parole delle righe OCR che cadono nel rettangolo `sel` (pixel
    immagine), in ordine di lettura. Stesse soglie del fallback al ritaglio
    (_SEL_ROW sull'asse trasversale, _SEL_WORD sulla parola) e stessa stima
    per proporzione dei caratteri di _sub_box: ciò che si restituisce qui è
    ESATTAMENTE ciò che boxes_for coprirà poi nei pixel."""
    sx0, sy0, sx1, sy1 = sel
    picked = []
    for line in lines:
        text = (line.get("t") or "").strip()
        if not text:
            continue
        a0, a1, c0, c1, horiz = _long_axis(line["b"], line.get("t", ""))
        sa0, sa1, sc0, sc1 = (sx0, sx1, sy0, sy1) if horiz else (sy0, sy1, sx0, sx1)
        span, cross = a1 - a0, c1 - c0
        if span <= 0 or cross <= 0:
            continue
        # riga accanto, non questa. I box OCR di righe adiacenti si
        # sovrappongono spesso di quasi metà (interlinea stretta), quindi la
        # soglia lasca del ritaglio (_SEL_ROW) qui non basta: la selezione
        # deve contenere il CENTRO trasversale della riga, o coprirne almeno
        # la metà (banda sottile passata per il centro, o riga tagliata
        # dal bordo della selezione).
        mid = (c0 + c1) / 2.0
        if not (sc0 <= mid <= sc1
                or min(c1, sc1) - max(c0, sc0) >= 0.5 * cross):
            continue
        n = len(text)
        for m in re.finditer(r"\S+", text):
            w0 = a0 + span * m.start() / n
            w1 = a0 + span * m.end() / n
            if max(0.0, min(w1, sa1) - max(w0, sa0)) >= _SEL_WORD * (w1 - w0):
                picked.append((mid, cross, w0, m.group()))
    return _reading_order(picked)


def _reading_order(words):
    """Testo di [(centro trasversale, spessore, inizio lungo la riga, parola)]
    in ordine di lettura: per RIGA e, dentro la riga, lungo l'asse di
    lettura. Le parole di una stessa riga arrivano dall'OCR in box distinti
    con y0 che differiscono di qualche pixel (le maiuscole e le lettere con
    la gamba non sono alte uguali): ordinare sulla coordinata esatta le
    mescolava. Due parole stanno sulla stessa riga se i loro centri
    trasversali distano meno di metà dello spessore della più sottile."""
    rows = []                                  # [[mid, cross, [(a, w)]]]
    for mid, cross, a, w in sorted(words, key=lambda t: t[0]):
        if rows and abs(mid - rows[-1][0]) < 0.5 * min(cross, rows[-1][1]):
            rows[-1][2].append((a, w))
        else:
            rows.append([mid, cross, [(a, w)]])
    out = [w for _m, _c, ws in rows for _a, w in sorted(ws)]
    return re.sub(r"\s+", " ", " ".join(out)).strip()


@mupdf_serialized
def _sel_in_images(pdf_bytes, n, rect, cache):
    """[(immagine della cache, selezione in PIXEL di quell'immagine)] per le
    immagini della cache piazzate sulla pagina n e toccate dalla selezione
    (punti pagina). La geometria è quella con cui il PDF piazza l'immagine:
      - pagina vettoriale (PAGE_KEY): il render a PAGE_DPI della pagina
        senza rotazione, quindi scala dpi/72 dopo aver tolto la rotazione;
      - immagine per xref: la matrice `transform` di get_image_info
        (quadrato unitario -> pagina), invertita — la rotazione di 90° di una
        scansione salvata "di lato" è dentro la matrice, nessun caso a parte;
      - allegato IMMAGINE (chiave "img"): il PDF di anteprima è
        image_to_pdf, un'unica immagine a 1 px = 1 pt — stessa via dell'xref
        sull'unica immagine della pagina."""
    import fitz
    out = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        if not (0 <= n < doc.page_count):
            return out
        page = doc[n]
        sel = fitz.Rect(rect)
        if sel.is_empty:
            return out
        corners = [fitz.Point(sel.x0, sel.y0), fitz.Point(sel.x1, sel.y0),
                   fitz.Point(sel.x0, sel.y1), fitz.Point(sel.x1, sel.y1)]
        infos = None
        for img in cache.get("images") or []:
            if img.get("pdf_signature"):
                continue  # No OCR text: do not suppress selection's crop fallback.
            if img.get("page", 0) != n:
                continue
            w, h = img.get("w") or 0, img.get("h") or 0
            if w <= 0 or h <= 0:
                continue
            key = str(img.get("key", ""))
            placements = []                  # liste di 4 angoli in px
            if img.get('pdf_region'):
                r = fitz.Rect(img['pdf_region']['rect'])
                placements.append([((p.x - r.x0) * w / r.width,
                                    (p.y - r.y0) * h / r.height) for p in corners])
            elif key.startswith(PAGE_KEY):
                inv = ~page.rotation_matrix
                s = PAGE_DPI / 72.0
                placements.append([((p * inv).x * s, (p * inv).y * s)
                                   for p in corners])
            else:
                if infos is None:
                    try:
                        infos = page.get_image_info(xrefs=True)
                    except Exception:
                        infos = []
                placed = [i for i in infos if str(i.get("xref")) == key]
                if not placed:
                    # anteprima ANONIMIZZATA (o allegato immagine, chiave
                    # "img"): replace_image + garbage collect del salvataggio
                    # rinumerano gli xref, ma l'immagine sostituita ha le
                    # stesse dimensioni in pixel. Si abbina per dimensioni se
                    # l'abbinamento è univoco sulla pagina; con più immagini
                    # uguali si segue l'ordine di disegno, che è l'ordine
                    # con cui pdf_images le ha enumerate.
                    same = [i for i in infos
                            if i.get("width") == w and i.get("height") == h]
                    if len(same) == 1 or key == "img":
                        placed = same[:1]
                    elif same:
                        twins = [c for c in cache["images"]
                                 if c.get("page", 0) == n and c.get("w") == w
                                 and c.get("h") == h]
                        k = next((j for j, c in enumerate(twins)
                                  if c is img), None)
                        if k is not None and k < len(same):
                            placed = [same[k]]
                for i in placed:
                    try:
                        inv = ~fitz.Matrix(*i["transform"])
                    except Exception:
                        continue
                    # quadrato unitario di PyMuPDF: y VERSO IL BASSO, come la
                    # pagina — (0,0) è l'angolo in alto a sinistra
                    # dell'immagine, quindi riga 0 del raster = v 0
                    pts = []
                    for p in corners:
                        u = p * inv
                        pts.append((u.x * w, u.y * h))
                    placements.append(pts)
            for pts in placements:
                xs, ys = [q[0] for q in pts], [q[1] for q in pts]
                px = (max(min(xs), 0.0), max(min(ys), 0.0),
                      min(max(xs), float(w)), min(max(ys), float(h)))
                if px[2] > px[0] and px[3] > px[1]:
                    out.append((img, px))
    return out


def text_in_rect_cache(pdf_bytes, n, rect, cache):
    """Testo sotto la selezione letto dalla CACHE OCR dell'allegato, non da
    un nuovo OCR del ritaglio. È la via principale della selezione manuale
    su immagini e pagine vettoriali, per un motivo di coerenza: la cache è
    l'unica lettura che il controllo "il valore esiste nel turno" e la
    redazione nei pixel conoscono. Lo stesso motore, fatto girare sul
    ritaglio ingrandito, tokenizza gli spazi in modo diverso ("n. 0164..."
    in cache, "n.0164..." sul ritaglio) e la stringa mostrata all'utente
    veniva poi respinta come "non presente" — dallo stesso OCR.

    Ritorna None se la cache non ha nessuna immagine sotto la selezione (il
    chiamante passa al fallback sul ritaglio), "" se ce l'ha ma lì non c'è
    nessuna parola, altrimenti le parole in ordine di lettura."""
    if not cache:
        return None
    hits = _sel_in_images(pdf_bytes, n, rect, cache)
    if not hits:
        return None
    parts = [_pick_words(img.get("lines") or [], px) for img, px in hits]
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()


def in_cache(text, cache):
    """Il testo (letto dal ritaglio) compare nel corpus della cache OCR? È la
    condizione perché un'anonimizzazione successiva possa coprirlo nei
    pixel (boxes_for cerca nelle righe della cache con la stessa regex)."""
    if not text or not cache:
        return False
    from .pdf_export import _value_pattern
    pat = _value_pattern(text)
    return bool(pat and pat.search(corpus(cache)[0]))


def text_in_rect_ocr(pdf_bytes, n, rect):
    """OCR del ritaglio di una pagina: è il FALLBACK della selezione manuale
    nella preview quando pdf.text_in_rect non trova nulla (l'area cade dentro
    un'immagine o una pagina vettoriale). Stesso motore del detect; PAROLE in
    ordine di lettura, spazi normalizzati. Si restituisce solo ciò che sta
    nella selezione: il ritaglio è più largo per forza (vedi la nota sui
    margini) e le parole che sono entrate solo col margine si scartano qui.
    Ritorna "" se non c'è nulla da leggere; OcrError se lo stack OCR manca
    (il chiamante filtra con available())."""
    got = _clip_png(pdf_bytes, n, rect)
    if got is None:
        return ""
    png, (cx0, cy0, cx1, cy1) = got
    from PIL import Image

    ocr = _get_ocr()
    with Image.open(io.BytesIO(png)) as im:
        pil = im.convert("RGB")
    if not pil.width or not pil.height:
        return ""
    # cornice del colore mediano del bordo (_framed), come il detect
    pad = min(int(min(pil.width, pil.height) * _SEL_FRAME), _SEL_FRAME_MAX)
    with _ocr_lock:
        res = ocr(_framed(pil, pad))

    sx, sy = (cx1 - cx0) / pil.width, (cy1 - cy0) / pil.height
    x0, y0, x1, y1 = rect
    picked = []
    boxes = res.boxes if res.boxes is not None else []
    for box, text in zip(boxes, res.txts or []):
        text = (text or "").strip()
        if not text:
            continue
        xs = [float(p[0]) - pad for p in box]
        ys = [float(p[1]) - pad for p in box]
        # px del ritaglio -> punti pagina, per confrontarli con la selezione
        bx0, bx1 = cx0 + min(xs) * sx, cx0 + max(xs) * sx
        by0, by1 = cy0 + min(ys) * sy, cy0 + max(ys) * sy
        span = bx1 - bx0
        if span <= 0 or by1 <= by0:
            continue
        if min(by1, y1) - max(by0, y0) <= _SEL_ROW * (by1 - by0):
            continue                          # riga sopra o sotto la selezione
        for m in re.finditer(r"\S+", text):
            # posizione della parola dentro il box, stimata per proporzione
            # dei caratteri (stessa approssimazione di _sub_box)
            wx0 = bx0 + span * m.start() / len(text)
            wx1 = bx0 + span * m.end() / len(text)
            if (max(0.0, min(wx1, x1) - max(wx0, x0))
                    >= _SEL_WORD * (wx1 - wx0)):
                picked.append(((by0 + by1) / 2.0, by1 - by0, wx0, m.group()))
    return _reading_order(picked)

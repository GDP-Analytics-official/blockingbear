"""Scheda automatica di un allegato della chat.

PERCHÉ: senza questa scheda il modello riceve solo il nome del file e la
prima cosa che fa è un `os.listdir` + `pd.read_excel(...).head()` per capire
com'è fatto — un giro completo di API + sandbox buttato in ogni conversazione
con allegati. Con la scheda in contesto parte direttamente dall'elaborazione:
è il motivo per cui i chat assistant "sembrano sapere già" com'è il file.

NIENTE LLM qui dentro: si legge il file con la stessa cassetta degli attrezzi
del motore di anonimizzazione (zipfile + ElementTree per OOXML, PyMuPDF per i
PDF, stdlib per il resto). Zero dipendenze nuove, costo zero, nessun byte che
esce dal server.

La scheda si calcola UNA volta, all'upload, e si salva in
`attachments.briefing_json`: rileggerla a ogni messaggio significherebbe
ri-parsare un xlsx da 20 MB a ogni turno. `as_prompt()` la rende testo per il
contesto, `label` è la riga che la UI mostra sul chip.

Principio: mai sollevare. Un file corrotto, cifrato o in un formato che non
conosciamo produce una scheda povera (`kind: "binary"`), non un upload
fallito — il modello ha comunque la sandbox per aprirlo a modo suo.
"""

import base64
import csv
import io
import json
import mimetypes
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from ..engine.mupdf_lock import mupdf_serialized
from ..engine.xlsx import (_cell_text, _col_idx, _sheet_parts, _si_text,
                           X as _X)

# Tetti: la scheda è un aiuto, non un dump. Superarli non è un errore, si
# tronca (il modello ha la sandbox per il dettaglio).
MAX_CARD_CHARS = 1600           # per singolo file, nel prompt
_MAX_SHEETS = 8                 # fogli descritti per workbook
_SAMPLE_ROWS = 4                # righe di esempio mostrate
_SCAN_ROWS = 12                 # righe lette dal foglio (header + campione)
_MAX_SST = 5000                 # stringhe condivise lette (vedi _shared_head)
_MAX_COLS = 25                  # colonne elencate per foglio/tabella
_CELL_CHARS = 40                # troncamento di una cella nel campione
_TEXT_CHARS = 700               # anteprima testuale (pdf, docx, txt)


# --- utilità di formato ---------------------------------------------------

def fmt_size(n):
    if n is None:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _num(n):
    return f"{n:,}".replace(",", ".")


def _clip(text, limit):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _ext(filename):
    return Path(filename or "").suffix.lower()


# --- Excel ------------------------------------------------------------------

def _shared_head(zf, names):
    """Le PRIME stringhe condivise dell'xlsx. La tabella è ordinata per primo
    uso, quindi le intestazioni (riga 1) sono in testa: leggerne 5.000 basta
    per header e prime righe, e su un file da 20 MB evita di caricare in RAM
    una sharedStrings da centinaia di migliaia di voci."""
    if "xl/sharedStrings.xml" not in names:
        return []
    out = []
    try:
        with zf.open("xl/sharedStrings.xml") as fh:
            for event, el in ET.iterparse(fh, events=("end",)):
                if el.tag != _X + "si":
                    continue
                out.append(_si_text(el))
                el.clear()
                if len(out) >= _MAX_SST:
                    break
    except (ET.ParseError, KeyError, OSError):
        return out
    return out


_DIM_RE = re.compile(r"^([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$")


def _letters(s):
    n = 0
    for ch in s:
        n = n * 26 + (ord(ch) - 64)
    return n


def _scan_sheet(zf, part, sst):
    """(righe_dichiarate, colonne_dichiarate, prime righe non vuote).

    Le dimensioni arrivano da <dimension ref="A1:H1240"/>, che sta in testa
    alla part: sapere che il foglio ha 200.000 righe senza scorrerle tutte.
    Le righe si leggono in streaming e si smette dopo _SCAN_ROWS."""
    rows, n_rows, n_cols = [], None, None
    try:
        with zf.open(part) as fh:
            for event, el in ET.iterparse(fh, events=("end",)):
                if el.tag == _X + "dimension":
                    m = _DIM_RE.match((el.get("ref") or "").upper())
                    if m and m.group(3):
                        n_rows = int(m.group(4)) - int(m.group(2)) + 1
                        n_cols = _letters(m.group(3)) - _letters(m.group(1)) + 1
                    el.clear()
                    continue
                if el.tag != _X + "row":
                    continue
                cells = []
                for c in el:
                    if c.tag != _X + "c":
                        continue
                    txt = _cell_text(c, sst)
                    if txt and txt.strip():
                        cells.append((_col_idx(c, len(cells)), txt,
                                      c.get("t") in ("s", "inlineStr", "str")))
                el.clear()
                if cells:
                    rows.append(cells)
                if len(rows) >= _SCAN_ROWS:
                    break
    except (ET.ParseError, KeyError, OSError, ValueError):
        pass
    return n_rows, n_cols, rows


def _looks_header(cells):
    """Prima riga = intestazione? Regola volutamente semplice (e la stessa
    idea di engine.xlsx._detect_header): tutte celle testuali, corte, uniche."""
    if len(cells) < 2:
        return False
    vals = []
    for _col, txt, is_text in cells:
        v = txt.strip()
        if not is_text or not v or len(v) > 60 or "\n" in v:
            return False
        vals.append(v.casefold())
    return len(set(vals)) == len(vals)


def _row_line(cells):
    return _clip(" | ".join(_clip(t, _CELL_CHARS) for _c, t, _i in cells), 220)


def _excel(path):
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError("non è un workbook Excel")
        sst = _shared_head(zf, names)
        parts = _sheet_parts(zf, names)
        lines, tot = [], 0
        for part, sheet in parts[:_MAX_SHEETS]:
            n_rows, n_cols, rows = _scan_sheet(zf, part, sst)
            if n_rows:
                tot += n_rows
            size = []
            if n_rows is not None:
                size.append(f"{_num(n_rows)} righe")
            elif rows:
                size.append(f"almeno {len(rows)} righe")
            if n_cols is not None:
                size.append(f"{n_cols} colonne")
            head = f'foglio "{sheet or part.rsplit("/", 1)[-1]}"'
            lines.append(f"{head}: {' × '.join(size)}" if size
                         else f"{head}: vuoto")
            if not rows:
                continue
            if _looks_header(rows[0]):
                cols = [t.strip() for _c, t, _i in rows[0][:_MAX_COLS]]
                more = ("…" if len(rows[0]) > _MAX_COLS else "")
                lines.append("  colonne: " + " | ".join(cols) + more)
                sample = rows[1:1 + _SAMPLE_ROWS]
            else:
                sample = rows[:_SAMPLE_ROWS]
            for cells in sample:
                lines.append("  " + _row_line(cells))
        extra = len(parts) - _MAX_SHEETS
        if extra > 0:
            lines.append(f"(e altri {extra} fogli)")
        n_sheets = len(parts)
        label = f"{n_sheets} fogl{'io' if n_sheets == 1 else 'i'}"
        if tot:
            label += f" · {_num(tot)} righe"
        return {"kind": "excel", "label": label, "lines": lines}


# --- CSV / testo ------------------------------------------------------------

def _decode(raw):
    """(testo, nome della codifica). `raw` è spesso un TRONCONE del file: se
    l'ultimo carattere multi-byte è tagliato a metà si accorcia di qualche
    byte invece di dichiarare una codifica sbagliata."""
    bom = raw[:3] == b"\xef\xbb\xbf"
    if bom:
        raw = raw[3:]
    for enc in ("utf-8", "cp1252", "latin-1"):
        for cut in (0, 1, 2, 3):
            try:
                text = raw[:len(raw) - cut].decode(enc)
            except UnicodeDecodeError:
                continue
            return text, enc + (" con BOM" if bom else "")
    return raw.decode("latin-1", "replace"), "latin-1"


def _count_lines(path):
    n = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(1 << 20)
            if not block:
                break
            n += block.count(b"\n")
    return n


def _csv(path):
    head, enc = _decode(Path(path).read_bytes()[:65536])
    try:
        dialect = csv.Sniffer().sniff(head[:8192], delimiters=",;\t|")
        sep = dialect.delimiter
    except csv.Error:
        sep = ";" if head.count(";") > head.count(",") else ","
    rows = list(csv.reader(io.StringIO(head), delimiter=sep))
    rows = [r for r in rows if any(c.strip() for c in r)]
    n_lines = _count_lines(path)
    sep_name = {",": "virgola", ";": "punto e virgola", "\t": "tabulazione",
                "|": "barra verticale"}.get(sep, repr(sep))
    lines = [f"separatore: {sep_name} · codifica: {enc} · "
             f"{_num(n_lines)} righe (fine file inclusa)"]
    if rows:
        header = rows[0]
        if len(header) > 1 and all(c.strip() for c in header):
            lines.append("colonne: " + " | ".join(
                _clip(c, _CELL_CHARS) for c in header[:_MAX_COLS])
                + ("…" if len(header) > _MAX_COLS else ""))
            body = rows[1:1 + _SAMPLE_ROWS]
        else:
            body = rows[:_SAMPLE_ROWS]
        for r in body:
            lines.append("  " + _clip(" | ".join(
                _clip(c, _CELL_CHARS) for c in r[:_MAX_COLS]), 220))
    n_cols = len(rows[0]) if rows else 0
    return {"kind": "csv", "lines": lines,
            "label": f"{_num(n_lines)} righe · {n_cols} colonne"}


def _plain(path, kind="text"):
    text, enc = _decode(Path(path).read_bytes()[:65536])
    n = _count_lines(path)
    lines = [f"codifica: {enc} · {_num(n)} righe",
             "inizio del file:", "  " + _clip(text, _TEXT_CHARS)]
    return {"kind": kind, "label": f"testo, {_num(n)} righe", "lines": lines}


def _json_file(path):
    raw = Path(path).read_bytes()
    text, _enc = _decode(raw)
    try:
        data = json.loads(text)
    except ValueError as e:
        return {"kind": "json", "label": "JSON non valido",
                "lines": [f"il file non è JSON valido: {e}"]}
    if isinstance(data, list):
        label = f"array di {_num(len(data))} elementi"
        lines = [label]
        if data and isinstance(data[0], dict):
            lines.append("chiavi del primo elemento: "
                         + ", ".join(list(data[0])[:_MAX_COLS]))
    elif isinstance(data, dict):
        label = f"oggetto con {len(data)} chiavi"
        lines = [label, "chiavi: " + ", ".join(list(data)[:_MAX_COLS])]
    else:
        label = f"valore {type(data).__name__}"
        lines = [label]
    lines.append("inizio del file: " + _clip(text, 300))
    return {"kind": "json", "label": label, "lines": lines}


# --- PDF / Word / PowerPoint ------------------------------------------------

@mupdf_serialized
def _pdf(path):
    import fitz                              # come engine/pdf.py
    with fitz.open(path) as doc:
        if doc.needs_pass:
            return {"kind": "pdf", "label": "PDF protetto da password",
                    "lines": ["il PDF è cifrato: non è leggibile"]}
        n = doc.page_count
        title = (doc.metadata or {}).get("title") or ""
        text = doc[0].get_text() if n else ""
    lines = [f"{n} pagin{'a' if n == 1 else 'e'}"]
    if title.strip():
        lines.append(f"titolo nei metadati: {_clip(title, 120)}")
    if text.strip():
        lines.append("inizio della prima pagina:")
        lines.append("  " + _clip(text, _TEXT_CHARS))
    else:
        lines.append("nessun testo estraibile dalla prima pagina "
                     "(probabile scansione: servirebbe un OCR)")
    return {"kind": "pdf", "label": f"PDF, {n} pagin{'a' if n == 1 else 'e'}",
            "lines": lines}


def _xml_texts(zf, part, tag):
    try:
        root = ET.fromstring(zf.read(part))
    except (ET.ParseError, KeyError):
        return []
    return [el.text for el in root.iter(tag) if el.text and el.text.strip()]


_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A_T = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"


def _docx(path):
    with zipfile.ZipFile(path) as zf:
        if "word/document.xml" not in set(zf.namelist()):
            raise ValueError("non è un documento Word")
        try:
            root = ET.fromstring(zf.read("word/document.xml"))
        except ET.ParseError:
            raise ValueError("XML del documento non valido")
        paragraphs = []
        for p in root.iter(_W_NS + "p"):
            txt = "".join(t.text or "" for t in p.iter(_W_NS + "t")).strip()
            if txt:
                paragraphs.append(txt)
        n_tables = sum(1 for _ in root.iter(_W_NS + "tbl"))
    lines = [f"{_num(len(paragraphs))} paragrafi con testo"
             + (f" · {n_tables} tabelle" if n_tables else "")]
    if paragraphs:
        lines.append("inizio del documento:")
        lines.append("  " + _clip(" ⏎ ".join(paragraphs[:12]), _TEXT_CHARS))
    return {"kind": "word", "label": f"Word, {len(paragraphs)} paragrafi",
            "lines": lines}


def _pptx(path):
    with zipfile.ZipFile(path) as zf:
        slides = sorted(n for n in zf.namelist()
                        if re.match(r"^ppt/slides/slide\d+\.xml$", n))
        if not slides:
            raise ValueError("non è una presentazione PowerPoint")
        lines = [f"{len(slides)} diapositiv{'a' if len(slides) == 1 else 'e'}"]
        for i, part in enumerate(slides[:10], 1):
            texts = _xml_texts(zf, part, _A_T)
            if texts:
                lines.append(f"  {i}. {_clip(' · '.join(texts[:4]), 140)}")
    if len(slides) > 10:
        lines.append(f"  (e altre {len(slides) - 10})")
    return {"kind": "powerpoint",
            "label": f"PowerPoint, {len(slides)} diapositive", "lines": lines}


# --- immagini ---------------------------------------------------------------

def _image_size(raw):
    """Dimensioni in pixel leggendo l'INTESTAZIONE (png/jpeg/gif/bmp/webp).
    Niente Pillow: nel backend non c'è (e decodificare l'immagine per
    sapere quanto è grande sarebbe sproporzionato)."""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
            return (int.from_bytes(raw[16:20], "big"),
                    int.from_bytes(raw[20:24], "big"))
        if raw[:3] == b"GIF":
            return (int.from_bytes(raw[6:8], "little"),
                    int.from_bytes(raw[8:10], "little"))
        if raw[:2] == b"BM":
            return (int.from_bytes(raw[18:22], "little"),
                    int.from_bytes(raw[22:26], "little"))
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            if raw[12:16] == b"VP8X":
                return (int.from_bytes(raw[24:27], "little") + 1,
                        int.from_bytes(raw[27:30], "little") + 1)
            return None
        if raw[:2] == b"\xff\xd8":              # JPEG: cerca il marker SOF
            i = 2
            while i + 9 < len(raw):
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return (int.from_bytes(raw[i + 7:i + 9], "big"),
                            int.from_bytes(raw[i + 5:i + 7], "big"))
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                i += 2 + int.from_bytes(raw[i + 2:i + 4], "big")
    except (IndexError, ValueError):
        pass
    return None


def _image(path, mime):
    raw = Path(path).read_bytes()[:65536]
    wh = _image_size(raw)
    fmt = (mime or "").split("/")[-1].upper() or _ext(path).lstrip(".").upper()
    label = fmt + (f", {wh[0]}×{wh[1]} px" if wh else "")
    lines = [f"immagine {label}"]
    return {"kind": "image", "label": label, "lines": lines}


# --- dispatch ---------------------------------------------------------------

_BY_EXT = {
    ".xlsx": _excel, ".xlsm": _excel,
    ".csv": _csv, ".tsv": _csv,
    ".pdf": _pdf,
    ".docx": _docx,
    ".pptx": _pptx,
    ".json": _json_file,
}
_PLAIN_EXT = {".txt", ".md", ".log", ".xml", ".html", ".htm", ".yaml", ".yml",
              ".py", ".sql", ".ini", ".cfg"}
IMAGE_MIMES = ("image/png", "image/jpeg", "image/gif", "image/webp")
# Formati che sul server non sappiamo leggere ma che nella sandbox si aprono:
# LibreOffice c'è, basta dirlo al modello invece di lasciarlo tentare.
_LEGACY = {".xls": ("Excel 97-2003", "xlsx"), ".doc": ("Word 97-2003", "docx"),
           ".ppt": ("PowerPoint 97-2003", "pptx"),
           ".ods": ("OpenDocument Calc", "xlsx"),
           ".odt": ("OpenDocument Writer", "docx"),
           ".odp": ("OpenDocument Impress", "pptx")}


def describe(path, filename=None, mime=None):
    """Scheda del file: {kind, label, lines[, error]}. Non solleva mai."""
    filename = filename or Path(path).name
    ext = _ext(filename)
    if ext in _LEGACY:
        name, target = _LEGACY[ext]
        return {"kind": "legacy", "label": name,
                "lines": [f"formato {name}: il contenuto non è leggibile "
                          f"così. Nella sandbox convertilo prima con "
                          f"`soffice --headless --convert-to {target} "
                          f"--outdir /tmp <file>`, poi lavora sulla copia."]}
    handler = _BY_EXT.get(ext)
    if handler is None and (ext in _PLAIN_EXT
                            or (mime or "").startswith("text/")):
        handler = _plain
    if handler is None and ((mime or "").startswith("image/")
                            or ext in (".png", ".jpg", ".jpeg", ".gif",
                                       ".webp", ".bmp")):
        try:
            return _image(path, mime)
        except OSError as e:
            return {"kind": "binary", "label": "", "error": str(e),
                    "lines": []}
    if handler is None:
        return {"kind": "binary", "label": "", "lines": []}
    try:
        return handler(path)
    except Exception as e:                   # file corrotto: scheda povera
        return {"kind": "binary", "label": "",
                "error": f"contenuto non analizzabile ({e})", "lines": []}


def as_prompt(filename, size, card):
    """La scheda come testo per il contesto. Formato deliberatamente asciutto
    (righe indentate, nessun markdown): questo blocco entra nel messaggio
    dell'utente e non deve sembrare una risposta da imitare."""
    head = f"- {filename} ({fmt_size(size)}"
    if card and card.get("label"):
        head += f", {card['label']}"
    head += ")"
    if not card:
        return head
    out = [head]
    if card.get("error"):
        out.append(f"    {card['error']}")
    for line in card.get("lines") or ():
        out.append("    " + line)
    text = "\n".join(out)
    if len(text) > MAX_CARD_CHARS:
        text = text[:MAX_CARD_CHARS].rstrip() + "…"
    return text


_PDF_MIME = "application/pdf"


def as_model_part(path, filename, mime, modalities, max_bytes):
    """Il file come CONTENUTO che il modello guarda (parte multimodale
    OpenRouter), o None se deve restare solo nella sandbox.

    Ogni file ha DUE destinazioni: la sandbox, sempre, e gli
    occhi del modello, solo per immagini (visione) e PDF (file nativi) e solo
    se le input_modalities lo permettono. Senza la seconda, «cosa c'è in
    questa foto?» non ha risposta: nella sandbox pillow può solo contare i
    pixel. Il file viaggia in base64 in OGNI richiesta del turno e si paga
    come token, quindi sopra `max_bytes` (pannello admin) non si allega e
    resta il solo percorso sandbox."""
    if path is None or not path.is_file() or path.stat().st_size > max_bytes:
        return None
    mime = mimetypes.guess_type(filename)[0] or mime or ""
    if mime in IMAGE_MIMES and "image" in modalities:
        data = base64.b64encode(path.read_bytes()).decode()
        return {"type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{data}"}}
    if mime == _PDF_MIME and "file" in modalities:
        data = base64.b64encode(path.read_bytes()).decode()
        return {"type": "file",
                "file": {"filename": filename,
                         "file_data": f"data:{_PDF_MIME};base64,{data}"}}
    return None

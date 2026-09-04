"""Box delle entità lette dall'OCR sull'anteprima degli OOXML (docx/pptx/xlsx).

L'anteprima di un OOXML è una RICONVERSIONE LibreOffice: i placeholder che
l'OCR ha dipinto NEI PIXEL delle immagini non entrano nel layer testo del PDF
convertito, quindi la sola ricerca testuale non li trova e l'overlay
interattivo del viewer (tooltip col valore, "Deanonimizza") mancava proprio
dove aveva lavorato l'OCR — il giallo si vedeva, ma non c'era niente da
passarci sopra col mouse. Qui si verifica che i box ci siano su tutti e tre i
formati e su entrambi i lati, e che cadano DAVVERO sopra i rettangoli gialli.

Le fixture si costruiscono al volo: un PNG con del testo dentro, impaginato in
un flat-ODF che LibreOffice converte nei tre formati OOXML (nessun file di
test in repo ha immagini). Il valore da anonimizzare non è scritto a mano ma
si legge da ciò che l'OCR ha davvero visto nell'immagine: così il test non
cade per una lettura leggermente diversa del modello.

Prerequisiti: stack OCR (rapidocr+onnxruntime+pillow) e LibreOffice.

    python backend/tests/ooxml_ocr_boxes_test.py
"""

import base64
import io
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

DATA_DIR = HERE / "data" / "test_ooxml_ocr_boxes"
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA_DIR)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

import fitz                                                      # noqa: E402
from PIL import Image, ImageDraw                                 # noqa: E402

from app import chat_anonymization as ca                         # noqa: E402
from app import chat_staging                                     # noqa: E402
from app.db import Attachment, Conversation, init_db             # noqa: E402
from app.engine import convert, image_ocr                        # noqa: E402

# profilo LibreOffice isolato: il backend di sviluppo può essere acceso e i
# due processi non possono condividerlo
convert._PROFILE_DIR = DATA_DIR / "lo_profile"

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class FakeEngine:
    """Detector deterministico sui valori passati (come chat_anonymization_test)."""

    def __init__(self, entries):
        self.entries = entries

    def analyze(self, text, excluded=None, **_kwargs):
        skip = set(excluded or ())
        entities = []
        for label, value in self.entries:
            if label in skip:
                continue
            for match in re.finditer(re.escape(value), text, re.IGNORECASE):
                entities.append({
                    "label": label, "start": match.start(), "end": match.end(),
                    "value": text[match.start():match.end()], "source": "test",
                    "validated": True, "ph": "[LOCAL_1]",
                })
        entities.sort(key=lambda e: e["start"])
        return {"entities": entities, "anonymized_text": text, "mapping": {},
                "n_entities": len(entities), "n_unique": len(entities),
                "by_label": {}}


# --- fixture: immagini con testo, impaginate in un OOXML --------------------- #

def png_with_text(lines, size=(900, 300), tweak=False):
    """PNG bianco con delle righe di testo nero abbastanza grandi da essere
    lette. `tweak`: un pixel diverso, per fabbricare due immagini quasi
    identiche (il caso in cui l'accoppiamento deve dichiararsi incerto)."""
    im = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(im)
    font = image_ocr._font(56)
    for i, line in enumerate(lines):
        draw.text((40, 50 + i * 100), line, fill="black", font=font)
    if tweak:
        draw.rectangle([size[0] - 6, size[1] - 6, size[0] - 2, size[1] - 2],
                       fill="black")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
       'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
       'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
       'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
       'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"')


def _frame(png, y_cm):
    return ('<draw:frame svg:width="16cm" svg:height="5.3cm" svg:x="2cm" '
            f'svg:y="{y_cm}cm"><draw:image><office:binary-data>'
            f'{base64.b64encode(png).decode()}'
            '</office:binary-data></draw:image></draw:frame>')


def ooxml_with_images(ext, pngs):
    """docx/pptx/xlsx con le immagini date, costruiti facendo convertire a
    LibreOffice un flat-ODF (l'unico modo per avere delle part media senza
    dipendere da python-docx/pptx e senza fixture binarie in repo)."""
    frames = "".join(_frame(p, 1 + i * 6) for i, p in enumerate(pngs))
    if ext == ".pptx":
        body = (f'<office:presentation><draw:page draw:name="pagina1">'
                f'{frames}</draw:page></office:presentation>')
        mime, suffix, to = "presentation", ".fodp", convert.to_pptx
    elif ext == ".docx":
        paras = "".join(f"<text:p>{f}</text:p>" for f in
                        [_frame(p, 1) for p in pngs])
        body = f"<office:text>{paras}<text:p>Allegato tecnico.</text:p></office:text>"
        mime, suffix, to = "text", ".fodt", convert.to_docx
    else:
        body = ('<office:spreadsheet><table:table table:name="Foglio1">'
                f'<table:shapes>{frames}</table:shapes>'
                '<table:table-row><table:table-cell office:value-type="string">'
                '<text:p>Allegato tecnico</text:p></table:table-cell>'
                '</table:table-row></table:table></office:spreadsheet>')
        mime, suffix, to = "spreadsheet", ".fods", convert.to_xlsx
    doc = ('<?xml version="1.0" encoding="UTF-8"?>'
           f'<office:document {_NS} office:version="1.3" '
           f'office:mimetype="application/vnd.oasis.opendocument.{mime}">'
           f"<office:body>{body}</office:body></office:document>")
    return to(doc.encode("utf-8"), suffix=suffix)


def ocr_words(png):
    """Le parole che l'OCR legge davvero nell'immagine, dalla più lunga: il
    valore del detector finto si sceglie da qui, non a tavolino."""
    cache = image_ocr.build_cache([{"key": "k", "ext": "png", "data": png}])
    if cache is None:
        return []
    return sorted((line["t"] for line in cache["images"][0]["lines"]),
                  key=len, reverse=True)


# --- verifica geometrica: il box cade sul giallo? ---------------------------- #

def yellow_ratio(pdf_bytes, page_no, box, samples=5):
    """Quanta parte del rettangolo è gialla nel render della pagina: se il box
    è proiettato bene copre il rettangolo dipinto nei pixel."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        page = doc[page_no]
        rect = page.rect
        png = page.get_pixmap(dpi=150).tobytes("png")
    with Image.open(io.BytesIO(png)) as raw:
        im = raw.convert("RGB")
    sx, sy = im.size[0] / rect.width, im.size[1] / rect.height
    hits = 0
    for i in range(samples):
        for j in range(samples):
            x = (box["x0"] + (box["x1"] - box["x0"]) * (i + 0.5) / samples) * sx
            y = (box["y0"] + (box["y1"] - box["y0"]) * (j + 0.5) / samples) * sy
            x = min(im.size[0] - 1, max(0, int(x)))
            y = min(im.size[1] - 1, max(0, int(y)))
            r, g, b = im.getpixel((x, y))
            if r > 200 and g > 190 and b < 140:
                hits += 1
    return hits / (samples * samples)


# --- turno in anteprima ------------------------------------------------------ #

def staged_item(name, ext, data):
    """Allegato -> turno anonimizzato con OCR -> il descrittore del suo pezzo
    di anteprima (la stessa forma che consuma il viewer)."""
    SessionLocal = init_db()
    with SessionLocal() as session:
        conv = Conversation(owner_id=1, anonymized=1)
        session.add(conv)
        session.flush()
        cid = conv.id
        att_dir = DATA_DIR / "chats" / cid
        att_dir.mkdir(parents=True, exist_ok=True)
        att = Attachment(conv_id=cid, direction="in", filename=name,
                         display_filename=name, mime="application/octet-stream",
                         size=len(data), anonymization_status="pending")
        session.add(att)
        session.flush()
        path = att_dir / f"{att.id}_{name}"
        path.write_bytes(data)
        att.original_path = str(path)
        att_id = att.id
        session.commit()
    chat_staging.stage_turn(cid, "Riassumi l'allegato.", [att_id], ocr=True)
    desc = chat_staging.descriptor(cid)
    item = next(i for i in desc["items"] if i["kind"] == "attachment")
    return cid, item


def boxes_of(item, side):
    return [b for blist in (item[side] or {}).values() for b in blist]


# --- 1. i tre formati -------------------------------------------------------- #

def part_formats(value, png):
    for ext in (".pptx", ".docx", ".xlsx"):
        print(f"\n--- {ext}: entità letta nell'immagine ---")
        try:
            data = ooxml_with_images(ext, [png])
        except convert.ConvertError as exc:
            check(f"{ext}: fixture costruita", False, str(exc)[:160])
            continue
        n_media = image_ocr.count_images(data, ext)
        check(f"{ext}: fixture con un'immagine dentro", n_media == 1,
              f"media={n_media}")

        cid, item = staged_item(f"allegato{ext}", ext, data)
        anon = boxes_of(item, "anonymized_boxes")
        orig = boxes_of(item, "original_boxes")
        check(f"{ext}: box sul lato ANONIMIZZATO", bool(anon),
              f"{len(anon)} box, ph={[b['ph'] for b in anon]}")
        check(f"{ext}: box anche sul lato originale", bool(orig),
              f"{len(orig)} box")
        check(f"{ext}: box marcati come letti dall'OCR",
              bool(anon) and all(b.get("ocr") for b in anon))
        check(f"{ext}: ogni box ha placeholder e categoria (contratto viewer)",
              bool(anon) and all(b.get("ph") and b.get("label") for b in anon))
        # il tooltip mostra mapping[ph]: il placeholder deve stare in mappa
        check(f"{ext}: il placeholder è nella mappa del pezzo",
              bool(anon) and any(item["mapping"].get(b["ph"]) == value
                                 for b in anon),
              f"mappa={item['mapping']}")

        # un'immagine a cavallo di un salto pagina viene ridisegnata sbordando
        # sulla pagina dopo: nessun box può finire fuori dalla sua pagina
        sizes = item["page_sizes"]["anonymized"]
        inside = all(b["x0"] >= 0 and b["y0"] >= 0
                     and b["x1"] <= sizes[int(pno)]["width"] + 0.5
                     and b["y1"] <= sizes[int(pno)]["height"] + 0.5
                     for pno, blist in item["anonymized_boxes"].items()
                     for b in blist)
        check(f"{ext}: nessun box fuori dalla sua pagina", inside)

        pdf = chat_staging.preview_pdf(cid, item["id"], "anonymized")
        ratios = [yellow_ratio(pdf, pno, b)
                  for pno, blist in item["anonymized_boxes"].items()
                  for b in blist]
        check(f"{ext}: i box cadono sul rettangolo giallo",
              bool(ratios) and min(ratios) >= 0.8,
              f"copertura min {min(ratios):.2f}" if ratios else "nessun box")


# --- 2. due immagini della stessa taglia: si sceglie quella giusta ----------- #

def part_same_size(value, png, other):
    print("\n--- due immagini identiche di taglia, una sola con l'entità ---")
    data = ooxml_with_images(".pptx", [other, png])
    check("fixture con due media della stessa taglia",
          image_ocr.count_images(data, ".pptx") == 2)
    _cid, item = staged_item("due_immagini.pptx", ".pptx", data)
    anon = boxes_of(item, "anonymized_boxes")
    check("box solo per l'immagine che contiene l'entità",
          bool(anon) and all(b["ph"] in item["mapping"] for b in anon),
          f"{len(anon)} box")
    pdf = chat_staging.preview_pdf(_cid, item["id"], "anonymized")
    ratios = [yellow_ratio(pdf, pno, b)
              for pno, blist in item["anonymized_boxes"].items()
              for b in blist]
    check("nessun box finito sull'immagine sbagliata",
          bool(ratios) and min(ratios) >= 0.8,
          f"copertura min {min(ratios):.2f}" if ratios else "nessun box")


# --- 3. due immagini quasi identiche: nel dubbio nessun box ------------------ #

def part_ambiguous(value, png):
    print("\n--- due immagini quasi identiche: accoppiamento incerto ---")
    twin = png_with_text([value, "Riferimento interno"], tweak=True)
    data = ooxml_with_images(".pptx", [png, twin])
    check("fixture con due media quasi identiche",
          image_ocr.count_images(data, ".pptx") == 2)
    _cid, item = staged_item("gemelle.pptx", ".pptx", data)
    anon = boxes_of(item, "anonymized_boxes")
    check("nel dubbio nessun box (mai uno fuori posto)", not anon,
          f"{len(anon)} box")
    # ...ma la REDAZIONE nei pixel c'è stata lo stesso: l'ambiguità riguarda
    # solo l'overlay dell'anteprima, non la copertura del file
    check("le occorrenze nei pixel restano coperte",
          (item["report"].get("image_occurrences") or 0) > 0,
          f"image_occurrences={item['report'].get('image_occurrences')}")


def main():
    if not image_ocr.available():
        print("stack OCR non installato: test saltato")
        return
    if not convert.available():
        print("LibreOffice non trovato: test saltato")
        return

    png = png_with_text(["Verdi", "Riferimento interno"])
    words = ocr_words(png)
    if not words:
        print("l'OCR non ha letto nulla nell'immagine di prova: test saltato")
        return
    value = words[0]
    print(f"valore letto dall'OCR e dato al detector finto: {value!r}")
    other = png_with_text(["Pagina di copertina", "documento tecnico"])

    ca._base_engine_real = ca._base_engine
    ca._base_engine = lambda: FakeEngine([("ORG", value)])
    try:
        part_formats(value, png)
        part_same_size(value, png, other)
        part_ambiguous(value, png)
    finally:
        ca._base_engine = ca._base_engine_real

    print(f"\n{PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

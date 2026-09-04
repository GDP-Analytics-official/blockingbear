"""Stress del tool read_document_images: 10 file per formato (docx, pptx,
xlsx, xlsm, pdf, immagini nude), test eterogenei per ciascuno, OCR VERO.

A differenza di read_images_tool_test.py (che monkeypatcha build_cache),
qui l'OCR è quello reale (RapidOCR) su immagini renderizzate con Pillow;
i produttori sono veri dove possibile (LibreOffice via flat-ODF per un file
per formato OOXML, PyMuPDF per i PDF); il NER è il FakeEngine deterministico
dei test di anonimizzazione (la qualità del modello è testata altrove), ma
il resto della gamba anonimizzata è vero: registro, lock, egress check.

Assi coperti per formato: PII in chiaro/anonimizzata, più immagini e formati
media misti, icone sotto MIN_SIDE, nessuna immagine, file corrotti, tetto
_OCR_MAX_IMAGES, grafica senza testo, cache RAM (niente ri-OCR), via veloce
dalla cache persistita (allegato chat e file di progetto), filtro `page` sui
PDF, pagine scansionate e vettoriali, nomi con accenti/estensioni maiuscole,
percorso mancante, argomenti sporchi, concorrenza (8 chiamate parallele) e
sfratto della cache RAM.

Prerequisiti: stack OCR (rapidocr+onnxruntime+pillow); LibreOffice è usato
se presente (altrimenti fallback zip, dichiarato a video).

    python backend/tests/read_images_stress_test.py
"""
import asyncio
import io
import json
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

DATA = HERE / "data" / "test_read_images_stress"
shutil.rmtree(DATA, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_SANDBOX_ENGINE"] = "off"

from PIL import Image, ImageDraw                                  # noqa: E402

from app import chat_anonymization as ca                          # noqa: E402
from app import db, project_files                                 # noqa: E402
from app.auth import seed_admin                                   # noqa: E402
from app.db import (Attachment, Conversation, Project,            # noqa: E402
                    ProjectFile, init_db)
from app.engine import image_ocr                                  # noqa: E402
from app.openrouter import tools as or_tools                      # noqa: E402

if not image_ocr.available():
    print("Stack OCR non disponibile: lo stress richiede l'OCR vero.")
    sys.exit(1)

PASS = 0
FAIL = 0
T0 = time.monotonic()


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


SessionLocal = init_db()
with SessionLocal() as s:
    seed_admin(s)

WORK = DATA / "work"
WORK.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# NER finto deterministico (la stessa sagoma di chat_anonymization_test):
# il registro, i lock e l'egress check restano quelli veri.
# --------------------------------------------------------------------------- #
class FakeEngine:
    # token singoli: l'OCR reale spesso spezza le parole in righe separate e
    # anonymize_web_texts anonimizza campo per campo — un finto detector a
    # frase intera non vedrebbe mai "Mario Rossi" diviso su due righe
    ENTRIES = (("FULLNAME", "Mario"), ("FULLNAME", "Rossi"),
               ("ORG", "Impianti"))

    def analyze(self, text, excluded=None, **_kw):
        skip = set(excluded or ())
        entities = []
        for label, value in self.ENTRIES:
            if label in skip:
                continue
            for m in re.finditer(re.escape(value), text, re.IGNORECASE):
                entities.append({"label": label, "start": m.start(),
                                 "end": m.end(), "value": m.group(0),
                                 "source": "test", "validated": True,
                                 "ph": "[X_1]"})
        entities.sort(key=lambda e: e["start"])
        return {"entities": entities, "anonymized_text": text, "mapping": {},
                "n_entities": len(entities), "n_unique": len(entities),
                "by_label": {}}


ca._base_engine = lambda: FakeEngine()

PH_RE = re.compile(r"\[[A-Z][A-Z0-9]*_\d+\]")


# --------------------------------------------------------------------------- #
# fabbrica di immagini e file
# --------------------------------------------------------------------------- #
_PIL_FMT = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".bmp": "BMP",
            ".gif": "GIF", ".tif": "TIFF", ".tiff": "TIFF", ".webp": "WEBP"}


def text_img(lines, ext=".png", width=760, font_size=48):
    """Immagine bianca con righe di testo nero grandi: l'OCR le legge."""
    if isinstance(lines, str):
        lines = [lines]
    h = 44 + len(lines) * (font_size + 34)
    im = Image.new("RGB", (width, h), "white")
    draw = ImageDraw.Draw(im)
    font = image_ocr._font(font_size)
    for i, line in enumerate(lines):
        draw.text((28, 22 + i * (font_size + 34)), line,
                  fill="black", font=font)
    buf = io.BytesIO()
    im.save(buf, _PIL_FMT[ext])
    return buf.getvalue()


def shapes_img(ext=".png"):
    """Solo grafica, nessun testo."""
    im = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([40, 40, 180, 160], outline="black", width=6)
    d.ellipse([220, 80, 360, 220], fill=(90, 140, 220))
    d.line([20, 280, 380, 20], fill="red", width=4)
    buf = io.BytesIO()
    im.save(buf, _PIL_FMT[ext])
    return buf.getvalue()


def tiny_img():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (120, 120, 120)).save(buf, "PNG")
    return buf.getvalue()


def garbled_img():
    """Testo degradato (piccolo, storto, rumoroso): letture incerte o nulle."""
    im = Image.new("RGB", (300, 80), (180, 180, 180))
    d = ImageDraw.Draw(im)
    d.text((8, 30), "fattura n. 88 del 3/2", fill=(140, 140, 140),
           font=image_ocr._font(13))
    for x in range(0, 300, 3):
        d.point((x, (x * 7) % 80), fill="black")
    im = im.rotate(8, expand=False, fillcolor=(180, 180, 180))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


_MEDIA_DIR = {".docx": "word", ".pptx": "ppt", ".xlsx": "xl", ".xlsm": "xl"}


def make_ooxml(path, images, extra_parts=()):
    """OOXML minimo via zip: a ooxml_images bastano le part media vere.
    `images` = [(nome_part_relativo, bytes)]."""
    ext = path.suffix.lower()
    root = _MEDIA_DIR[ext]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{root}/document.xml", "<root/>")
        for name, data in images:
            zf.writestr(f"{root}/media/{name}", data)
        for name, data in extra_parts:
            zf.writestr(name, data)
    return path


# --- OOXML "veri" via LibreOffice (flat-ODF), con fallback zip --------------- #
LO_OK = True
try:
    from app.engine import convert
    convert._PROFILE_DIR = DATA / "lo_profile"
except Exception:
    LO_OK = False

_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
       'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
       'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:'
       'svg-compatible:1.0" '
       'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
       'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"')


def _frame(png, y_cm):
    import base64
    return ('<draw:frame svg:width="16cm" svg:height="5.3cm" svg:x="2cm" '
            f'svg:y="{y_cm}cm"><draw:image><office:binary-data>'
            f'{base64.b64encode(png).decode()}'
            '</office:binary-data></draw:image></draw:frame>')


def lo_ooxml(path, pngs):
    """docx/pptx/xlsx PRODOTTO DA LIBREOFFICE con le immagini date; se LO
    manca o fallisce, fallback zip (dichiarato)."""
    global LO_OK
    ext = path.suffix.lower()
    if LO_OK:
        try:
            frames = "".join(_frame(p, 1 + i * 6) for i, p in enumerate(pngs))
            if ext == ".pptx":
                body = ('<office:presentation><draw:page draw:name="p1">'
                        f"{frames}</draw:page></office:presentation>")
                mime, suffix, to = "presentation", ".fodp", convert.to_pptx
            elif ext == ".docx":
                paras = "".join(f"<text:p>{_frame(p, 1)}</text:p>"
                                for p in pngs)
                body = (f"<office:text>{paras}<text:p>Allegato tecnico."
                        "</text:p></office:text>")
                mime, suffix, to = "text", ".fodt", convert.to_docx
            else:
                body = ('<office:spreadsheet><table:table '
                        'table:name="Foglio1"><table:shapes>'
                        f"{frames}</table:shapes><table:table-row>"
                        '<table:table-cell office:value-type="string">'
                        "<text:p>dati</text:p></table:table-cell>"
                        "</table:table-row></table:table>"
                        "</office:spreadsheet>")
                mime, suffix, to = "spreadsheet", ".fods", convert.to_xlsx
            doc = ('<?xml version="1.0" encoding="UTF-8"?>'
                   f'<office:document {_NS} office:version="1.3" '
                   'office:mimetype="application/vnd.oasis.'
                   f'opendocument.{mime}">'
                   f"<office:body>{body}</office:body></office:document>")
            path.write_bytes(to(doc.encode("utf-8"), suffix=suffix))
            return path, "LibreOffice"
        except Exception as e:
            LO_OK = False
            print(f"  (LibreOffice non usabile, fallback zip: {e})")
    make_ooxml(path, [(f"image{i}.png", p) for i, p in enumerate(pngs, 1)])
    return path, "zip"


def make_pdf(path, pages):
    """PDF via PyMuPDF. `pages` = lista di liste di elementi:
    ("text", str) | ("img", bytes) | ("rect",)."""
    import fitz
    doc = fitz.open()
    for elems in pages:
        page = doc.new_page(width=595, height=842)
        y = 60.0
        for elem in elems:
            if elem[0] == "text":
                page.insert_text((72, y), elem[1], fontsize=12)
                y += 30
            elif elem[0] == "img":
                rect = fitz.Rect(72, y, 523, y + 180)
                page.insert_image(rect, stream=elem[1])
                y += 200
            elif elem[0] == "rect":
                page.draw_rect(fitz.Rect(72, y, 300, y + 90),
                               color=(0, 0, 0), width=2)
                y += 110
    data = doc.tobytes()
    doc.close()
    path.write_bytes(data)
    return path


def scan_pdf(path, png):
    """Una pagina che È un'immagine (scansione): nessun testo nativo."""
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(fitz.Rect(0, 0, 595, 842), stream=png)
    data = doc.tobytes()
    doc.close()
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------- #
# infrastruttura chiamate
# --------------------------------------------------------------------------- #
def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def new_conv(anonymized=False, project_id=None):
    with db.SessionLocal() as s:
        conv = Conversation(owner_id=1, anonymized=1 if anonymized else 0,
                            project_id=project_id)
        s.add(conv)
        s.commit()
        return conv.id


def call(conv_id, files, filename, anonymized=False, page=None, scope=None):
    args = {"filename": filename}
    if page is not None:
        args["page"] = page
    ctx = {"files": files, "anonymized": anonymized,
           "scope": scope or conv_id}
    try:
        return run(or_tools._read_document_images(conv_id, args, ctx))
    except Exception as e:                 # il contratto vieta le eccezioni
        return {"outcome": "exception", "stderr": repr(e), "images": []}


def all_text(res):
    return "\n".join(i.get("text", "") for i in res.get("images", []))


class _Raiser:
    def __call__(self, *a, **k):
        raise AssertionError("OCR non atteso su questo percorso")


def no_ocr(fn):
    """Esegue fn con build_cache sabotato: se l'OCR parte, il test cade."""
    real = image_ocr.build_cache
    image_ocr.build_cache = _Raiser()
    try:
        return fn()
    finally:
        image_ocr.build_cache = real


def seed_registry(scope):
    """Registra i token PII nel registro vero dello scope (come se il file
    fosse passato dall'anonimizzazione) e ritorna il tag di "Mario"."""
    ca.anonymize_web_texts(scope, ["Contratto con Mario Rossi"])
    mapping = or_tools._scope_mapping(scope)
    for ph, v in mapping.items():
        if v == "Mario":
            return ph
    return None


def no_pii(text):
    return not re.search(r"Mario|Rossi|Impianti", text)


# --------------------------------------------------------------------------- #
# 1. DOCX — 10 file
# --------------------------------------------------------------------------- #
print("\n== DOCX ==")

d1, prod = lo_ooxml(WORK / "d1_contratto.docx",
                    [text_img("Contratto con Mario Rossi")])
conv = new_conv()
res = call(conv, {"d1.docx": str(d1)}, "d1.docx")
check(f"d1 ({prod}): 1 immagine letta, PII in chiaro (raw)",
      res["outcome"] == "ok" and len(res["images"]) == 1
      and "Rossi" in all_text(res), res.get("stderr") or all_text(res))

d2, prod = lo_ooxml(WORK / "d2_doppia.docx",
                    [text_img("Preventivo lavori 2026"),
                     text_img("Sconto del venti percento")])
res = call(conv, {"d2.docx": str(d2)}, "d2.docx")
check(f"d2 ({prod}): 2 immagini nell'ordine",
      res["outcome"] == "ok" and len(res["images"]) == 2
      and "Preventivo" in res["images"][0]["text"]
      and "percento" in res["images"][1]["text"],
      all_text(res))

d3 = make_ooxml(WORK / "d3_misto.docx",
                [("image1.png", text_img("prima nota", ".png")),
                 ("image2.jpg", text_img("seconda nota", ".jpg")),
                 ("image3.bmp", text_img("terza nota", ".bmp"))])
res = call(conv, {"d3.docx": str(d3)}, "d3.docx")
check("d3: media png+jpg+bmp tutte enumerate",
      res["outcome"] == "ok" and len(res["images"]) == 3, all_text(res))
check("d3: contenuti giusti nell'ordine delle part",
      "prima" in res["images"][0]["text"]
      and "seconda" in res["images"][1]["text"]
      and "terza" in res["images"][2]["text"], all_text(res))

d4 = make_ooxml(WORK / "d4_icone.docx",
                [(f"icon{i}.png", tiny_img()) for i in range(3)])
res = call(conv, {"d4.docx": str(d4)}, "d4.docx")
check("d4: sole icone sotto MIN_SIDE -> nessuna immagine leggibile",
      res["outcome"] == "ok" and not res["images"]
      and "Nessuna immagine leggibile" in (res.get("notice") or ""),
      res.get("notice"))

d5 = make_ooxml(WORK / "d5_senza.docx", [])
res = call(conv, {"d5.docx": str(d5)}, "d5.docx")
check("d5: nessuna media -> notice, non errore",
      res["outcome"] == "ok" and not res["images"]
      and "Nessuna immagine" in (res.get("notice") or ""), res.get("notice"))

d6 = make_ooxml(WORK / "d6_venticinque.docx",
                [(f"image{i:02d}.png", text_img(f"riga {i}", width=340,
                                                font_size=40))
                 for i in range(1, 26)])
res = call(conv, {"d6.docx": str(d6)}, "d6.docx")
check("d6: 25 immagini -> tetto a 20 con notice",
      res["outcome"] == "ok" and len(res["images"]) == 20
      and "prime 20" in (res.get("notice") or ""), res.get("notice"))
check("d6: niente suggerimento `page` (non è un PDF)",
      "page" not in (res.get("notice") or ""), res.get("notice"))

d7 = make_ooxml(WORK / "d7_grafica.docx", [("image1.png", shapes_img())])
res = call(conv, {"d7.docx": str(d7)}, "d7.docx")
check("d7: grafica senza testo -> ok, nessuna parola inventata",
      res["outcome"] == "ok"
      and not any(re.search(r"[a-zA-Z]{3,}", i.get("text", ""))
                  for i in res["images"]),
      f"notice={res.get('notice')} images={res.get('images')}")

d8 = WORK / "d8_corrotto.docx"
d8.write_bytes(b"PK\x03\x04troncato a meta'...")
res = call(conv, {"d8.docx": str(d8)}, "d8.docx")
check("d8: zip corrotto -> mai un'eccezione",
      res["outcome"] == "ok" and not res["images"]
      and "Nessuna immagine" in (res.get("notice") or ""),
      f"{res['outcome']}: {res.get('stderr') or res.get('notice')}")

d9 = make_ooxml(WORK / "d9_riservato.docx",
                [("image1.png", text_img(["Contratto con Mario Rossi",
                                          "fornitore Rossi Impianti"]))])
aconv = new_conv(anonymized=True)
res = call(aconv, {"d9.docx": str(d9)}, "d9.docx", anonymized=True)
check("d9: anon fresco -> segnaposto, niente PII",
      res["outcome"] == "ok" and no_pii(all_text(res))
      and PH_RE.search(all_text(res)), all_text(res))
check("d9: il registro dello scope si è popolato",
      any(v == "Mario"
          for v in or_tools._scope_mapping(aconv).values()))

# d10: via veloce — cache persistita dell'allegato + registro vero
fconv = new_conv(anonymized=True)
ph = seed_registry(fconv)
d10 = WORK / "d10_orig.docx"
make_ooxml(d10, [("image1.png",
                  text_img(["Contratto con Mario Rossi", "durata: due anni"]))])
stored = image_ocr.build_cache(image_ocr.ooxml_images(d10.read_bytes()))
with db.SessionLocal() as s:
    att = Attachment(conv_id=fconv, direction="in", filename="doc.docx",
                     model_filename="d10.docx", size=d10.stat().st_size,
                     mime="application/vnd.openxmlformats-officedocument"
                          ".wordprocessingml.document",
                     original_path=str(d10),
                     anonymization_status="protected",
                     protected_path=str(d10))
    s.add(att)
    s.commit()
    # la cache OCR vive ACCANTO all'originale (chat_anon._ocr_cache_path)
    (d10.parent / f"{att.id}_ocr.json").write_text(
        json.dumps(stored, ensure_ascii=False), encoding="utf-8")
res = no_ocr(lambda: call(fconv, {"d10.docx": str(d10)}, "d10.docx",
                          anonymized=True))
check("d10: via veloce senza OCR, tag del registro al posto della PII",
      res["outcome"] == "ok" and ph and ph in all_text(res)
      and no_pii(all_text(res)),
      f"ph={ph} testo={all_text(res)!r} stderr={res.get('stderr')}")
check("d10: le righe pulite restano in chiaro",
      "durata" in all_text(res), all_text(res))


# --------------------------------------------------------------------------- #
# 2. PPTX — 10 file
# --------------------------------------------------------------------------- #
print("\n== PPTX ==")

p1, prod = lo_ooxml(WORK / "p1_slide.pptx",
                    [text_img("Piano industriale 2027")])
conv = new_conv()
res = call(conv, {"p1.pptx": str(p1)}, "p1.pptx")
check(f"p1 ({prod}): immagine della slide letta",
      res["outcome"] == "ok" and len(res["images"]) >= 1
      and "industriale" in all_text(res), all_text(res))

p2 = make_ooxml(WORK / "p2_due.pptx",
                [("image1.png", text_img("budget marketing")),
                 ("image2.png", text_img("canali di vendita"))])
res = call(conv, {"p2.pptx": str(p2)}, "p2.pptx")
check("p2: 2 immagini", res["outcome"] == "ok" and len(res["images"]) == 2,
      all_text(res))

p3 = make_ooxml(WORK / "p3_gif_tiff.pptx",
                [("image1.gif", text_img("grafico vendite", ".gif")),
                 ("image2.tiff", text_img("quote di mercato", ".tiff"))])
res = call(conv, {"p3.pptx": str(p3)}, "p3.pptx")
check("p3: media gif+tiff lette",
      res["outcome"] == "ok" and len(res["images"]) == 2
      and "vendite" in all_text(res) and "mercato" in all_text(res),
      all_text(res))

p4 = make_ooxml(WORK / "p4_icone.pptx",
                [(f"i{i}.png", tiny_img()) for i in range(4)])
res = call(conv, {"p4.pptx": str(p4)}, "p4.pptx")
check("p4: icone -> nessuna leggibile", res["outcome"] == "ok"
      and not res["images"], res.get("notice"))

p5 = make_ooxml(WORK / "p5_vuoto.pptx", [])
res = call(conv, {"p5.pptx": str(p5)}, "p5.pptx")
check("p5: senza media -> notice", res["outcome"] == "ok"
      and "Nessuna immagine" in (res.get("notice") or ""), res.get("notice"))

p6 = make_ooxml(WORK / "p6_estranei.pptx",
                [("image1.png", text_img("slide vera"))],
                extra_parts=[("ppt/media/notes.txt", b"non sono un'immagine"),
                             ("ppt/other/image9.png", text_img("fuori posto")),
                             ("docProps/thumbnail.jpeg",
                              text_img("anteprima", ".jpeg"))])
res = call(conv, {"p6.pptx": str(p6)}, "p6.pptx")
check("p6: part estranee ignorate (solo ppt/media/*. immagine)",
      res["outcome"] == "ok" and len(res["images"]) == 1
      and "vera" in all_text(res)
      and "anteprima" not in all_text(res)
      and "fuori" not in all_text(res), all_text(res))

p7 = WORK / "p7_corrotto.pptx"
p7.write_bytes(b"\x00\x01\x02 non sono uno zip")
res = call(conv, {"p7.pptx": str(p7)}, "p7.pptx")
check("p7: bytes non-zip -> mai un'eccezione",
      res["outcome"] == "ok" and not res["images"],
      f"{res['outcome']}: {res.get('stderr') or res.get('notice')}")

p8 = make_ooxml(WORK / "p8_grafica.pptx", [("image1.png", shapes_img())])
res = call(conv, {"p8.pptx": str(p8)}, "p8.pptx")
check("p8: grafica pura -> ok senza testo",
      res["outcome"] == "ok"
      and (bool(res.get("notice"))
           or all("vendite" not in i.get("text", "")
                  for i in res["images"])),
      str(res.get("images") or res.get("notice")))

p9 = make_ooxml(WORK / "p9_cache.pptx", [("image1.png",
                                          text_img("nota di rilascio"))])
res = call(conv, {"p9.pptx": str(p9)}, "p9.pptx")
ok1 = res["outcome"] == "ok" and "rilascio" in all_text(res)
res = no_ocr(lambda: call(conv, {"p9.pptx": str(p9)}, "p9.pptx"))
check("p9: secondo giro dalla cache RAM (zero ri-OCR)",
      ok1 and res["outcome"] == "ok" and "rilascio" in all_text(res),
      res.get("stderr") or all_text(res))

p10 = make_ooxml(WORK / "p10_pii.pptx",
                 [("image1.png", text_img("Referente: Mario Rossi"))])
aconv = new_conv(anonymized=True)
res = call(aconv, {"p10.pptx": str(p10)}, "p10.pptx", anonymized=True)
check("p10: anon fresco su pptx -> segnaposto",
      res["outcome"] == "ok" and no_pii(all_text(res))
      and PH_RE.search(all_text(res)), all_text(res))


# --------------------------------------------------------------------------- #
# 3. XLSX — 10 file
# --------------------------------------------------------------------------- #
print("\n== XLSX ==")

x1, prod = lo_ooxml(WORK / "x1_report.xlsx", [text_img("Totale ricavi 2026")])
conv = new_conv()
res = call(conv, {"x1.xlsx": str(x1)}, "x1.xlsx")
check(f"x1 ({prod}): immagine del foglio letta",
      res["outcome"] == "ok" and len(res["images"]) >= 1
      and "ricavi" in all_text(res), all_text(res))

x2 = make_ooxml(WORK / "x2_tre.xlsx",
                [("image1.png", text_img("gennaio")),
                 ("image2.png", text_img("febbraio")),
                 ("image3.png", text_img("marzo"))])
res = call(conv, {"x2.xlsx": str(x2)}, "x2.xlsx")
check("x2: 3 immagini nell'ordine",
      res["outcome"] == "ok" and len(res["images"]) == 3
      and "gennaio" in res["images"][0]["text"]
      and "marzo" in res["images"][2]["text"], all_text(res))

x3 = make_ooxml(WORK / "x3_pii.xlsx",
                [("image1.png", text_img("Cliente: Rossi Impianti"))])
aconv = new_conv(anonymized=True)
res = call(aconv, {"x3.xlsx": str(x3)}, "x3.xlsx", anonymized=True)
check("x3: anon fresco su xlsx -> segnaposto",
      res["outcome"] == "ok" and no_pii(all_text(res))
      and PH_RE.search(all_text(res)), all_text(res))

x4 = make_ooxml(WORK / "x4_icone.xlsx", [("i1.png", tiny_img())])
res = call(conv, {"x4.xlsx": str(x4)}, "x4.xlsx")
check("x4: icone -> nessuna leggibile",
      res["outcome"] == "ok" and not res["images"], res.get("notice"))

x5 = make_ooxml(WORK / "x5_vuoto.xlsx", [])
res = call(conv, {"x5.xlsx": str(x5)}, "x5.xlsx")
check("x5: senza media -> notice", res["outcome"] == "ok"
      and "Nessuna immagine" in (res.get("notice") or ""), res.get("notice"))

x6 = WORK / "x6_corrotto.xlsx"
x6.write_bytes(bytes(range(64)))
res = call(conv, {"x6.xlsx": str(x6)}, "x6.xlsx")
check("x6: corrotto -> mai un'eccezione",
      res["outcome"] == "ok" and not res["images"],
      f"{res['outcome']}: {res.get('stderr') or res.get('notice')}")

x7 = make_ooxml(WORK / "x7_webp.xlsx",
                [("image1.webp", text_img("margine lordo", ".webp"))])
res = call(conv, {"x7.xlsx": str(x7)}, "x7.xlsx")
check("x7: media webp letta", res["outcome"] == "ok"
      and len(res["images"]) == 1 and "margine" in all_text(res),
      all_text(res))

# x8: via veloce dal FILE DI PROGETTO confermato
with db.SessionLocal() as s:
    proj = Project(owner_id=1, anonymized=1, name="Stress OCR")
    s.add(proj)
    s.commit()
    proj_id = proj.id
pconv = new_conv(anonymized=True, project_id=proj_id)
ph_p = seed_registry(proj_id)
x8 = WORK / "x8_progetto.xlsx"
make_ooxml(x8, [("image1.png", text_img("Fornitore: Mario Rossi"))])
stored = image_ocr.build_cache(image_ocr.ooxml_images(x8.read_bytes()))
with db.SessionLocal() as s:
    pf = ProjectFile(project_id=proj_id, filename="listino.xlsx",
                     model_filename="progetto_01.xlsx", confirmed=1,
                     size=x8.stat().st_size)
    s.add(pf)
    s.commit()
    cache_path = project_files.ocr_cache_path(pf)
cache_path.parent.mkdir(parents=True, exist_ok=True)
cache_path.write_text(json.dumps(stored, ensure_ascii=False),
                      encoding="utf-8")
res = no_ocr(lambda: call(pconv, {"progetto_01.xlsx": str(x8)},
                          "progetto_01.xlsx", anonymized=True,
                          scope=proj_id))
check("x8: via veloce dal file di progetto (registro del progetto)",
      res["outcome"] == "ok" and ph_p and ph_p in all_text(res)
      and no_pii(all_text(res)),
      f"ph={ph_p} testo={all_text(res)!r} stderr={res.get('stderr')}")

x9 = WORK / "x9_manca.xlsx"          # mai scritto su disco
res = call(conv, {"x9.xlsx": str(x9)}, "x9.xlsx")
check("x9: percorso mancante -> errore parlante, non crash",
      res["outcome"] == "error" and "non leggibile" in res["stderr"],
      f"{res['outcome']}: {res.get('stderr')}")

x10 = make_ooxml(WORK / "x10_jpeg_q.xlsx",
                 [("image1.jpeg", text_img("nota spese approvata", ".jpeg"))])
res = call(conv, {"x10.xlsx": str(x10)}, "x10.xlsx")
check("x10: jpeg (estensione lunga) letta",
      res["outcome"] == "ok" and "spese" in all_text(res), all_text(res))


# --------------------------------------------------------------------------- #
# 4. XLSM — 10 file
# --------------------------------------------------------------------------- #
print("\n== XLSM ==")

m1 = make_ooxml(WORK / "m1_macro.xlsm",
                [("image1.png", text_img("scadenzario fornitori"))])
conv = new_conv()
res = call(conv, {"m1.xlsm": str(m1)}, "m1.xlsm")
check("m1: xlsm letto come xlsx", res["outcome"] == "ok"
      and "scadenzario" in all_text(res), all_text(res))

m2 = make_ooxml(WORK / "m2_misto.xlsm",
                [("image1.png", text_img("primo trimestre")),
                 ("image2.jpg", text_img("secondo trimestre", ".jpg")),
                 ("image3.bmp", text_img("terzo trimestre", ".bmp"))])
res = call(conv, {"m2.xlsm": str(m2)}, "m2.xlsm")
check("m2: 3 media miste", res["outcome"] == "ok"
      and len(res["images"]) == 3, all_text(res))

m3 = make_ooxml(WORK / "m3_pii.xlsm",
                [("image1.png", text_img("Titolare: Mario Rossi"))])
aconv = new_conv(anonymized=True)
res = call(aconv, {"m3.xlsm": str(m3)}, "m3.xlsm", anonymized=True)
check("m3: anon fresco su xlsm -> segnaposto",
      res["outcome"] == "ok" and no_pii(all_text(res))
      and PH_RE.search(all_text(res)), all_text(res))

m4 = make_ooxml(WORK / "m4_icone.xlsm",
                [(f"i{i}.png", tiny_img()) for i in range(2)])
res = call(conv, {"m4.xlsm": str(m4)}, "m4.xlsm")
check("m4: icone -> nessuna leggibile",
      res["outcome"] == "ok" and not res["images"], res.get("notice"))

m5 = make_ooxml(WORK / "m5_vuoto.xlsm", [])
res = call(conv, {"m5.xlsm": str(m5)}, "m5.xlsm")
check("m5: senza media -> notice", res["outcome"] == "ok"
      and "Nessuna immagine" in (res.get("notice") or ""), res.get("notice"))

m6 = WORK / "m6_corrotto.xlsm"
m6.write_bytes(b"PK\x03\x04" + b"\xff" * 40)
res = call(conv, {"m6.xlsm": str(m6)}, "m6.xlsm")
check("m6: corrotto -> mai un'eccezione",
      res["outcome"] == "ok" and not res["images"],
      f"{res['outcome']}: {res.get('stderr') or res.get('notice')}")

m7 = make_ooxml(WORK / "m7_ventidue.xlsm",
                [(f"image{i:02d}.png", text_img(f"voce {i}", width=320,
                                                font_size=40))
                 for i in range(1, 23)])
res = call(conv, {"m7.xlsm": str(m7)}, "m7.xlsm")
check("m7: 22 immagini -> tetto a 20",
      res["outcome"] == "ok" and len(res["images"]) == 20
      and "prime 20" in (res.get("notice") or ""), res.get("notice"))

m8 = make_ooxml(WORK / "m8_grafica.xlsm", [("image1.png", shapes_img())])
res = call(conv, {"m8.xlsm": str(m8)}, "m8.xlsm")
check("m8: grafica pura -> ok", res["outcome"] == "ok",
      res.get("stderr") or "")

m9 = make_ooxml(WORK / "m9_cache.xlsm",
                [("image1.png", text_img("collaudo superato"))])
res = call(conv, {"m9.xlsm": str(m9)}, "m9.xlsm")
ok1 = res["outcome"] == "ok" and "collaudo" in all_text(res)
res = no_ocr(lambda: call(conv, {"m9.xlsm": str(m9)}, "m9.xlsm"))
check("m9: cache RAM anche per xlsm", ok1 and res["outcome"] == "ok"
      and "collaudo" in all_text(res), all_text(res))

# m10: la SCHEDA — count_images deve contare anche negli xlsm (riga
# "immagini nel file" all'upload), non solo leggere col tool
n = image_ocr.count_images(m1.read_bytes(), ".xlsm")
check("m10: count_images conta le immagini degli xlsm (riga scheda)",
      n == 1, f"contate: {n}")


# --------------------------------------------------------------------------- #
# 5. PDF — 10 file
# --------------------------------------------------------------------------- #
print("\n== PDF ==")

f1 = make_pdf(WORK / "f1_due_pagine.pdf",
              [[("text", "Pagina uno del rapporto"),
                ("img", text_img("mappa del cantiere"))],
               [("text", "Pagina due del rapporto"),
                ("img", text_img("planimetria generale"))]])
conv = new_conv()
res = call(conv, {"f1.pdf": str(f1)}, "f1.pdf")
check("f1: immagini su 2 pagine, campo page 1-based",
      res["outcome"] == "ok" and len(res["images"]) == 2
      and res["images"][0].get("page") == 1
      and res["images"][1].get("page") == 2, str(res.get("images"))[:200])
check("f1: contenuti per pagina",
      "cantiere" in res["images"][0]["text"]
      and "planimetria" in res["images"][1]["text"], all_text(res))

res = call(conv, {"f1.pdf": str(f1)}, "f1.pdf", page=2)
check("f2: filtro page=2 -> solo pagina 2",
      res["outcome"] == "ok" and len(res["images"]) == 1
      and "planimetria" in all_text(res), all_text(res))

res = call(conv, {"f1.pdf": str(f1)}, "f1.pdf", page=9)
check("f3: page inesistente -> notice parlante",
      res["outcome"] == "ok" and not res["images"]
      and "pagina 9" in (res.get("notice") or ""), res.get("notice"))

f4 = make_pdf(WORK / "f4_solo_testo.pdf",
              [[("text", "Solo testo nativo, nessuna immagine.")]])
res = call(conv, {"f4.pdf": str(f4)}, "f4.pdf")
check("f4: PDF di solo testo -> nessuna immagine, notice",
      res["outcome"] == "ok" and not res["images"]
      and "Nessuna immagine" in (res.get("notice") or ""), res.get("notice"))

f5 = scan_pdf(WORK / "f5_scansione.pdf",
              text_img(["VERBALE DI CONSEGNA", "impianto collaudato"],
                       width=1200, font_size=64))
res = call(conv, {"f5.pdf": str(f5)}, "f5.pdf")
check("f5: pagina scansionata letta",
      res["outcome"] == "ok" and len(res["images"]) == 1
      and "CONSEGNA" in all_text(res).upper(), all_text(res))

f6 = make_pdf(WORK / "f6_vettoriale.pdf", [[("rect",), ("rect",)]])
res = call(conv, {"f6.pdf": str(f6)}, "f6.pdf")
check("f6: pagina vettoriale senza testo -> ok senza crash",
      res["outcome"] == "ok",
      f"{res['outcome']}: {res.get('stderr') or res.get('notice')}")

f7 = make_pdf(WORK / "f7_tante.pdf",
              [[("img", text_img(f"voce {p * 5 + i}", width=320,
                                 font_size=40)) for i in range(5)]
               for p in range(5)])
res = call(conv, {"f7.pdf": str(f7)}, "f7.pdf")
check("f7: 25 immagini -> tetto a 20 e suggerimento `page`",
      res["outcome"] == "ok" and len(res["images"]) == 20
      and "prime 20" in (res.get("notice") or "")
      and "page" in (res.get("notice") or ""), res.get("notice"))
res = call(conv, {"f7.pdf": str(f7)}, "f7.pdf", page=4)
check("f7b: col filtro page le immagini oltre il tetto si raggiungono",
      res["outcome"] == "ok" and len(res["images"]) == 5
      and all(i.get("page") == 4 for i in res["images"]),
      str([(i.get("page"), i.get("text")) for i in res.get("images", [])]))

f8 = WORK / "f8_corrotto.pdf"
f8.write_bytes(b"%PDF-1.7\nspazzatura totale senza xref")
res = call(conv, {"f8.pdf": str(f8)}, "f8.pdf")
check("f8: PDF corrotto -> tool result d'errore, MAI un'eccezione",
      res["outcome"] == "error",
      f"{res['outcome']}: {res.get('stderr')}")

f9 = make_pdf(WORK / "f9_pii.pdf",
              [[("text", "allegato fotografico"),
                ("img", text_img("Consegnato a Mario Rossi"))]])
aconv = new_conv(anonymized=True)
res = call(aconv, {"f9.pdf": str(f9)}, "f9.pdf", anonymized=True)
check("f9: anon fresco su PDF -> segnaposto",
      res["outcome"] == "ok" and no_pii(all_text(res))
      and PH_RE.search(all_text(res)), all_text(res))

# f10: via veloce su PDF con pagine + filtro page sulla via veloce
fconv = new_conv(anonymized=True)
ph = seed_registry(fconv)
f10 = make_pdf(WORK / "f10_orig.pdf",
               [[("text", "pagina uno"),
                 ("img", text_img("Contratto con Mario Rossi"))],
                [("text", "pagina due"),
                 ("img", text_img("clausola di recesso"))]])
stored = image_ocr.pdf_images(f10.read_bytes())
stored = image_ocr.build_cache(stored)
with db.SessionLocal() as s:
    att = Attachment(conv_id=fconv, direction="in", filename="scan.pdf",
                     model_filename="f10.pdf", size=f10.stat().st_size,
                     mime="application/pdf", original_path=str(f10),
                     anonymization_status="protected",
                     protected_path=str(f10))
    s.add(att)
    s.commit()
    (f10.parent / f"{att.id}_ocr.json").write_text(
        json.dumps(stored, ensure_ascii=False), encoding="utf-8")
res = no_ocr(lambda: call(fconv, {"f10.pdf": str(f10)}, "f10.pdf",
                          anonymized=True))
check("f10: via veloce PDF -> tag del registro, pagine 1-based",
      res["outcome"] == "ok" and ph and ph in all_text(res)
      and no_pii(all_text(res))
      and res["images"] and res["images"][0].get("page") == 1,
      f"ph={ph} {str(res.get('images'))[:220]} {res.get('stderr')}")
res = no_ocr(lambda: call(fconv, {"f10.pdf": str(f10)}, "f10.pdf",
                          anonymized=True, page=2))
check("f10b: filtro page anche sulla via veloce",
      res["outcome"] == "ok" and len(res["images"]) == 1
      and "recesso" in all_text(res), all_text(res))


# --------------------------------------------------------------------------- #
# 6. IMMAGINI NUDE — 10 file
# --------------------------------------------------------------------------- #
print("\n== IMMAGINI ==")

conv = new_conv()
IMG_CASES = [
    ("i1_nota.png", ".png", "promemoria riunione"),
    ("i2_foto.jpg", ".jpg", "cartello di cantiere"),
    ("i3_scheda.jpeg", ".jpeg", "scheda tecnica motore"),
    ("i4_fax.bmp", ".bmp", "protocollo in entrata"),
    ("i5_logo.gif", ".gif", "insegna del negozio"),
    ("i6_scan.tif", ".tif", "ricevuta di ritorno"),
    ("i7_web.webp", ".webp", "banner promozionale"),
]
for fname, ext, phrase in IMG_CASES:
    p = WORK / fname
    p.write_bytes(text_img(phrase, ext))
    res = call(conv, {fname: str(p)}, fname)
    word = phrase.split()[0]
    check(f"{fname.split('_')[0]}: {ext} nudo letto",
          res["outcome"] == "ok" and len(res["images"]) == 1
          and word in all_text(res),
          res.get("stderr") or all_text(res))

i8 = WORK / "i8_icona.png"
i8.write_bytes(tiny_img())
res = call(conv, {"i8.png": str(i8)}, "i8.png")
check("i8: icona sotto MIN_SIDE -> nessuna leggibile",
      res["outcome"] == "ok" and not res["images"], res.get("notice"))

i9 = WORK / "i9_corrotta.png"
i9.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30)
res = call(conv, {"i9.png": str(i9)}, "i9.png")
check("i9: PNG corrotto -> mai un'eccezione",
      res["outcome"] == "ok" and not res["images"],
      f"{res['outcome']}: {res.get('stderr') or res.get('notice')}")

i10 = WORK / "i10_FOTO.PNG"
i10.write_bytes(text_img(["Intestatario: Mario Rossi",
                          "pratica numero 4411"]))
aconv = new_conv(anonymized=True)
res = call(aconv, {"i10_FOTO.PNG": str(i10)}, "i10_FOTO.PNG",
           anonymized=True)
check("i10: estensione MAIUSCOLA + anon -> segnaposto",
      res["outcome"] == "ok" and no_pii(all_text(res))
      and PH_RE.search(all_text(res)) and "4411" in all_text(res),
      all_text(res))


# --------------------------------------------------------------------------- #
# 7. Trasversali: argomenti sporchi, accenti, concorrenza, sfratto RAM
# --------------------------------------------------------------------------- #
print("\n== trasversali ==")

conv = new_conv()
acc = WORK / "società perizia.docx"
make_ooxml(acc, [("image1.png", text_img("perizia giurata"))])
res = call(conv, {"società perizia.docx": str(acc)}, "società perizia.docx")
check("nome con accento e spazio", res["outcome"] == "ok"
      and "perizia" in all_text(res), res.get("stderr") or all_text(res))

res = run(or_tools._read_document_images(
    conv, {"filename": "società perizia.docx", "page": "abc"},
    {"files": {"società perizia.docx": str(acc)}, "anonymized": False,
     "scope": conv}))
check("page non numerico -> ignorato senza crash",
      res["outcome"] == "ok" and len(res["images"]) == 1, res.get("stderr"))

res = call(conv, {"a.docx": "x"}, "fantasma.docx")
check("filename sconosciuto -> errore con elenco",
      res["outcome"] == "error" and "a.docx" in res["stderr"],
      res.get("stderr"))

sample = call(conv, {"società perizia.docx": str(acc)},
              "società perizia.docx")
check("contratto del risultato (chiavi standard)",
      all(k in sample for k in ("filename", "images", "outcome", "stderr",
                                "elapsed_ms")), str(sorted(sample)))

# concorrenza: 8 chiamate insieme su formati misti, cache RAM azzerata
or_tools._ocr_ram.clear()


async def _burst():
    jobs = [
        ("d3.docx", str(d3), False, conv),
        ("p2.pptx", str(p2), False, conv),
        ("x2.xlsx", str(x2), False, conv),
        ("m2.xlsm", str(m2), False, conv),
        ("f1.pdf", str(f1), False, conv),
        ("i1_nota.png", str(WORK / "i1_nota.png"), False, conv),
        ("f5.pdf", str(f5), False, conv),
        ("d1.docx", str(d1), False, conv),
    ]
    return await asyncio.gather(*[
        or_tools._read_document_images(
            c, {"filename": name},
            {"files": {name: path}, "anonymized": anon, "scope": c})
        for name, path, anon, c in jobs])


t = time.monotonic()
results = run(_burst())
check("concorrenza: 8 chiamate parallele tutte ok",
      all(r["outcome"] == "ok" for r in results),
      "; ".join(f"{r['filename']}={r['outcome']}" for r in results))
check("concorrenza: ogni file ha letto il proprio contenuto",
      "prima" in results[0]["images"][0]["text"]
      and "cantiere" in results[4]["images"][0]["text"]
      and "promemoria" in results[5]["images"][0]["text"],
      f"{time.monotonic() - t:.1f}s")

check("sfratto RAM: la cache resta sotto il tetto",
      len(or_tools._ocr_ram) <= or_tools._OCR_RAM_MAX,
      f"{len(or_tools._ocr_ram)} voci")

# dopo lo sfratto si ri-OCR-izza senza errori
or_tools._ocr_ram.clear()
res = call(conv, {"d3.docx": str(d3)}, "d3.docx")
check("dopo lo sfratto si rilegge da capo",
      res["outcome"] == "ok" and len(res["images"]) == 3)


print(f"\nTotale: {PASS} PASS, {FAIL} FAIL "
      f"({time.monotonic() - T0:.0f}s)")
sys.exit(1 if FAIL else 0)

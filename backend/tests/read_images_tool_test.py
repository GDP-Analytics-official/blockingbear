"""Tool read_document_images (OCR host-side sulle immagini dentro gli
allegati) e riga "immagini nel file" delle schede.

NIENTE OCR vero e niente modello NER: build_cache e anonymize_web_texts sono
monkeypatchati (deterministici); l'enumerazione OOXML e i filtri taglia sono
invece REALI (docx costruito a mano con zipfile + PNG Pillow).

Copre:
  - image_ocr.redacted_lines (la via veloce): piano ∩ mappa, voci locali
    UNREADABLE/SIGNATURE sempre coperte, ri-ricerca delle superfici in mappa,
    placeholder de-anonimizzato che resta in chiaro, pagina preservata;
  - handler: filename sconosciuto, chat raw (testo in chiaro + righe a bassa
    confidenza incluse), chat anonimizzata senza cache (righe illeggibili
    omesse + gamba anonymize_web_texts), via veloce dalla cache persistita,
    cache RAM (secondo giro senza ri-OCR), filtro `page` sui PDF;
  - registry: spec dichiarata, result_keys, ui_kind;
  - schede: riga "immagini nel file" in _attachment_block (e mai sui file
    immagine).

Uso:
    python backend/tests/read_images_tool_test.py
"""
import asyncio
import io
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

DATA = HERE / "data" / "test_read_images"
shutil.rmtree(DATA, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_SANDBOX_ENGINE"] = "off"

from app import chat_anonymization as chat_anon                  # noqa: E402
from app import db                                               # noqa: E402
from app.auth import seed_admin                                  # noqa: E402
from app.db import Attachment, Conversation, init_db             # noqa: E402
from app.engine import image_ocr                                 # noqa: E402
from app.openrouter import tools as or_tools                     # noqa: E402
from app.routes import chat_routes                               # noqa: E402

PASS = 0
FAIL = 0


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


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# 1. redacted_lines: la via veloce, senza OCR né DB
# --------------------------------------------------------------------------- #
print("\n== redacted_lines ==")

CACHE = {"v": 1, "images": [
    {"key": "word/media/image1.png", "ext": "png", "w": 800, "h": 600,
     "lines": [
         {"t": "Contratto con Mario Rossi", "s": 0.95, "b": [0, 0, 400, 20]},
         {"t": "scarabocchio illeggibile", "s": 0.40, "b": [0, 30, 200, 50]},
         {"t": "Riga senza entità", "s": 0.90, "b": [0, 60, 200, 80]},
     ],
     "regions": [{"b": [10, 90, 60, 120], "s": 0.5}],
     "plan": [
         {"line": 0, "s": 14, "e": 25, "ph": "[FULLNAME_1]"},
         {"line": 1, "s": 0, "e": 24, "ph": "[UNREADABLE_1]"},
         {"region": 0, "ph": "[SIGNATURE_1]"},
     ]},
    {"key": "7", "page": 1, "ext": "png", "w": 100, "h": 100,
     "lines": [{"t": "conto IT60X0542811101000000123456 attivo",
                "s": 0.92, "b": [0, 0, 90, 10]}],
     "regions": [], "plan": []},
]}

MAPPING = {"[FULLNAME_1]": "Mario Rossi",
           "[IBAN_1]": "IT60X0542811101000000123456"}

out = image_ocr.redacted_lines(CACHE, MAPPING)
check("un elemento per immagine", len(out) == 2)
check("entità del piano coperta",
      out[0]["lines"][0] == "Contratto con [FULLNAME_1]",
      out[0]["lines"][0])
check("riga illeggibile coperta anche fuori registro",
      out[0]["lines"][1] == "[UNREADABLE_1]", out[0]["lines"][1])
check("riga pulita in chiaro",
      out[0]["lines"][2] == "Riga senza entità")
check("firma/timbro in coda alle righe",
      out[0]["lines"][-1] == "[SIGNATURE_1]", str(out[0]["lines"]))
check("superficie in mappa ri-cercata (non pianificata)",
      "[IBAN_1]" in out[1]["lines"][0]
      and "0542811101" not in out[1]["lines"][0], out[1]["lines"][0])
check("pagina preservata", out[1].get("page") == 1)

# placeholder de-anonimizzato: tolto dalla mappa, il valore resta in chiaro
# (parità con l'immagine redatta, dove il box sparisce alla ri-redazione)
out2 = image_ocr.redacted_lines(CACHE, {"[IBAN_1]": MAPPING["[IBAN_1]"]})
check("ph fuori mappa resta in chiaro",
      out2[0]["lines"][0] == "Contratto con Mario Rossi",
      out2[0]["lines"][0])
check("le voci locali restano coperte comunque",
      out2[0]["lines"][1] == "[UNREADABLE_1]"
      and out2[0]["lines"][-1] == "[SIGNATURE_1]")


# --------------------------------------------------------------------------- #
# 2. il handler: setup comune (docx vero, OCR finto)
# --------------------------------------------------------------------------- #
print("\n== handler ==")


def png_bytes(w=64, h=64):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 200, 200)).save(buf, format="PNG")
    return buf.getvalue()


def make_docx(path, images=((64, 64),)):
    """Un .docx minimo con part media vere: a ooxml_images bastano quelle."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", "<w:document/>")
        for i, (w, h) in enumerate(images, 1):
            zf.writestr(f"word/media/image{i}.png", png_bytes(w, h))


def fake_build_cache(images, ctl=None):
    """Cache deterministica: una riga buona + una a bassa confidenza per
    immagine, così i due percorsi (raw e anonimizzato) si distinguono."""
    cache = {"v": 1, "images": []}
    for img in images:
        entry = {"key": img["key"], "ext": img["ext"], "w": 64, "h": 64,
                 "lines": [
                     {"t": f"testo di {img['key']} con Mario Rossi",
                      "s": 0.95, "b": [0, 0, 60, 10]},
                     {"t": "r1ga st0rp1ata", "s": 0.30, "b": [0, 20, 40, 30]},
                 ],
                 "regions": [], "plan": []}
        if "page" in img:
            entry["page"] = img["page"]
        cache["images"].append(entry)
    return cache


ANON_CALLS = []


def fake_anonymize_web_texts(scope, texts):
    ANON_CALLS.append((scope, list(texts)))
    return [t.replace("Mario Rossi", "[FULLNAME_1]") for t in texts]


image_ocr.build_cache = fake_build_cache
chat_anon.anonymize_web_texts = fake_anonymize_web_texts


def new_conv(anonymized=False):
    with db.SessionLocal() as s:
        conv = Conversation(owner_id=1, anonymized=1 if anonymized else 0)
        s.add(conv)
        s.commit()
        return conv.id


def call(conv_id, files, filename, anonymized=False, page=None, scope=None):
    args = {"filename": filename}
    if page is not None:
        args["page"] = page
    ctx = {"files": files, "anonymized": anonymized,
           "scope": scope or conv_id}
    return run(or_tools._read_document_images(conv_id, args, ctx))


# --- filename sconosciuto ---------------------------------------------------
conv = new_conv()
res = call(conv, {"noto.docx": "x"}, "ignoto.docx")
check("file sconosciuto -> errore", res["outcome"] == "error")
check("l'errore elenca i file disponibili", "noto.docx" in res["stderr"],
      res["stderr"])

# --- chat raw: OCR fresco, tutto in chiaro -----------------------------------
work = DATA / "work"
work.mkdir(parents=True, exist_ok=True)
docx = work / "contratto.docx"
make_docx(docx, images=((64, 64), (100, 50), (8, 8)))   # la terza è un'icona

conv = new_conv()
res = call(conv, {"contratto.docx": str(docx)}, "contratto.docx")
check("raw: outcome ok", res["outcome"] == "ok", res.get("stderr"))
check("raw: icona sotto MIN_SIDE esclusa", len(res["images"]) == 2,
      str(len(res.get("images", []))))
check("raw: testo in chiaro",
      "Mario Rossi" in res["images"][0]["text"], res["images"][0]["text"])
check("raw: righe a bassa confidenza incluse",
      "r1ga st0rp1ata" in res["images"][0]["text"])
check("raw: niente gamba di anonimizzazione", not ANON_CALLS)

# --- cache RAM: secondo giro senza ri-OCR -------------------------------------
def broken_build_cache(images, ctl=None):
    raise AssertionError("l'OCR non deve girare due volte sullo stesso file")


image_ocr.build_cache = broken_build_cache
res = call(conv, {"contratto.docx": str(docx)}, "contratto.docx")
check("cache RAM: secondo giro senza OCR", res["outcome"] == "ok"
      and len(res["images"]) == 2)
image_ocr.build_cache = fake_build_cache

# --- chat anonimizzata senza cache persistita ---------------------------------
conv = new_conv(anonymized=True)
docx2 = work / "riservato.docx"
make_docx(docx2, images=((64, 64),))
res = call(conv, {"riservato.docx": str(docx2)}, "riservato.docx",
           anonymized=True)
check("anon fresco: outcome ok", res["outcome"] == "ok", res.get("stderr"))
check("anon fresco: testo anonimizzato",
      "[FULLNAME_1]" in res["images"][0]["text"]
      and "Mario Rossi" not in res["images"][0]["text"],
      res["images"][0]["text"])
check("anon fresco: gamba anonymize_web_texts usata",
      len(ANON_CALLS) == 1 and ANON_CALLS[0][0] == conv)
check("anon fresco: riga a bassa confidenza omessa",
      "st0rp1ata" not in res["images"][0]["text"])
check("anon fresco: notice sulle righe omesse",
      "bassa confidenza" in (res.get("notice") or ""), res.get("notice"))

# --- errore di anonimizzazione -> tool result d'errore -------------------------
def failing_anonymize(scope, texts):
    raise chat_anon.TurnAnonymizationError("Controllo di uscita fallito.")


chat_anon.anonymize_web_texts = failing_anonymize
or_tools._ocr_ram.clear()
res = call(conv, {"riservato.docx": str(docx2)}, "riservato.docx",
           anonymized=True)
check("anon fresco: TurnAnonymizationError -> errore",
      res["outcome"] == "error"
      and "Controllo di uscita" in res["stderr"], res.get("stderr"))
chat_anon.anonymize_web_texts = fake_anonymize_web_texts

# --- via veloce: cache OCR persistita dell'allegato ----------------------------
conv = new_conv(anonymized=True)
att_dir = DATA / "chats" / conv
att_dir.mkdir(parents=True, exist_ok=True)
orig = att_dir / "orig.docx"
make_docx(orig)
with db.SessionLocal() as s:
    att = Attachment(conv_id=conv, direction="in", filename="doc.docx",
                     model_filename="doc.docx", size=orig.stat().st_size,
                     mime="application/vnd.openxmlformats-officedocument"
                          ".wordprocessingml.document",
                     original_path=str(orig),
                     anonymization_status="protected",
                     protected_path=str(orig))
    s.add(att)
    s.commit()
    att_id = att.id
(att_dir / f"{att_id}_ocr.json").write_text(
    json.dumps(CACHE, ensure_ascii=False), encoding="utf-8")

or_tools._scope_mapping = lambda scope: MAPPING   # il registro è testato altrove
ANON_CALLS.clear()
image_ocr.build_cache = broken_build_cache        # la via veloce non OCR-izza
res = call(conv, {"doc.docx": str(orig)}, "doc.docx", anonymized=True)
check("via veloce: outcome ok", res["outcome"] == "ok", res.get("stderr"))
check("via veloce: niente OCR e niente NER", not ANON_CALLS)
check("via veloce: righe coperte dal piano",
      res["images"][0]["text"].startswith("Contratto con [FULLNAME_1]"),
      res["images"][0]["text"])
check("via veloce: firma e illeggibile coperte",
      "[SIGNATURE_1]" in res["images"][0]["text"]
      and "[UNREADABLE_1]" in res["images"][0]["text"])
check("via veloce: pagina 1-based nel risultato",
      res["images"][1].get("page") == 2, str(res["images"][1]))
image_ocr.build_cache = fake_build_cache

# --- filtro page (PDF) ---------------------------------------------------------
REAL_ENUM = or_tools._enumerate_images


def fake_pdf_enum(data, ext):
    return [{"key": "10", "page": 0, "ext": "png", "data": png_bytes()},
            {"key": "11", "page": 1, "ext": "png", "data": png_bytes()}]


or_tools._enumerate_images = fake_pdf_enum
conv = new_conv()
pdf = work / "scan.pdf"
pdf.write_bytes(b"%PDF-fake")
res = call(conv, {"scan.pdf": str(pdf)}, "scan.pdf", page=2)
check("page: solo la pagina chiesta", len(res["images"]) == 1
      and res["images"][0]["page"] == 2, str(res.get("images")))
res = call(conv, {"scan.pdf": str(pdf)}, "scan.pdf", page=5)
check("page inesistente: notice parlante",
      res["outcome"] == "ok" and "pagina 5" in (res.get("notice") or ""),
      res.get("notice"))
or_tools._enumerate_images = REAL_ENUM

# --- formato non supportato -----------------------------------------------------
conv = new_conv()
txt = work / "note.txt"
txt.write_text("ciao", encoding="utf-8")
res = call(conv, {"note.txt": str(txt)}, "note.txt")
check("formato senza immagini: notice, non errore",
      res["outcome"] == "ok" and not res["images"]
      and "Formato non supportato" in (res.get("notice") or ""),
      res.get("notice"))


# --------------------------------------------------------------------------- #
# 3. registry e schede
# --------------------------------------------------------------------------- #
print("\n== registry e schede ==")

spec = or_tools.get("read_document_images")
check("spec nel registry", spec is not None)
check("ui_kind ocr", spec.ui_kind == "ocr")
check("required_args", spec.required_args == ("filename",))
check("result_keys senza dettagli interni",
      set(spec.result_keys) == {"filename", "images", "outcome", "stderr",
                                "elapsed_ms"})
check("non è un tool web (batch seriale)",
      "read_document_images" not in or_tools.WEB_TOOL_NAMES)
schema = spec.schema(False)["function"]
check("schema: nome e parametri", schema["name"] == "read_document_images"
      and "filename" in schema["parameters"]["properties"]
      and "page" in schema["parameters"]["properties"])
check("prompt_block raw senza nota segnaposto",
      "segnaposto" not in spec.prompt_block(False))
check("prompt_block anonimizzato con nota segnaposto",
      "segnaposto" in spec.prompt_block(True))

# la riga "immagini nel file" nelle schede allegato
with db.SessionLocal() as s:
    att = s.get(Attachment, att_id)
    att.n_images = 3
    block = chat_routes._attachment_block([att], set())
check("scheda: riga immagini nel file", "immagini nel file: 3" in block,
      block)
with db.SessionLocal() as s:
    att = s.get(Attachment, att_id)
    att.n_images = 0
    block = chat_routes._attachment_block([att], set())
check("scheda: niente riga senza immagini",
      "immagini nel file" not in block)
with db.SessionLocal() as s:
    att = s.get(Attachment, att_id)
    att.n_images = 1
    att.mime = "image/png"
    block = chat_routes._attachment_block([att], set())
check("scheda: niente riga sui file immagine",
      "immagini nel file" not in block)


print(f"\n{PASS} PASS, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)

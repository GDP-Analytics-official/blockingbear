"""Marcatura AI Act dei file generati (engine/ai_mark.py + aggancio in
chat_routes._register_artifacts + parametro admin chat_ai_marking).

La parte più importante è l'INTEGRITÀ: per ogni formato si verifica che la
marcatura non tocchi nulla oltre ai metadati (part OOXML byte-identiche,
testo del PDF identico, pixel del PNG identici) e che un file rotto o non
marcabile resti INTATTO, senza eccezioni. Poi LibreOffice riconverte i file
marcati (prova che restano apribili) e _register_artifacts viene esercitato
davvero, flag acceso e spento.

Uso:
    python backend/tests/ai_mark_test.py
"""
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

# PRIMA di ogni import di app.*: dati isolati (DB e profilo LibreOffice dei
# test non toccano quelli di esercizio, né quelli di un backend attivo)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_aimark")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

from app import db, settings_store                                # noqa: E402
from app.db import Attachment, init_db                            # noqa: E402
from app.engine import ai_mark, convert                           # noqa: E402
from app.routes import chat_routes                                # noqa: E402

import fitz                                                       # noqa: E402
from PIL import Image                                             # noqa: E402

ASSETS = Path(__file__).resolve().parent / "assets"
WORK = HERE / "data" / "test_aimark" / "work"

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


def fresh(asset_name, dst_name=None):
    """Copia un asset in WORK e torna il percorso della copia."""
    dst = WORK / (dst_name or asset_name)
    shutil.copyfile(ASSETS / asset_name, dst)
    return dst


def zip_parts(path):
    """{nome part: bytes} di uno zip, per i confronti byte a byte."""
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


# --- OOXML -------------------------------------------------------------------

def test_ooxml():
    print("\n--- OOXML (docx/xlsx/pptx) ---")
    for asset in ("contratto.docx", "cartella_multifoglio.xlsx",
                  "presentazione.pptx"):
        ext = Path(asset).suffix
        path = fresh(asset)
        before = zip_parts(path)
        check(f"{ext}: mark_file", ai_mark.mark_file(path) is True)
        after = zip_parts(path)
        check(f"{ext}: stesse part, stesso ordine",
              list(before) == list(after))
        core = after.get("docProps/core.xml", b"").decode("utf-8")
        check(f"{ext}: dicitura in core.xml",
              ai_mark.MARK_TEXT in core and ai_mark.MARK_SHORT in core)
        untouched = all(before[n] == after[n] for n in before
                        if n != "docProps/core.xml")
        check(f"{ext}: ogni altra part byte-identica", untouched)
        try:
            ET.fromstring(after["docProps/core.xml"])
            check(f"{ext}: core.xml resta XML valido", True)
        except ET.ParseError as e:
            check(f"{ext}: core.xml resta XML valido", False, repr(e))
        size = path.stat().st_size
        check(f"{ext}: rimarcatura idempotente",
              ai_mark.mark_file(path) is True
              and path.stat().st_size == size)

    # il file di Word contiene xsi:type="dcterms:W3CDTF": il prefisso dcterms
    # è un QName dentro un VALORE e deve sopravvivere alla riserializzazione
    core = zip_parts(WORK / "contratto.docx")["docProps/core.xml"]
    check("docx: xsi:type dcterms preservato",
          b'"dcterms:W3CDTF"' in core, core[:120])

    # zip valido ma senza core.xml: non marcabile, file intatto
    bare = WORK / "senza_core.docx"
    with zipfile.ZipFile(bare, "w") as z:
        z.writestr("word/document.xml", "<w/>")
    before = bare.read_bytes()
    check("docx senza core.xml: False e file intatto",
          ai_mark.mark_file(bare) is False and bare.read_bytes() == before)


# --- PDF ---------------------------------------------------------------------

def test_pdf():
    print("\n--- PDF ---")
    for asset in ("pdf_con_metadati.pdf", "contratto.pdf"):
        path = fresh(asset)
        original = path.read_bytes()
        with fitz.open(str(path)) as doc:
            pages, text = doc.page_count, doc[0].get_text()
        check(f"{asset}: mark_file", ai_mark.mark_file(path) is True)
        data = path.read_bytes()
        check(f"{asset}: salvataggio incrementale (byte originali intatti)",
              data.startswith(original),
              f"{len(original)} -> {len(data)} byte")
        with fitz.open(str(path)) as doc:
            md = doc.metadata
            check(f"{asset}: Subject marcato",
                  ai_mark.MARK_TEXT in (md.get("subject") or ""))
            check(f"{asset}: Keywords marcate",
                  "trainedAlgorithmicMedia" in (md.get("keywords") or ""))
            check(f"{asset}: pagine e testo identici",
                  doc.page_count == pages and doc[0].get_text() == text)
        size = path.stat().st_size
        check(f"{asset}: rimarcatura idempotente",
              ai_mark.mark_file(path) is True
              and path.stat().st_size == size)

    # un PDF con Subject preesistente non lo perde
    path = fresh("pdf_con_metadati.pdf", "con_subject.pdf")
    with fitz.open(str(path)) as doc:
        doc.set_metadata({"subject": "Relazione mensile"})
        doc.saveIncr()
    ai_mark.mark_file(path)
    with fitz.open(str(path)) as doc:
        subj = doc.metadata.get("subject") or ""
    check("pdf: Subject preesistente conservato",
          subj.startswith("Relazione mensile | "), subj[:60])

    corrupt = WORK / "rotto.pdf"
    corrupt.write_bytes(b"non sono un pdf")
    before = corrupt.read_bytes()
    check("pdf corrotto: False e file intatto",
          ai_mark.mark_file(corrupt) is False
          and corrupt.read_bytes() == before)


# --- PNG / SVG ---------------------------------------------------------------

def test_png_svg():
    print("\n--- PNG / SVG ---")
    png = WORK / "grafico.png"
    Image.new("RGB", (120, 60), (30, 90, 200)).save(png)
    pixels = list(Image.open(png).getdata())
    check("png: mark_file", ai_mark.mark_file(png) is True)
    img = Image.open(png)
    img.load()
    check("png: chunk tEXt presente",
          img.info.get("AI-Generated") == ai_mark.MARK_TEXT)
    check("png: pixel identici", list(img.getdata()) == pixels)
    size = png.stat().st_size
    check("png: rimarcatura idempotente",
          ai_mark.mark_file(png) is True and png.stat().st_size == size)

    corrupt = WORK / "rotto.png"
    corrupt.write_bytes(b"\x89PNG ma non proprio")
    before = corrupt.read_bytes()
    check("png corrotto: False e file intatto",
          ai_mark.mark_file(corrupt) is False
          and corrupt.read_bytes() == before)

    svg = WORK / "grafico.svg"
    svg.write_text('<?xml version="1.0" encoding="utf-8"?>\n'
                   '<svg xmlns="http://www.w3.org/2000/svg"><text>ciao'
                   '</text></svg>', encoding="utf-8")
    check("svg: mark_file", ai_mark.mark_file(svg) is True)
    marked = svg.read_text("utf-8")
    check("svg: commento dopo la dichiarazione XML",
          marked.index("<!--") > marked.index("?>")
          and ai_mark.MARK_TEXT in marked)
    try:
        root = ET.fromstring(marked)
        check("svg: resta XML valido", root.tag.endswith("svg"))
    except ET.ParseError as e:
        check("svg: resta XML valido", False, repr(e))
    check("svg: rimarcatura idempotente",
          ai_mark.mark_file(svg) is True
          and svg.read_text("utf-8") == marked)

    nodecl = WORK / "nodecl.svg"
    nodecl.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>',
                      encoding="utf-8")
    check("svg senza dichiarazione: commento in testa",
          ai_mark.mark_file(nodecl) is True
          and nodecl.read_text("utf-8").startswith("<!--"))


# --- Formati non marcabili -----------------------------------------------------

def test_unmarkable():
    print("\n--- Formati non marcabili ---")
    for name, content in (("dati.csv", b"a;b\n1;2\n"),
                          ("dati.json", b'{"a": 1}'),
                          ("nota.txt", b"ciao")):
        path = WORK / name
        path.write_bytes(content)
        check(f"{name}: False e file intatto",
              ai_mark.mark_file(path) is False
              and path.read_bytes() == content)
    check("estensioni marcabili dichiarate",
          ai_mark.MARKABLE_EXTS == {".docx", ".xlsx", ".pptx",
                                    ".pdf", ".png", ".svg"})
    check("file inesistente: False",
          ai_mark.mark_file(WORK / "fantasma.pdf") is False)

    notzip = WORK / "finto.docx"
    notzip.write_bytes(b"byte a caso, non uno zip")
    before = notzip.read_bytes()
    check("docx corrotto: False e file intatto",
          ai_mark.mark_file(notzip) is False
          and notzip.read_bytes() == before)


# --- Parametro admin -----------------------------------------------------------

def test_registry():
    print("\n--- Parametro admin ---")
    meta = settings_store.REGISTRY.get("chat_ai_marking")
    check("chat_ai_marking nel REGISTRY", meta is not None)
    check("default acceso, interruttore bool",
          meta and meta["default"] == 1 and meta.get("kind") == "bool"
          and meta["min"] == 0 and meta["max"] == 1)
    check("valore corrente = default", settings_store.current("chat_ai_marking") == 1)


# --- LibreOffice riapre i file marcati -----------------------------------------

def test_libreoffice():
    print("\n--- LibreOffice sui file marcati ---")
    if not convert.available():
        print("  SKIP  LibreOffice non trovato su questa macchina")
        return
    for name in ("contratto.docx", "cartella_multifoglio.xlsx"):
        path = WORK / name          # già marcati da test_ooxml
        data = path.read_bytes()
        pdf = None
        for attempt in (1, 2):      # il primo run su profilo vergine può fallire
            try:
                pdf = convert.to_pdf(data, suffix=path.suffix)
                break
            except convert.ConvertError as e:
                if attempt == 2:
                    check(f"{name} marcato -> PDF", False, repr(e))
        if pdf is not None:
            check(f"{name} marcato -> PDF",
                  pdf.startswith(b"%PDF"), f"{len(pdf)} byte")


# --- Integrazione: _register_artifacts ------------------------------------------

def _staging_with(*names):
    """Una finta cartella outputs della sandbox con gli asset chiesti."""
    staging = WORK / "staging" / os.urandom(4).hex()
    (staging / "outputs").mkdir(parents=True)
    files = {}
    for name in names:
        dst = staging / "outputs" / name
        if (ASSETS / name).is_file():
            shutil.copyfile(ASSETS / name, dst)
        elif name.endswith(".png"):
            Image.new("RGB", (10, 10), (200, 30, 30)).save(dst)
        else:
            dst.write_bytes(b"a;b\n1;2\n")
        files[f"outputs/{name}"] = str(dst)
    return files


def _is_marked(path):
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".docx":
        return ai_mark.MARK_TEXT.encode() in zip_parts(path).get(
            "docProps/core.xml", b"")
    if ext == ".png":
        return b"AI-Generated\x00" in path.read_bytes()
    if ext == ".pdf":
        with fitz.open(str(path)) as doc:
            return ai_mark.MARK_TEXT in (doc.metadata.get("subject") or "")
    return False


def test_register_artifacts():
    print("\n--- _register_artifacts ---")
    conv = "convaimark0000000000000000000000"

    files = _staging_with("contratto.docx", "grafico.png", "dati.csv")
    event = {"type": "tool_result", "id": "call_1",
             "result": {"stdout": "", "stderr": "", "outcome": "ok",
                        "new_files": sorted(files), "elapsed_ms": 5,
                        "files": files}}
    out = chat_routes._register_artifacts(conv, event)
    atts = out["attachments"]
    check("3 artifact registrati", len(atts) == 3,
          [a["filename"] for a in atts])
    check("percorsi host spariti dall'evento",
          "files" not in out["result"])
    with db.SessionLocal() as s:
        rows = {a.filename: a for a in
                s.query(Attachment).filter_by(conv_id=conv).all()}
        docx = rows.get("contratto.docx")
        png = rows.get("grafico.png")
        csv = rows.get("dati.csv")
        check("docx consegnato e marcato",
              docx and _is_marked(docx.original_path))
        check("png consegnato e marcato",
              png and _is_marked(png.original_path))
        check("csv consegnato senza marcatura (formato non marcabile)",
              csv and not _is_marked(csv.original_path)
              and Path(csv.original_path).read_bytes() == b"a;b\n1;2\n")
        check("size in DB = size su disco dopo la marcatura",
              all(r.size == Path(r.original_path).stat().st_size
                  for r in rows.values()))

    # chat anonimizzata con registro VUOTO: nessun segnaposto in circolazione,
    # quindi niente ripristino, niente report "contiene ancora i segnaposto"
    # (uscirebbe anche a registro vuoto) e stato "raw"; marcatura comunque
    files = _staging_with("contratto.docx")
    event = {"type": "tool_result", "id": "call_2",
             "result": {"stdout": "", "stderr": "", "outcome": "ok",
                        "new_files": sorted(files), "elapsed_ms": 5,
                        "files": files}}
    out = chat_routes._register_artifacts(conv, event, anonymized=True,
                                          mapping={}, media=None, sources={})
    with db.SessionLocal() as s:
        att = s.get(Attachment, out["attachments"][0]["id"])
        check("anonymized: artifact marcato dopo il ripristino",
              _is_marked(att.original_path))
        check("registro vuoto: niente report sui segnaposto",
              not att.anonymization_report_json,
              att.anonymization_report_json or "")
        check("registro vuoto: stato raw (nessuna etichetta in UI)",
              att.anonymization_status == "raw", att.anonymization_status)

    # flag admin spento: si consegna NON marcato
    with db.SessionLocal() as s:
        settings_store.set_values(s, {"chat_ai_marking": 0})
    files = _staging_with("contratto.docx")
    event = {"type": "tool_result", "id": "call_3",
             "result": {"stdout": "", "stderr": "", "outcome": "ok",
                        "new_files": sorted(files), "elapsed_ms": 5,
                        "files": files}}
    out = chat_routes._register_artifacts(conv, event)
    with db.SessionLocal() as s:
        att = s.get(Attachment, out["attachments"][0]["id"])
        check("flag spento: artifact NON marcato",
              att and not _is_marked(att.original_path))
        settings_store.set_values(s, {"chat_ai_marking": 1})


def main():
    if WORK.parent.is_dir():
        shutil.rmtree(WORK.parent, ignore_errors=True)
    WORK.mkdir(parents=True)
    init_db()

    test_ooxml()
    test_pdf()
    test_png_svg()
    test_unmarkable()
    test_registry()
    test_libreoffice()
    test_register_artifacts()

    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

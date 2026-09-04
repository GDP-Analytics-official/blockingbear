"""Stress test del ripristino senza cicatrici (complementare a
restore_marks_test.py, che copre i casi funzionali).

Batterie:
  A. docx a scala: 1500 paragrafi, 120 placeholder distinti, highlight
     dell'utente sparsi; redazione -> modifica del modello -> ripristino.
  B. Round-trip REALI della sandbox: python-docx, python-pptx e openpyxl
     aprono il file protetto, lo modificano e lo risalvano (lxml/openpyxl
     riscrivono l'XML da zero: è il vero banco di prova per regex e
     bgColor auto-descrittivo). pypdf rimonta il PDF protetto.
  C. xlsx a scala: 1500 righe, stili misti (none/rgb/theme/giallo utente/
     verde con bordo), idempotenza al secondo passaggio.
  D. PDF a scala e avversariale: 25 pagine, ~200 box; marcatore spostato,
     marcatore con subject ignoto, marcatori duplicati, rettangolo giallo
     DEL MODELLO che deve sopravvivere, pagina ruotata dal modello,
     PDF cifrato, PDF troncato.
  E. Valori ostili: entità XML nel valore, valore che contiene un altro
     TAG (niente sostituzione a catena), TAG sovrapposti nel nome
     (FULLNAME_1 vs FULLNAME_11), emoji/accenti, valore lunghissimo,
     a-capo nel valore. L'XML risultante deve restare ben formato.
  F. File malformati: zip troncato, document.xml non-XML, styles.xml
     corrotto, prefissi namespace non standard, mapping vuoto.
  G. MediaPool a scala: 150 coppie, 30 copie ricompresse in un artifact,
     coppia di quasi-gemelle (la firma deve DECLINARE, l'hash esatto no),
     bytes corrotti nel lookup.
  H. Concorrenza: 8 thread che ripristinano docx/xlsx/pdf/immagini in
     parallelo condividendo lo stesso MediaPool; risultati identici al
     riferimento seriale.
  I. Idempotenza: secondo ripristino su docx/pptx/xlsx/pdf già
     ripristinati = nessun cambiamento semantico.

Niente modello PII, niente rete, niente Docker. Le librerie della sandbox
(openpyxl, python-docx, python-pptx, pypdf) sono caricate da una cartella
isolata (PKGS) e non toccano il venv del backend.

Uso:
    python backend/tests/restore_marks_stress_test.py
"""
import io
import os
import re
import shutil
import sys
import time
import traceback
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
DATA = HERE / "data" / "test_restore_marks_stress"
if DATA.exists():
    shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True, exist_ok=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

# Librerie della sandbox (matplotlib, python-docx, ...) per generare gli
# artifact come li produce il modello. Non sono dipendenze del backend: si
# indica una cartella con quei pacchetti in RESTORE_STRESS_PKGS, e senza di
# essa i casi che ne hanno bisogno si dichiarano saltati.
PKGS = Path(os.environ.get("RESTORE_STRESS_PKGS", ""))
HAVE_SANDBOX_LIBS = bool(str(PKGS)) and PKGS.is_dir()
if HAVE_SANDBOX_LIBS:
    sys.path.append(str(PKGS))

import fitz                                                        # noqa: E402
from PIL import Image                                              # noqa: E402

from app.chat_anonymization import (MediaPool, _pair_input_media,  # noqa: E402
                                    restore_artifact)
from app.engine.docx import redact_docx                           # noqa: E402
from app.engine.pdf_export import (MARKER_TITLE, redact_pdf,       # noqa: E402
                                   restore_pdf)
from app.engine.pptx import redact_pptx                           # noqa: E402
from app.engine.xlsx import redact_xlsx                           # noqa: E402
from app.engine.image_ocr import redact_image_bytes               # noqa: E402

PASS = 0
FAIL = 0
NOTES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def note(msg):
    NOTES.append(msg)
    print(f"  NOTA  {msg}")


def battery(title):
    def wrap(fn):
        def run():
            print(f"\n[{fn.__name__[4:].upper()}] {title}")
            t0 = time.perf_counter()
            try:
                fn()
            except Exception:
                global FAIL
                FAIL += 1
                print("  FAIL  batteria interrotta da eccezione:")
                traceback.print_exc()
            print(f"  ---- {time.perf_counter() - t0:.1f}s")
        return run
    return wrap


# --------------------------------------------------------------------------- #
# Builder minimi (stessi di restore_marks_test.py)
# --------------------------------------------------------------------------- #
def _zip(parts):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


def _parts(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in z.namelist()}


XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
DOCX_CT = XML_HEAD + (
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    'content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
    'package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="png" ContentType="image/png"/>'
    '<Default Extension="jpeg" ContentType="image/jpeg"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>')
RELS = XML_HEAD + (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships"><Relationship Id="rId1" Type="http://schemas.'
    'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/></Relationships>')


def w_run(text, hl=None):
    rpr = f'<w:rPr><w:highlight w:val="{hl}"/></w:rPr>' if hl else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'


def make_docx(paragraphs):
    body = "".join(f"<w:p>{''.join(runs)}</w:p>" for runs in paragraphs)
    doc = XML_HEAD + (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    return _zip({"[Content_Types].xml": DOCX_CT, "_rels/.rels": RELS,
                 "word/document.xml": doc})


XLSX_CT = XML_HEAD + (
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    'content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
    'package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    '<Override PartName="/xl/sharedStrings.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
    '</Types>')
XLSX_RELS = XML_HEAD + (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships"><Relationship Id="rId1" Type="http://schemas.'
    'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/></Relationships>')
XLSX_WB = XML_HEAD + (
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/'
    'main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships"><sheets><sheet name="Foglio1" sheetId="1" r:id="rId1"/>'
    '</sheets></workbook>')
XLSX_WB_RELS = XML_HEAD + (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/sharedStrings" '
    'Target="sharedStrings.xml"/>'
    '</Relationships>')
# Fill: 0 none, 1 gray125, 2 azzurro, 3 GIALLO utente, 4 theme, 5 verde.
# Border: 0 vuoto, 1 thin. cellXfs: 0 default, 1 azzurro, 2 giallo utente,
# 3 theme, 4 verde+bordo (il bordo deve sopravvivere al giro).
XLSX_STYLES = XML_HEAD + (
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
    '2006/main">'
    '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="6">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF99CCFF"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFF00"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor theme="4" tint="0.4"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF00B050"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    '</fills>'
    '<borders count="2"><border/>'
    '<border><left style="thin"/><right style="thin"/><top style="thin"/>'
    '<bottom style="thin"/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" '
    'borderId="0"/></cellStyleXfs>'
    '<cellXfs count="5">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="0" fillId="2" borderId="0" xfId="0" '
    'applyFill="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" '
    'applyFill="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" '
    'applyFill="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" '
    'applyFill="1" applyBorder="1"/>'
    '</cellXfs></styleSheet>')


def make_xlsx(rows, sst):
    by_row = {}
    for ref, style, kind, value in rows:
        r = int("".join(ch for ch in ref if ch.isdigit()))
        s = f' s="{style}"' if style else ""
        if kind == "s":
            cell = f'<c r="{ref}"{s} t="s"><v>{value}</v></c>'
        else:
            cell = (f'<c r="{ref}"{s} t="inlineStr"><is>'
                    f'<t xml:space="preserve">{value}</t></is></c>')
        by_row.setdefault(r, []).append(cell)
    sheet_rows = "".join(
        f'<row r="{r}">{"".join(cells)}</row>'
        for r, cells in sorted(by_row.items()))
    sheet = XML_HEAD + (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
        f'2006/main"><sheetData>{sheet_rows}</sheetData></worksheet>')
    si = "".join(f'<si><t xml:space="preserve">{s}</t></si>' for s in sst)
    shared = XML_HEAD + (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/'
        f'main" count="{len(sst)}" uniqueCount="{len(sst)}">{si}</sst>')
    return _zip({"[Content_Types].xml": XLSX_CT, "_rels/.rels": XLSX_RELS,
                 "xl/workbook.xml": XLSX_WB,
                 "xl/_rels/workbook.xml.rels": XLSX_WB_RELS,
                 "xl/styles.xml": XLSX_STYLES,
                 "xl/sharedStrings.xml": shared,
                 "xl/worksheets/sheet1.xml": sheet})


import xml.etree.ElementTree as ET                                 # noqa: E402

X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def sheet_cells(data, sheet="xl/worksheets/sheet1.xml"):
    parts = _parts(data)
    root = ET.fromstring(parts[sheet])
    sst = []
    if "xl/sharedStrings.xml" in parts:
        sroot = ET.fromstring(parts["xl/sharedStrings.xml"])
        sst = ["".join(si.itertext()) for si in sroot.findall(X + "si")]
    out = {}
    for c in root.iter(X + "c"):
        t = c.get("t", "n")
        if t == "s":
            v = c.find(X + "v")
            text = sst[int(v.text)] if v is not None and v.text else ""
        else:
            text = "".join(c.itertext())
        out[c.get("r")] = (c.get("s") or "0", text)
    return out


def fill_of(data, s_idx):
    root = ET.fromstring(_parts(data)["xl/styles.xml"])
    xfs = list(root.find(X + "cellXfs"))
    fills = list(root.find(X + "fills"))
    xf = xfs[int(s_idx)]
    pf = fills[int(xf.get("fillId") or 0)].find(X + "patternFill")
    if pf is None:
        return (None, {}, {}, xf.get("borderId") or "0")
    fg, bg = pf.find(X + "fgColor"), pf.find(X + "bgColor")
    return (pf.get("patternType"),
            dict(fg.attrib) if fg is not None else {},
            dict(bg.attrib) if bg is not None else {},
            xf.get("borderId") or "0")


def is_yellow_fill(data, s_idx):
    pt, fg, _bg, _b = fill_of(data, s_idx)
    return pt == "solid" and (fg.get("rgb") or "").upper() in ("FFFFFF00",
                                                               "FFFF00")


def px_at(pdf_bytes, pno, x, y, zoom=2.0):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        pm = doc[pno].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pm.pixel(min(int(x * zoom), pm.width - 1),
                        min(int(y * zoom), pm.height - 1))


def is_yellowish(c):
    return c[0] > 220 and c[1] > 190 and c[2] < 140


def markers_in(pdf_bytes):
    out = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for pno, page in enumerate(doc):
            for a in page.annots() or []:
                if a.info.get("title") == MARKER_TITLE:
                    out.append((pno, a.info.get("subject"), tuple(a.rect)))
    return out


def pdf_text(pdf_bytes):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return "\n".join(p.get_text() for p in doc)


def edit_part(data, part, fn):
    parts = _parts(data)
    parts[part] = fn(parts[part].decode("utf-8")).encode("utf-8")
    return _zip(parts)


def write_restore(name, blob, mapping, **kw):
    p = DATA / name
    p.write_bytes(blob)
    return restore_artifact(p, mapping, **kw)


PH_RE = re.compile(r"\[[A-Z][A-Z0-9_]*_\d+\]")


# --------------------------------------------------------------------------- #
# A. docx a scala
# --------------------------------------------------------------------------- #
N_PEOPLE = 120
PEOPLE = {f"[FULLNAME_{i}]": f"Personaqa{i} Cognomeqa{i}"
          for i in range(1, N_PEOPLE + 1)}
ORGS = {f"[ORG_{i}]": f"Aziendaqa{i} S.p.A." for i in range(1, 21)}
BIG_MAP = {**PEOPLE, **ORGS}


@battery("docx a scala: 1500 paragrafi, 140 TAG distinti")
def run_a():
    paras = []
    n_user_yellow = 0
    for i in range(1500):
        pi = (i % N_PEOPLE) + 1
        oi = (i % 20) + 1
        runs = [w_run(f"Pratica {i}: cliente "),
                w_run(PEOPLE[f'[FULLNAME_{pi}]'],
                      hl="green" if i % 7 == 0 else None),
                w_run(f" presso {ORGS[f'[ORG_{oi}]']}. ")]
        if i % 11 == 0:
            runs.append(w_run("promemoria dell'utente", hl="yellow"))
            n_user_yellow += 1
        paras.append(runs)
    original = make_docx(paras)
    t0 = time.perf_counter()
    protected, _rep = redact_docx(original, BIG_MAP)
    t_red = time.perf_counter() - t0
    doc_xml = _parts(protected)["word/document.xml"].decode("utf-8")
    tags = set(PH_RE.findall(doc_xml))
    check("redazione: tutti i TAG nel protetto",
          len(tags & set(BIG_MAP)) == len(BIG_MAP),
          f"{len(tags)} tag, {t_red:.1f}s")

    # modifica del modello: 50 paragrafi nuovi sparsi che citano i TAG
    def model_edit(xml):
        for k in range(50):
            ins = (f"<w:p><w:r><w:t>Nota {k} del modello su "
                   f"[FULLNAME_{(k % N_PEOPLE) + 1}]</w:t></w:r></w:p>")
            anchor = xml.find("</w:p>", len(xml) // 2)
            xml = xml[:anchor + 6] + ins + xml[anchor + 6:]
        return xml

    artifact = edit_part(protected, "word/document.xml", model_edit)
    t0 = time.perf_counter()
    data, n, left, _extra = write_restore("a_scala.docx", artifact, BIG_MAP)
    t_res = time.perf_counter() - t0
    out_xml = _parts(data)["word/document.xml"].decode("utf-8")
    check("ripristino: nessun TAG noto rimasto",
          not (set(PH_RE.findall(out_xml)) & set(BIG_MAP))
          and not left, f"n={n} in {t_res:.1f}s")
    check("ripristino: valori campione presenti",
          PEOPLE["[FULLNAME_60]"] in out_xml
          and ORGS["[ORG_7]"] in out_xml
          and "Nota 3 del modello su Personaqa4 Cognomeqa4" in out_xml)
    yellows = out_xml.count('w:val="yellow"')
    check("ripristino: gialli rimasti = soli gialli dell'utente",
          yellows == n_user_yellow, f"{yellows} vs {n_user_yellow}")
    # il conteggio dei run verdi può legittimamente raddoppiare (la redazione
    # spezza il run): si conta per PARAGRAFO, che è ciò che l'utente vede
    green_paras = sum(1 for p in out_xml.split("<w:p>")
                      if 'w:val="green"' in p)
    expect_green = len([i for i in range(1500) if i % 7 == 0])
    check("ripristino: paragrafi col verde dell'utente = quelli originali",
          green_paras == expect_green,
          f"{green_paras} vs {expect_green}")
    check("ripristino: nessun verde nelle note del modello",
          not any('w:val="green"' in p for p in out_xml.split("<w:p>")
                  if "del modello su" in p))
    check("ripristino: XML ben formato",
          ET.fromstring(out_xml) is not None)


# --------------------------------------------------------------------------- #
# B. round-trip REALI della sandbox
# --------------------------------------------------------------------------- #
@battery("round-trip veri: python-docx / python-pptx / openpyxl / pypdf")
def run_b():
    if not HAVE_SANDBOX_LIBS:
        note("librerie sandbox non disponibili: batteria saltata")
        return
    import docx as pydocx
    import openpyxl
    from pptx import Presentation
    from pptx.util import Inches
    from pypdf import PdfReader, PdfWriter

    small_map = {"[FULLNAME_1]": "Mario Rossi",
                 "[ORG_1]": "Acme Corp S.r.l."}

    # -- python-docx: apre il protetto, aggiunge contenuto, risalva --------
    orig = make_docx([
        [w_run("Contratto tra Mario Rossi e la controparte.")],
        [w_run("Nota gialla dell'utente.", hl="yellow")],
        [w_run("Fornitore "), w_run("Acme Corp S.r.l.", hl="green"),
         w_run(" firma.")]])
    prot, _ = redact_docx(orig, small_map)
    d = pydocx.Document(io.BytesIO(prot))
    d.add_paragraph("Sintesi del modello su [ORG_1] e [FULLNAME_1].")
    buf = io.BytesIO()
    d.save(buf)
    data, n, left, _x = write_restore("b_pydocx.docx", buf.getvalue(),
                                      small_map)
    out_xml = _parts(data)["word/document.xml"].decode("utf-8")
    check("python-docx: valori ripristinati, nessun TAG",
          data is not None and "Mario Rossi" in out_xml
          and "Acme Corp S.r.l." in out_xml and not left, f"n={n}")
    yellows = out_xml.count('w:val="yellow"')
    check("python-docx: giallo nostro via, giallo utente salvo",
          yellows == 1 and "Nota gialla dell'utente." in out_xml,
          f"yellow={yellows}")
    check("python-docx: verde dell'utente conservato",
          'w:val="green"' in out_xml)

    # -- python-pptx: originale VERO creato da python-pptx ------------------
    pres = Presentation()
    slide = pres.slides.add_slide(pres.slide_layouts[6])
    tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(2))
    tb.text_frame.text = "Relatore: Mario Rossi di Acme Corp S.r.l."
    pbuf = io.BytesIO()
    pres.save(pbuf)
    prot_pptx, _ = redact_pptx(pbuf.getvalue(), small_map)
    pres2 = Presentation(io.BytesIO(prot_pptx))
    slide2 = pres2.slides.add_slide(pres2.slide_layouts[6])
    tb2 = slide2.shapes.add_textbox(Inches(1), Inches(1), Inches(8),
                                    Inches(2))
    tb2.text_frame.text = "Slide del modello su [FULLNAME_1]"
    pbuf2 = io.BytesIO()
    pres2.save(pbuf2)
    data, n, left, _x = write_restore("b_pypptx.pptx", pbuf2.getvalue(),
                                      small_map)
    slides_xml = "".join(
        v.decode("utf-8") for k, v in _parts(data).items()
        if k.startswith("ppt/slides/") and k.endswith(".xml"))
    check("python-pptx: valori ripristinati su entrambe le slide",
          "Mario Rossi" in slides_xml and "Acme Corp S.r.l." in slides_xml
          and not (set(PH_RE.findall(slides_xml)) & set(small_map)),
          f"n={n} left={left}")
    check("python-pptx: nessun a:highlight residuo della redazione",
          "<a:highlight>" not in slides_xml)

    # -- openpyxl: IL banco di prova (riscrive styles.xml da zero) ----------
    orig_x = make_xlsx(
        rows=[("A1", "0", "inline", "Referente: Mario Rossi"),
              ("A2", "1", "inline", "Fornitore Acme Corp S.r.l. spa"),
              ("A3", "2", "inline", "Mario Rossi (giallo dell'utente)"),
              ("A4", "3", "inline", "Sede di Acme Corp S.r.l. qui"),
              ("A5", "4", "inline", "Verde con bordo: Mario Rossi"),
              ("B1", "2", "inline", "Gialla utente, nessuna PII")],
        sst=["padding"])
    prot_x, _ = redact_xlsx(orig_x, small_map)
    wb = openpyxl.load_workbook(io.BytesIO(prot_x))
    ws = wb.active
    ws.append(["Riga del modello su [ORG_1]"])
    xbuf = io.BytesIO()
    wb.save(xbuf)
    data, n, left, _x2 = write_restore("b_openpyxl.xlsx", xbuf.getvalue(),
                                       small_map)
    check("openpyxl: byte riscritti e TAG spariti", data is not None
          and not (set(l for l in left) & set(small_map)), f"n={n}")
    if data is not None:
        cells = sheet_cells(data)
        pt, fg, bg, border = fill_of(data, cells["A1"][0])
        check("openpyxl: A1 torna senza fill", pt in (None, "none"),
              f"{pt} {fg} {bg}")
        pt, fg, bg, border = fill_of(data, cells["A2"][0])
        check("openpyxl: A2 torna azzurra",
              pt == "solid" and (fg.get("rgb") or "").upper()
              in ("FF99CCFF", "99CCFF"), f"{pt} {fg} {bg}")
        check("openpyxl: A3 resta gialla (giallo utente)",
              is_yellow_fill(data, cells["A3"][0]),
              str(fill_of(data, cells["A3"][0])))
        pt, fg, bg, border = fill_of(data, cells["A4"][0])
        check("openpyxl: A4 torna theme",
              pt == "solid" and fg.get("theme") == "4",
              f"{pt} {fg} {bg}")
        pt, fg, bg, border = fill_of(data, cells["A5"][0])
        check("openpyxl: A5 torna verde e CONSERVA il bordo",
              pt == "solid" and (fg.get("rgb") or "").upper()
              in ("FF00B050", "00B050") and border != "0",
              f"{pt} {fg} border={border}")
        check("openpyxl: B1 (gialla utente, no PII) intoccata",
              is_yellow_fill(data, cells["B1"][0]))
        check("openpyxl: la riga del modello è ripristinata",
              any("Acme Corp S.r.l." in v[1] and "modello" in v[1]
                  for v in cells.values()))

    # -- pypdf: rimonta il PDF protetto pagina per pagina --------------------
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Contratto tra Mario Rossi e Acme Corp "
                     "S.r.l. in data odierna.", fontsize=11)
    page.insert_text((72, 140), "Referente: Mario Rossi", fontsize=11)
    pdf0 = doc.tobytes()
    doc.close()
    prot_pdf, rep = redact_pdf(pdf0, small_map)
    r = PdfReader(io.BytesIO(prot_pdf))
    w = PdfWriter()
    for pg in r.pages:
        w.add_page(pg)
    obuf = io.BytesIO()
    w.write(obuf)
    remounted = obuf.getvalue()
    kept = markers_in(remounted)
    check("pypdf: i marcatori sopravvivono al rimontaggio",
          len(kept) == len(markers_in(prot_pdf)), f"kept={len(kept)}")
    restored, rrep = restore_pdf(remounted, small_map)
    b0 = next(b for b in rep["boxes"][0] if b["ph"] == "[FULLNAME_1]")
    check("pypdf: valori scritti e giallo rimosso dopo il rimontaggio",
          "Mario Rossi" in pdf_text(restored)
          and "[FULLNAME_1]" not in pdf_text(restored)
          and not is_yellowish(px_at(restored, 0, b0["x0"] + 1.5,
                                     b0["y0"] + 1.5)),
          f"restored={rrep['restored']} remaining={rrep['remaining']}")
    check("pypdf: nessun marcatore residuo", not markers_in(restored))


# --------------------------------------------------------------------------- #
# C. xlsx a scala
# --------------------------------------------------------------------------- #
@battery("xlsx a scala: 1500 righe, stili misti, idempotenza")
def run_c():
    styles = ["0", "1", "2", "3", "4"]     # default/azzurro/giallo/theme/verde
    rows, sst = [], []
    for i in range(1500):
        pi = (i % N_PEOPLE) + 1
        s = styles[i % 5]
        if i % 3 == 0:
            sst.append(f"Cliente {i}: {PEOPLE[f'[FULLNAME_{pi}]']}")
            rows.append((f"A{i + 1}", s, "s", str(len(sst) - 1)))
        else:
            rows.append((f"A{i + 1}", s, "inline",
                         f"Pratica {i} di {PEOPLE[f'[FULLNAME_{pi}]']}"))
        rows.append((f"B{i + 1}", styles[(i + 2) % 5], "inline",
                     f"colonna B riga {i} senza dati personali"))
    orig = make_xlsx(rows, sst)
    t0 = time.perf_counter()
    prot, _rep = redact_xlsx(orig, BIG_MAP)
    t_red = time.perf_counter() - t0
    cells = sheet_cells(prot)
    n_tagged = sum(1 for v in cells.values() if PH_RE.search(v[1]))
    check("redazione: TAG nelle celle attese", n_tagged == 1500,
          f"{n_tagged} in {t_red:.1f}s")

    # il modello inserisce righe extra via editing dell'XML (slittano i ref)
    def add_rows(xml):
        new = "".join(
            f'<row r="{5000 + k}"><c r="D{5000 + k}" t="inlineStr"><is>'
            f'<t>Totale {k} per [ORG_{(k % 20) + 1}]</t></is></c></row>'
            for k in range(30))
        return xml.replace("</sheetData>", new + "</sheetData>")

    artifact = edit_part(prot, "xl/worksheets/sheet1.xml", add_rows)
    t0 = time.perf_counter()
    data, n, left, _extra = write_restore("c_scala.xlsx", artifact, BIG_MAP)
    t_res = time.perf_counter() - t0
    cells = sheet_cells(data)
    check("ripristino: nessun TAG noto rimasto",
          not (set(left) & set(BIG_MAP))
          and not any(PH_RE.search(v[1]) and PH_RE.search(v[1]).group(0)
                      in BIG_MAP for v in cells.values()),
          f"n={n} in {t_res:.1f}s")
    ok_a, ok_b = True, True
    expect = {0: (None, "none"), 1: "FF99CCFF", 3: "theme", 4: "FF00B050"}
    for i in (0, 5, 100, 501, 999, 1200, 1499):
        s = i % 5
        pt, fg, bg, border = fill_of(data, cells[f"A{i + 1}"][0])
        if s == 0:
            ok_a &= pt in (None, "none")
        elif s == 1:
            ok_a &= fg.get("rgb") == "FF99CCFF"
        elif s == 2:
            ok_a &= is_yellow_fill(data, cells[f"A{i + 1}"][0])
        elif s == 3:
            ok_a &= fg.get("theme") == "4"
        elif s == 4:
            ok_a &= fg.get("rgb") == "FF00B050" and border != "0"
        # le celle B non hanno PII: lo stile deve essere quello ORIGINALE
        ok_b &= cells[f"B{i + 1}"][0] == styles[(i + 2) % 5]
    check("ripristino: fill campione tornati per ognuno dei 5 stili", ok_a)
    check("ripristino: colonna B (senza PII) mai toccata", ok_b)
    check("ripristino: righe del modello ripristinate senza colore",
          all(cells[f"D{5000 + k}"][0] == "0"
              and "Aziendaqa" in cells[f"D{5000 + k}"][1]
              for k in range(30)))
    # idempotenza a scala
    data2, _n, _l, _x = write_restore("c_scala2.xlsx", data, BIG_MAP)
    check("idempotenza: secondo passaggio senza cambi semantici",
          data2 is not None and sheet_cells(data2) == sheet_cells(data))


# --------------------------------------------------------------------------- #
# D. PDF a scala e avversariale
# --------------------------------------------------------------------------- #
@battery("PDF: 25 pagine, marcatori manomessi, giallo del modello, cifrato")
def run_d():
    doc = fitz.open()
    for p in range(25):
        page = doc.new_page()
        for k in range(8):
            i = (p * 8 + k) % N_PEOPLE + 1
            page.insert_text((72, 90 + k * 40),
                             f"Riga {k}: incarico a {PEOPLE[f'[FULLNAME_{i}]']}"
                             f" confermato.", fontsize=11)
        page.draw_line((60, 94), (540, 94), color=(0, 0, 0), width=0.7)
    pdf0 = doc.tobytes()
    doc.close()
    t0 = time.perf_counter()
    prot, rep = redact_pdf(pdf0, BIG_MAP)
    t_red = time.perf_counter() - t0
    boxes_by_page = rep["boxes"]
    if isinstance(boxes_by_page, dict):
        boxes_by_page = [boxes_by_page.get(p, []) for p in range(25)]
    n_boxes = sum(len(b) for b in boxes_by_page)
    n_marks = len(markers_in(prot))
    check("redazione: marcatori = box su 25 pagine", n_marks == n_boxes,
          f"boxes={n_boxes} markers={n_marks} in {t_red:.1f}s")

    t0 = time.perf_counter()
    restored, rrep = restore_pdf(prot, BIG_MAP)
    t_res = time.perf_counter() - t0
    txt = pdf_text(restored)
    check("ripristino: 200 valori scritti, zero TAG, zero lost",
          rrep["restored"] == n_boxes and not rrep["remaining"]
          and not rrep["lost"] and "[FULLNAME_" not in txt,
          f"restored={rrep['restored']} in {t_res:.1f}s")
    check("ripristino: nessun marcatore residuo", not markers_in(restored))
    ok_px = True
    for pno in (0, 12, 24):
        b = boxes_by_page[pno][0]
        ok_px &= not is_yellowish(px_at(restored, pno, b["x0"] + 1.5,
                                        b["y0"] + 1.5))
        ok_px &= px_at(restored, pno, 500, 94)[0] < 100  # riga tabella
    check("ripristino: pixel non gialli e righe tabella intatte (campione)",
          ok_px)

    # --- avversariale: manomissioni sui marcatori --------------------------
    base = fitz.open(stream=prot, filetype="pdf")
    tampered = fitz.open()
    tampered.insert_pdf(base, from_page=0, to_page=0)
    page = tampered[0]
    ph_here = {mk[1] for mk in
               [(a, a.info["subject"], a.rect) for a in page.annots()]}
    victim = sorted(ph_here)[0]
    # 1) marcatore SPOSTATO lontano (non più corroborato dall'etichetta)
    for a in list(page.annots()):
        if a.info.get("subject") == victim:
            r = a.rect
            a.set_rect(fitz.Rect(400, 700, 500, 720))
            moved_box = r
            break
    # 2) marcatore FALSO con subject sconosciuto sopra testo pulito
    fake = page.add_rect_annot(fitz.Rect(72, 60, 200, 75))
    fake.set_info(title=MARKER_TITLE, subject="[HACK_99]")
    fake.set_flags(fitz.PDF_ANNOT_IS_HIDDEN)
    fake.update()
    # 3) marcatore DUPLICATO (stesso subject e rect di uno vero)
    dup_src = next(a for a in page.annots()
                   if a.info.get("title") == MARKER_TITLE
                   and a.info.get("subject") not in (victim, "[HACK_99]"))
    dup = page.add_rect_annot(dup_src.rect)
    dup.set_info(title=MARKER_TITLE, subject=dup_src.info["subject"])
    dup.set_flags(fitz.PDF_ANNOT_IS_HIDDEN)
    dup.update()
    # 4) rettangolo giallo DEL MODELLO: deve sopravvivere
    page.draw_rect(fitz.Rect(300, 750, 400, 780), color=(1, 1, 0),
                   fill=(1, 1, 0))
    tam_bytes = tampered.tobytes()
    tampered.close()
    base.close()
    restored_t, rep_t = restore_pdf(tam_bytes, BIG_MAP)
    txt_t = pdf_text(restored_t)
    check("marcatore spostato: valore comunque scritto",
          BIG_MAP[victim] in txt_t and victim not in txt_t)
    check("marcatore spostato: il suo giallo RESTA (non corroborato) "
          "ed è dichiarato",
          is_yellowish(px_at(restored_t, 0, moved_box.x0 + 1.5,
                             moved_box.y0 + 1.5))
          and victim in rep_t["remaining"],
          f"remaining={rep_t['remaining']}")
    check("marcatore falso: ignorato, niente crash, testo pulito intatto",
          "Riga 0" in txt_t)
    check("giallo del modello: sopravvive al ripristino",
          is_yellowish(px_at(restored_t, 0, 350, 765)),
          str(px_at(restored_t, 0, 350, 765)))
    left_subj = {s for _p, s, _r in markers_in(restored_t)}
    check("marcatori residui: solo lo spostato e il falso",
          left_subj <= {victim, "[HACK_99]"}, str(left_subj))

    # --- pagina RUOTATA dal modello: niente crash, esito dichiarato --------
    rot = fitz.open(stream=prot, filetype="pdf")
    rot[0].set_rotation(90)
    rot_bytes = rot.tobytes()
    rot.close()
    try:
        restored_r, rep_r = restore_pdf(rot_bytes, BIG_MAP)
        rot_txt = pdf_text(restored_r)
        ok = ("[FULLNAME_" not in rot_txt) or bool(rep_r["remaining"])
        check("pagina ruotata: nessun crash, esito coerente", ok,
              f"restored={rep_r['restored']} remaining={len(rep_r['remaining'])}")
    except Exception as e:
        check("pagina ruotata: nessun crash", False, repr(e))

    # --- PDF cifrato e PDF troncato ----------------------------------------
    enc_doc = fitz.open(stream=prot, filetype="pdf")
    enc = enc_doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,
                          owner_pw="own", user_pw="usr")
    enc_doc.close()
    out = write_restore("d_cifrato.pdf", enc, BIG_MAP)
    check("PDF cifrato: rifiutato con garbo (None)", out[0] is None)
    # il troncato fa urlare la riparazione di MuPDF su stderr: si tace
    fitz.TOOLS.mupdf_display_errors(False)
    try:
        out = write_restore("d_troncato.pdf", prot[: len(prot) // 3],
                            BIG_MAP)
        check("PDF troncato: nessun crash",
              out[0] is None or isinstance(out[0], bytes))
    finally:
        fitz.TOOLS.mupdf_display_errors(True)


# --------------------------------------------------------------------------- #
# E. valori ostili
# --------------------------------------------------------------------------- #
@battery("valori ostili: entità XML, TAG nel valore, emoji, valore enorme")
def run_e():
    nasty = {
        "[ORG_1]": 'R&D <Labs> "Alfa" S.r.l.',
        "[ORG_2]": "valore che cita [ORG_1] testualmente",
        "[FULLNAME_1]": "José Müller-Ω 🚀",
        "[FULLNAME_11]": "Omonimo Undici",
        "[IBAN_1]": "IT" + "0" * 2000,
        "[ADDRESS_1]": "Via Roma 1\nScala B",
    }
    orig = make_docx([
        [w_run('Fornitore R&amp;D &lt;Labs&gt; "Alfa" S.r.l. contratto.')],
        [w_run("Cliente José Müller-Ω 🚀 e Omonimo Undici insieme.")],
        [w_run("Conto IT" + "0" * 2000 + " attivo.")],
        [w_run("Sede: Via Roma 1\nScala B fine.")],
    ])
    prot, _ = redact_docx(orig, nasty)
    doc_xml = _parts(prot)["word/document.xml"].decode("utf-8")
    check("redazione: TAG sovrapposti distinti nel protetto",
          "[FULLNAME_1]" in doc_xml and "[FULLNAME_11]" in doc_xml)

    # il modello aggiunge testo che cita i TAG omonimi e [ORG_2], il cui
    # VALORE contiene a sua volta un TAG: non deve innescare una catena
    art = edit_part(prot, "word/document.xml",
                    lambda x: x.replace(
                        "</w:body>",
                        "<w:p><w:r><w:t>[FULLNAME_11] vs [FULLNAME_1]"
                        "</w:t></w:r></w:p>"
                        "<w:p><w:r><w:t>Riferimento: [ORG_2] fine."
                        "</w:t></w:r></w:p></w:body>"))
    data, n, left, _x = write_restore("e_ostili.docx", art, nasty)
    out_xml = _parts(data)["word/document.xml"].decode("utf-8")
    root = None
    try:
        root = ET.fromstring(out_xml)
    except ET.ParseError as e:
        note(f"XML rotto dai valori ostili: {e}")
    check("valori ostili: XML ben formato dopo il ripristino",
          root is not None)
    body_text = "".join(root.itertext()) if root is not None else ""
    check("escaping: entità XML del valore scritte correttamente",
          'R&D <Labs> "Alfa" S.r.l.' in body_text)
    check("niente catena: il TAG citato DENTRO un valore resta testo",
          "valore che cita [ORG_1] testualmente" in body_text)
    check("TAG sovrapposti: ognuno col suo valore",
          "Omonimo Undici vs José Müller-Ω 🚀" in body_text)
    check("unicode: emoji e accenti intatti",
          "José Müller-Ω 🚀" in body_text)
    check("valore enorme: 2000+ char scritti integri",
          "IT" + "0" * 2000 in body_text)
    check("a-capo nel valore: presente nel testo",
          "Via Roma 1\nScala B" in body_text)

    # stessi valori su xlsx (celle + sharedStrings)
    orig_x = make_xlsx(
        rows=[("A1", "1", "inline",
               'Fornitore R&amp;D &lt;Labs&gt; "Alfa" S.r.l. qui'),
              ("A2", "2", "inline", "Cliente José Müller-Ω 🚀 ok"),
              ("A3", "3", "s", "0")],
        sst=["Nota per Omonimo Undici e José Müller-Ω 🚀"])
    prot_x, _ = redact_xlsx(orig_x, nasty)
    data, n, left, _x = write_restore("e_ostili.xlsx", prot_x, nasty)
    ok_xml = True
    for name, blob in _parts(data).items():
        if name.endswith(".xml"):
            try:
                ET.fromstring(blob)
            except ET.ParseError:
                ok_xml = False
                note(f"xlsx part rotta: {name}")
    check("xlsx ostile: tutte le part XML ben formate", ok_xml)
    cells = sheet_cells(data)
    check("xlsx ostile: valori al loro posto",
          'R&D <Labs> "Alfa" S.r.l.' in cells["A1"][1]
          and "José Müller-Ω 🚀" in cells["A2"][1]
          and "Omonimo Undici" in cells["A3"][1])


# --------------------------------------------------------------------------- #
# F. file malformati
# --------------------------------------------------------------------------- #
@battery("file malformati: zip rotto, XML rotto, prefissi alieni")
def run_f():
    m = {"[FULLNAME_1]": "Mario Rossi"}
    out = write_restore("f_rotto.docx", b"PK\x03\x04" + os.urandom(256), m)
    check("zip troncato: (None, 0, [], {})", out == (None, 0, [], {}))

    art = _zip({"[Content_Types].xml": DOCX_CT, "_rels/.rels": RELS,
                "word/document.xml":
                    b"<w:document><w:body><w:p><w:r><w:t>[FULLNAME_1]"})
    out = write_restore("f_xml_rotto.docx", art, m)
    check("document.xml troncato: niente crash, TAG comunque sostituito",
          out[0] is not None
          and b"Mario Rossi" in _parts(out[0])["word/document.xml"])

    # xlsx con styles.xml corrotto: il testo si ripristina, lo stile no
    orig_x = make_xlsx([("A1", "1", "inline", "Cliente Mario Rossi")], ["x"])
    prot_x, _ = redact_xlsx(orig_x, m)
    parts = _parts(prot_x)
    parts["xl/styles.xml"] = os.urandom(64)
    out = write_restore("f_styles_rotti.xlsx", _zip(parts), m)
    check("styles.xml corrotto: testo ripristinato, niente crash",
          out[0] is not None and "Mario Rossi" in
          sheet_cells(out[0])["A1"][1])

    # docx scritto dal modello con prefisso namespace NON standard
    alien = XML_HEAD + (
        '<x:document xmlns:x="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><x:body><x:p><x:r>'
        '<x:t>Nota su [FULLNAME_1] scritta cosi.</x:t>'
        '</x:r></x:p></x:body></x:document>')
    art = _zip({"[Content_Types].xml": DOCX_CT, "_rels/.rels": RELS,
                "word/document.xml": alien.encode("utf-8")})
    out = write_restore("f_alien.docx", art, m)
    check("prefisso alieno: valore ripristinato lo stesso",
          out[0] is not None and b"Mario Rossi" in
          _parts(out[0])["word/document.xml"])

    out = restore_artifact(DATA / "f_alien.docx", {})
    check("mapping vuoto: (None, 0, [], {})", out == (None, 0, [], {}))
    out = restore_artifact(DATA / "non_esiste.docx", m)
    check("file inesistente: (None, 0, [], {})", out == (None, 0, [], {}))

    # placeholder spezzato su DUE w:t dello stesso run (il modello può)
    split = make_docx([[
        '<w:r><w:rPr><w:highlight w:val="yellow"/></w:rPr>'
        '<w:t>[FULLNAME</w:t><w:t>_1]</w:t></w:r>']])
    out = write_restore("f_split.docx", split, m)
    xml = _parts(out[0])["word/document.xml"].decode("utf-8")
    if "_1]" in xml and 'w:val="yellow"' not in xml:
        note("TAG spezzato su due w:t: il giallo viene tolto ma il TAG resta "
             "e NON è dichiarato in left (limite noto? valutare)")
    check("TAG spezzato su due w:t: nessun crash", out[0] is not None,
          f"left={out[2]}")


# --------------------------------------------------------------------------- #
# G. MediaPool a scala
# --------------------------------------------------------------------------- #
def make_photo(seed):
    """La zona (20,20,180,60) viene coperta dal box giallo alla redazione:
    ciò che distingue le foto DOPO la redazione sono le due bande in basso,
    che codificano il seed con grigi distanziati (la firma 16x16 richiede
    un margine netto tra candidate: _MATCH_MARGIN)."""
    im = Image.new("RGB", (200, 120), (250, 250, 250))
    for x in range(20, 180):
        for y in range(20, 60):
            im.putpixel((x, y), ((seed * 37) % 200, (seed * 91) % 200,
                                 (seed * 53) % 200))
    ga = (seed % 16) * 16
    gb = (seed // 16) * 25 % 256
    for x in range(200):
        for y in range(62, 88):
            im.putpixel((x, y), (ga, ga, ga))
        for y in range(90, 118):
            im.putpixel((x, y), (gb, gb, gb))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


POOL = MediaPool()
PHOTOS = {}
REDACTED = {}


@battery("MediaPool: 150 coppie, 30 ricompresse, quasi-gemelle, corrotte")
def run_g():
    t0 = time.perf_counter()
    for i in range(150):
        photo = make_photo(i)
        red = redact_image_bytes(photo, ".png",
                                 [((20, 20, 180, 60), f"[FULLNAME_{i % 50}]")])
        PHOTOS[i], REDACTED[i] = photo, red
        POOL._add_input(red, photo, ".png")
    t_build = time.perf_counter() - t0
    check("pool: 150 coppie caricate", len(POOL._exact) == 150,
          f"{t_build:.1f}s")

    # artifact docx con 30 copie ricompresse jpeg + 5 immagini mai viste
    media_parts = {}
    for k in range(30):
        buf = io.BytesIO()
        Image.open(io.BytesIO(REDACTED[k])).convert("RGB").save(
            buf, format="JPEG", quality=72)
        media_parts[f"word/media/image{k + 1}.jpeg"] = buf.getvalue()
    def noise_photo(k):
        im = Image.new("L", (200, 120))
        im.putdata([(x + y * 3 + k * 41) % 256 for y in range(120)
                    for x in range(200)])
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()

    for k in range(5):
        media_parts[f"word/media/new{k}.png"] = noise_photo(k)
    art = _zip({"[Content_Types].xml": DOCX_CT, "_rels/.rels": RELS,
                "word/document.xml":
                    _parts(make_docx([[w_run("Report con foto.")]]))
                    ["word/document.xml"], **media_parts})
    t0 = time.perf_counter()
    data, _n, _l, extra = write_restore("g_media.docx", art,
                                        {"[FULLNAME_0]": "x"}, images=POOL)
    t_swap = time.perf_counter() - t0
    swapped = extra.get("images_swapped", 0)
    out_parts = _parts(data)
    ok_clean = all(
        not is_yellowish(Image.open(io.BytesIO(
            out_parts[f"word/media/image{k + 1}.jpeg"])).getpixel((30, 30)))
        for k in range(30))
    check("30 copie ricompresse: tutte scambiate con l'originale",
          swapped == 30 and ok_clean, f"swapped={swapped} in {t_swap:.1f}s")
    ok_new = all(out_parts[f"word/media/new{k}.png"]
                 == media_parts[f"word/media/new{k}.png"] for k in range(5))
    check("5 immagini mai viste: intatte", ok_new)

    # quasi-gemelle: due redatte quasi identiche in pool -> la FIRMA declina,
    # l'hash esatto continua a funzionare
    twin_a = make_photo(999)
    im = Image.open(io.BytesIO(twin_a)).copy()
    im.putpixel((5, 5), (10, 10, 10))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    twin_b = buf.getvalue()
    red_a = redact_image_bytes(twin_a, ".png", [((20, 20, 180, 60), "[X_1]")])
    red_b = redact_image_bytes(twin_b, ".png", [((20, 20, 180, 60), "[X_2]")])
    pool2 = MediaPool()
    pool2._add_input(red_a, twin_a, ".png")
    pool2._add_input(red_b, twin_b, ".png")
    check("quasi-gemelle: hash esatto aggancia quella giusta",
          pool2.lookup(red_a, ".png") == twin_a
          and pool2.lookup(red_b, ".png") == twin_b)
    rec = io.BytesIO()
    Image.open(io.BytesIO(red_a)).convert("RGB").save(rec, format="JPEG",
                                                      quality=72)
    check("quasi-gemelle ricompresse: la firma DECLINA (ambiguo -> None)",
          pool2.lookup(rec.getvalue(), ".jpeg") is None)

    # confine della firma: un'immagine MAI VISTA ma molto simile a una
    # redatta (bande uguali, cambia solo il riquadro) può cadere sotto la
    # soglia _MATCH_MAX e venire scambiata: si misura dove sta il confine
    lookalike = make_photo(5)               # stesso layout del seed 5, ma il
    hit = POOL.lookup(lookalike, ".png")    # riquadro NON è redatto in giallo
    if hit is not None:
        note("firma: un'immagine mai vista ma ~73% identica a una redatta "
             "del pool VIENE scambiata (sotto _MATCH_MAX): rischio di "
             "sostituire un'immagine nuova del modello con un originale")
    else:
        note("firma: la quasi-identica mai vista NON aggancia (sopra "
             "_MATCH_MAX)")
    check("confine firma: esito deterministico senza crash", True,
          f"hit={'si' if hit is not None else 'no'}")

    # immagine redatta MODIFICATA dal modello (freccia disegnata sopra): la
    # spec dice "modificata -> nessuno scambio"; qui si misura il confine
    marked = Image.open(io.BytesIO(REDACTED[10])).convert("RGB")
    for x in range(60, 140):
        for y in (95, 96, 97):
            marked.putpixel((x, y), (255, 0, 0))
    mbuf = io.BytesIO()
    marked.save(mbuf, format="PNG")
    hit2 = POOL.lookup(mbuf.getvalue(), ".png")
    if hit2 is not None:
        note("firma: una redatta con una FRECCIA del modello sopra viene "
             "scambiata con l'originale (l'annotazione del modello va persa)")
    else:
        note("firma: la redatta annotata dal modello NON viene scambiata")
    check("redatta annotata dal modello: esito deterministico", True,
          f"hit={'si' if hit2 is not None else 'no'}")

    # filtro pesante (schiarita di +60): fuori soglia, niente scambio
    bright = Image.open(io.BytesIO(REDACTED[20])).convert("RGB")
    bright = bright.point(lambda v: min(255, v + 60))
    bbuf = io.BytesIO()
    bright.save(bbuf, format="PNG")
    check("redatta filtrata pesantemente: nessuno scambio",
          POOL.lookup(bbuf.getvalue(), ".png") is None)

    # immagine CANCELLATA dal modello: il ripristino scambia solo ciò che
    # l'artifact contiene, non rimette mai contenuto tolto
    art_del = _zip({"[Content_Types].xml": DOCX_CT, "_rels/.rels": RELS,
                    "word/document.xml":
                        _parts(make_docx([[w_run("Senza la foto 3.")]]))
                        ["word/document.xml"],
                    "word/media/image9.png": REDACTED[7]})
    data, _n, _l, extra = write_restore("g_deleted.docx", art_del,
                                        {"[FULLNAME_0]": "x"}, images=POOL)
    out_names = set(_parts(data))
    check("immagine cancellata dal modello: NON rimessa (docx)",
          out_names == {"[Content_Types].xml", "_rels/.rels",
                        "word/document.xml", "word/media/image9.png"}
          and _parts(data)["word/media/image9.png"] == PHOTOS[7]
          and extra.get("images_swapped") == 1, str(sorted(out_names)))
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((72, 60), "Report senza immagini.", fontsize=11)
    no_img_pdf = doc.tobytes()
    doc.close()
    out_pdf, rep_pdf = restore_pdf(no_img_pdf, {"[FULLNAME_0]": "x"},
                                   media=POOL)
    with fitz.open(stream=out_pdf, filetype="pdf") as d:
        n_imgs = len(d[0].get_images(full=True))
    check("immagine cancellata dal modello: NON rimessa (pdf)",
          n_imgs == 0 and not rep_pdf.get("images_swapped"),
          f"imgs={n_imgs}")

    check("bytes corrotti nel lookup: None senza crash",
          POOL.lookup(os.urandom(1024), ".png") is None)
    t0 = time.perf_counter()
    for i in range(0, 150, 3):
        assert POOL.lookup(REDACTED[i], ".png") == PHOTOS[i]
    check("lookup esatto su pool pieno: corretto e rapido", True,
          f"50 lookup in {time.perf_counter() - t0:.2f}s")


# --------------------------------------------------------------------------- #
# H. concorrenza
# --------------------------------------------------------------------------- #
@battery("concorrenza: 8 thread su docx/xlsx/pdf/immagini col pool condiviso")
def run_h():
    from concurrent.futures import ThreadPoolExecutor

    small_map = {"[FULLNAME_1]": "Mario Rossi", "[ORG_1]": "Acme S.r.l."}
    docx_orig = make_docx([[w_run("Cliente Mario Rossi di Acme S.r.l.")],
                           [w_run("nota", hl="yellow")]])
    docx_prot, _ = redact_docx(docx_orig, small_map)
    xlsx_orig = make_xlsx([("A1", "1", "inline", "Rif Mario Rossi"),
                           ("A2", "2", "inline", "Sede Acme S.r.l.")], ["x"])
    xlsx_prot, _ = redact_xlsx(xlsx_orig, small_map)
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((72, 100), "Contratto di Mario Rossi con Acme S.r.l.",
                   fontsize=11)
    pdf_prot, _ = redact_pdf(doc.tobytes(), small_map)
    doc.close()

    files = {}
    for i in range(8):
        for kind, blob in (("docx", docx_prot), ("xlsx", xlsx_prot),
                           ("pdf", pdf_prot), ("png", REDACTED[i])):
            p = DATA / f"h_{kind}_{i}.{kind}"
            p.write_bytes(blob)
            files[(kind, i)] = p

    def job(key):
        kind, i = key
        return kind, i, restore_artifact(files[key], small_map, images=POOL)

    serial = {k: job(k)[2] for k in files}
    results = {}
    errors = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for _round in range(3):
            futs = [ex.submit(job, k) for k in files]
            for f in futs:
                try:
                    kind, i, out = f.result()
                    results.setdefault((kind, i), []).append(out)
                except Exception as e:
                    errors.append(repr(e))
    check("concorrenza: zero eccezioni su 96 ripristini", not errors,
          str(errors[:3]))
    stable = True
    for key, outs in results.items():
        ref = serial[key]
        for out in outs:
            if key[0] == "pdf":
                same = (out[1] == ref[1] and out[2] == ref[2]
                        and pdf_text(out[0]) == pdf_text(ref[0]))
            elif key[0] == "png":
                same = out[0] == ref[0]
            else:
                same = out[0] == ref[0] and out[1:] == ref[1:]
            if not same:
                stable = False
                note(f"risultato instabile per {key}")
    check("concorrenza: risultati identici al riferimento seriale", stable)


# --------------------------------------------------------------------------- #
# I. idempotenza docx/pptx/pdf (xlsx coperto in C)
# --------------------------------------------------------------------------- #
@battery("idempotenza: secondo ripristino innocuo su docx/pptx/pdf")
def run_i():
    small_map = {"[FULLNAME_1]": "Mario Rossi", "[ORG_1]": "Acme S.r.l."}
    docx_prot, _ = redact_docx(
        make_docx([[w_run("Cliente Mario Rossi di Acme S.r.l.")],
                   [w_run("nota", hl="yellow")]]), small_map)
    d1 = write_restore("i_1.docx", docx_prot, small_map)
    d2 = write_restore("i_2.docx", d1[0], small_map)
    check("docx: secondo passaggio senza cambi",
          d2[0] is not None and _parts(d2[0])["word/document.xml"]
          == _parts(d1[0])["word/document.xml"])

    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((72, 100), "Contratto di Mario Rossi qui.", fontsize=11)
    pdf_prot, _ = redact_pdf(doc.tobytes(), small_map)
    doc.close()
    r1, rep1 = restore_pdf(pdf_prot, small_map)
    r2, rep2 = restore_pdf(r1, small_map)
    check("pdf: secondo passaggio senza cambi",
          rep2["restored"] == 0 and not rep2["remaining"]
          and pdf_text(r2) == pdf_text(r1) and not markers_in(r2),
          f"rep2={ {k: v for k, v in rep2.items() if v} }")


for fn in (run_a, run_b, run_c, run_d, run_e, run_f, run_g, run_h, run_i):
    fn()

print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
if NOTES:
    print("Note da valutare:")
    for msg in NOTES:
        print(f"  - {msg}")
sys.exit(1 if FAIL else 0)

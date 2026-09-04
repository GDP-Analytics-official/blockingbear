"""Ripristino senza cicatrici: i segni che la redazione lascia nella copia
protetta (evidenziatore giallo nei docx/pptx, fill giallo nelle celle xlsx,
box gialli nei PDF, immagini redatte nei pixel) devono sparire dagli artifact
insieme ai placeholder — e i segni dell'UTENTE (highlight suoi, fill suoi,
grafica gialla sua) devono restare esattamente dov'erano.

Sezioni:
  1. docx  — giallo via dai run col placeholder; highlight preesistenti
             dell'utente (verde, e giallo su testo non-PII) sopravvivono;
             contenuto inserito a metà file non confonde niente; placeholder
             sconosciuto tiene il suo giallo.
  2. pptx  — idem con a:highlight (struttura DrawingML).
  3. xlsx  — fill giallo -> stile originale auto-descritto nel bgColor:
             nessun fill, solid rgb, solid theme, giallo dell'utente su PII
             (punto fisso), righe aggiunte, indici degli stili rimescolati.
  4. PDF   — marcatore invisibile per box; al ripristino il box giallo
             SPARISCE dai pixel e il valore è al suo posto; placeholder
             ignoto tiene box e marcatore; la grafica che attraversa il box
             (bordi tabella) resta; pagine copiate con fitz (artifact
             rimontato) funzionano uguale.
  5. Media — accoppiamento redatto/originale per part (OOXML), per
             rettangolo (PDF), file singolo; lookup sha1 e ripiego per firma
             16x16 con ricompressione; aree sigillate ESCLUSE dal pool.
  6. Integrazione — MediaPool.load_inputs da righe DB vere e restore_artifact
             su artifact docx/xlsx/png completi.

Niente modello PII, niente rete, niente Docker: LibreOffice serve solo per
la prova di validità dei file costruiti a mano (conversione in PDF).

Uso:
    python backend/tests/restore_marks_test.py
"""
import io
import os
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
DATA = HERE / "data" / "test_restore_marks"
if DATA.exists():
    shutil.rmtree(DATA, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

import fitz                                                       # noqa: E402
from PIL import Image                                             # noqa: E402

from app import db                                                # noqa: E402
from app.chat_anonymization import (MediaPool, _pair_input_media,  # noqa: E402
                                    _strip_run_marks, restore_artifact)
from app.engine import convert, image_ocr                         # noqa: E402
from app.engine.docx import redact_docx                           # noqa: E402
from app.engine.pdf_export import (MARKER_TITLE, redact_pdf,      # noqa: E402
                                   restore_pdf)
from app.engine.pptx import redact_pptx                           # noqa: E402
from app.engine.xlsx import redact_xlsx, restore_cell_styles      # noqa: E402
from app.engine.image_ocr import redact_image_bytes               # noqa: E402

convert._PROFILE_DIR = DATA / "lo_profile"

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


MAPPING = {"[FULLNAME_1]": "Mario Rossi", "[ORG_1]": "Acme Corp S.r.l."}


# --------------------------------------------------------------------------- #
# Builder OOXML minimi (niente python-docx/pptx/openpyxl sull'host)
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
    return (f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>')


def make_docx(paragraphs):
    """paragraphs: lista di liste di run già serializzati (w_run)."""
    body = "".join(f"<w:p>{''.join(runs)}</w:p>" for runs in paragraphs)
    doc = XML_HEAD + (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    return _zip({"[Content_Types].xml": DOCX_CT, "_rels/.rels": RELS,
                 "word/document.xml": doc})


PPTX_CT = XML_HEAD + (
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    'content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
    'package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/ppt/presentation.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
    '<Override PartName="/ppt/slides/slide1.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
    '</Types>')
PPTX_RELS = XML_HEAD + (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships"><Relationship Id="rId1" Type="http://schemas.'
    'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="ppt/presentation.xml"/></Relationships>')
PPTX_PRES = XML_HEAD + (
    '<p:presentation xmlns:p="http://schemas.openxmlformats.org/'
    'presentationml/2006/main"/>')


def a_run(text, hl=False):
    rpr = ('<a:rPr lang="it-IT"><a:highlight><a:srgbClr val="FFFF00"/>'
           '</a:highlight></a:rPr>') if hl else '<a:rPr lang="it-IT"/>'
    return f'<a:r>{rpr}<a:t>{text}</a:t></a:r>'


def make_pptx(runs):
    slide = XML_HEAD + (
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/'
        '2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/'
        '2006/main"><p:cSld><p:spTree><p:sp><p:txBody>'
        f'<a:p>{"".join(runs)}</a:p>'
        '</p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
    return _zip({"[Content_Types].xml": PPTX_CT, "_rels/.rels": PPTX_RELS,
                 "ppt/presentation.xml": PPTX_PRES,
                 "ppt/slides/slide1.xml": slide})


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
# Fill: 0=none, 1=gray125 (obbligatori), 2=azzurro, 3=GIALLO dell'utente,
# 4=solid theme. cellXfs: 0=default, 1=azzurro, 2=giallo utente, 3=theme.
XLSX_STYLES = XML_HEAD + (
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
    '2006/main">'
    '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="5">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF99CCFF"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFF00"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor theme="4" tint="0.4"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    '</fills>'
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" '
    'borderId="0"/></cellStyleXfs>'
    '<cellXfs count="4">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="0" fillId="2" borderId="0" xfId="0" '
    'applyFill="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" '
    'applyFill="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" '
    'applyFill="1"/>'
    '</cellXfs></styleSheet>')


def make_xlsx(rows, sst):
    """rows: [(ref, style, kind, value)] con kind 's' (indice sst) o 'inline'.
    sst: lista di stringhe condivise."""
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


import re                                                          # noqa: E402

_RUN_RE = re.compile(r"<w:r(?:>|\s[^>]*>).*?</w:r>", re.S)


def run_of(xml, needle):
    """Il blocco <w:r> che contiene `needle` (il primo)."""
    for m in _RUN_RE.finditer(xml):
        if needle in m.group(0):
            return m.group(0)
    return ""


def edit_part(data, part, fn):
    parts = _parts(data)
    parts[part] = fn(parts[part].decode("utf-8")).encode("utf-8")
    return _zip(parts)


# --------------------------------------------------------------------------- #
# 1. docx
# --------------------------------------------------------------------------- #
print("\n[1] docx: evidenziatore della redazione vs highlight dell'utente")

original = make_docx([
    [w_run("Contratto tra Mario Rossi e la controparte.")],
    [w_run("Nota gialla dell'utente su testo qualunque.", hl="yellow")],
    [w_run("Il fornitore "), w_run("Acme Corp S.r.l.", hl="green"),
     w_run(" firma qui.")],
])
protected, report = redact_docx(original, MAPPING)
doc_xml = _parts(protected)["word/document.xml"].decode("utf-8")
check("redazione: placeholder nel testo",
      "[FULLNAME_1]" in doc_xml and "[ORG_1]" in doc_xml)
check("redazione: giallo NOSTRO sul run di [FULLNAME_1]",
      '<w:highlight w:val="yellow" />' in doc_xml.replace(
          '"yellow"/>', '"yellow" />'))
# il run di [ORG_1] aveva l'highlight VERDE dell'utente: si tiene quello
org_run = run_of(doc_xml, "[ORG_1]")
check("redazione: [ORG_1] eredita il verde dell'utente, niente giallo",
      'w:val="green"' in org_run and 'w:val="yellow"' not in org_run)

# "modifica del modello": un paragrafo in mezzo (con un placeholder nel testo
# nuovo, senza highlight — è quello che fa python-docx) + uno in coda
protected_path = DATA / "in.docx"
DATA.mkdir(parents=True, exist_ok=True)


def model_edit(xml):
    insert = (f"<w:p><w:r><w:t>Riassunto su [ORG_1] scritto dal modello."
              f"</w:t></w:r></w:p>")
    first_p_end = xml.find("</w:p>") + len("</w:p>")
    xml = xml[:first_p_end] + insert + xml[first_p_end:]
    return xml.replace("</w:body>",
                       "<w:p><w:r><w:t>Coda aggiunta.</w:t></w:r></w:p>"
                       "</w:body>")


artifact = edit_part(protected, "word/document.xml", model_edit)
art_path = DATA / "artifact.docx"
art_path.write_bytes(artifact)
data, n, left, extra = restore_artifact(art_path, MAPPING)
check("ripristino: byte riscritti", data is not None)
out_xml = _parts(data)["word/document.xml"].decode("utf-8")
check("ripristino: valori al posto dei placeholder",
      "Mario Rossi" in out_xml and "Acme Corp S.r.l." in out_xml
      and "[FULLNAME_1]" not in out_xml and "[ORG_1]" not in out_xml,
      f"n={n} left={left}")
yellows = out_xml.count('w:val="yellow"')
check("ripristino: giallo NOSTRO sparito, giallo dell'utente intatto",
      yellows == 1 and "Nota gialla" in out_xml, f"yellow rimasti={yellows}")
# il valore ripristinato dentro il run che l'utente aveva evidenziato di
# verde tiene il SUO verde (la redazione l'aveva ereditato, non sostituito)
check("ripristino: verde dell'utente sul valore ripristinato",
      'w:val="green"' in run_of(out_xml, ">Acme Corp S.r.l.</w:t>"))
# il testo nuovo del modello col placeholder: valore scritto, nessun giallo
check("ripristino: testo del modello senza evidenziature spurie",
      "Riassunto su Acme Corp S.r.l. scritto dal modello." in out_xml)

# placeholder SCONOSCIUTO: tiene il suo segno (qui il verde ereditato) e il
# segnaposto; il giallo dell'utente su testo non-PII non si tocca mai
partial = {"[FULLNAME_1]": "Mario Rossi"}
data2, n2, left2, _ = restore_artifact(art_path, partial)
out2 = _parts(data2)["word/document.xml"].decode("utf-8")
check("placeholder ignoto: segno e segnaposto restano",
      "[ORG_1]" in out2
      and 'w:val="green"' in run_of(out2, ">[ORG_1]</w:t>")
      and out2.count('w:val="yellow"') == 1
      and "[FULLNAME_1]" not in out2, f"left={left2}")
# ...e un ignoto evidenziato col giallo NOSTRO tiene il giallo
data3, _n3, _l3, _ = restore_artifact(art_path, {"[ORG_1]": MAPPING["[ORG_1]"]})
out3 = _parts(data3)["word/document.xml"].decode("utf-8")
check("placeholder ignoto: il giallo nostro resta finché il TAG resta",
      'w:val="yellow"' in run_of(out3, "[FULLNAME_1]"))

# validità LibreOffice del giro completo (originale -> protetto -> artifact)
try:
    pdf_ok = convert.to_pdf(data, suffix=".docx")
    check("validità: l'artifact ripristinato si converte in PDF",
          pdf_ok[:4] == b"%PDF")
except Exception as e:
    check("validità: l'artifact ripristinato si converte in PDF", False,
          repr(e))

# --------------------------------------------------------------------------- #
# 2. pptx
# --------------------------------------------------------------------------- #
print("\n[2] pptx: a:highlight della redazione vs highlight dell'utente")

orig_pptx = make_pptx([
    a_run("Relatore: Mario Rossi. "),
    a_run("Titolo evidenziato dall'utente.", hl=True),
])
prot_pptx, rep = redact_pptx(orig_pptx, MAPPING)
slide = _parts(prot_pptx)["ppt/slides/slide1.xml"].decode("utf-8")
check("redazione: placeholder nel run", "[FULLNAME_1]" in slide)
check("redazione: giallo nostro presente",
      slide.count("<a:highlight>") >= 2)

art = edit_part(
    prot_pptx, "ppt/slides/slide1.xml",
    lambda xml: xml.replace("</a:p>", "</a:p><a:p><a:r><a:rPr/>"
                            "<a:t>Slide nota del modello</a:t></a:r></a:p>",
                            1))
p = DATA / "artifact.pptx"
p.write_bytes(art)
data, n, left, extra = restore_artifact(p, MAPPING)
out = _parts(data)["ppt/slides/slide1.xml"].decode("utf-8")
check("ripristino: valore nel run e placeholder sparito",
      "Mario Rossi" in out and "[FULLNAME_1]" not in out)
check("ripristino: resta solo l'highlight dell'utente",
      out.count("<a:highlight>") == 1
      and "Titolo evidenziato dall'utente." in out)

# --------------------------------------------------------------------------- #
# 3. xlsx
# --------------------------------------------------------------------------- #
print("\n[3] xlsx: fill giallo auto-descrittivo -> stile originale")

import xml.etree.ElementTree as ET                                 # noqa: E402

X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def sheet_cells(data):
    """{ref: (s, testo)} del primo foglio."""
    root = ET.fromstring(_parts(data)["xl/worksheets/sheet1.xml"])
    sst = []
    if "xl/sharedStrings.xml" in _parts(data):
        sroot = ET.fromstring(_parts(data)["xl/sharedStrings.xml"])
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
    """(patternType, fg attrs, bg attrs) del fill dello stile `s_idx`."""
    root = ET.fromstring(_parts(data)["xl/styles.xml"])
    xfs = list(root.find(X + "cellXfs"))
    fills = list(root.find(X + "fills"))
    pf = fills[int(xfs[int(s_idx)].get("fillId") or 0)].find(X + "patternFill")
    if pf is None:
        return (None, {}, {})
    fg, bg = pf.find(X + "fgColor"), pf.find(X + "bgColor")
    return (pf.get("patternType"),
            dict(fg.attrib) if fg is not None else {},
            dict(bg.attrib) if bg is not None else {})


# A1 senza fill, A2 fill azzurro, A3 GIALLO dell'utente, A4 fill theme,
# B1 giallo dell'utente su testo NON PII (non si deve toccare mai),
# A5 via sharedStrings (t="s"), A6 placeholder che resterà ignoto.
orig_xlsx = make_xlsx(
    rows=[("A1", "0", "inline", "Referente: Mario Rossi"),
          ("A2", "1", "inline", "Fornitore Acme Corp S.r.l. spa"),
          ("A3", "2", "inline", "Mario Rossi (evidenziato dall'utente)"),
          ("A4", "3", "inline", "Sede di Acme Corp S.r.l. qui"),
          ("B1", "2", "inline", "Cella gialla dell'utente, nessuna PII"),
          ("A5", "0", "s", "0"),
          ("A6", "0", "inline", "Riga con Anna Verdi che resterà TAG")],
    sst=["Contratto con Mario Rossi"])
XLSX_MAP = dict(MAPPING)
XLSX_MAP["[FULLNAME_2]"] = "Anna Verdi"
prot_xlsx, rep = redact_xlsx(orig_xlsx, XLSX_MAP)
cells = sheet_cells(prot_xlsx)
check("redazione: placeholder nelle celle",
      "[FULLNAME_1]" in cells["A1"][1] and "[ORG_1]" in cells["A2"][1]
      and "[FULLNAME_1]" in cells["A5"][1], str({k: v[1] for k, v in
                                                 cells.items()}))
pt, fg, bg = fill_of(prot_xlsx, cells["A1"][0])
check("redazione: giallo con bgColor 'nessun fill' (A1)",
      pt == "solid" and fg.get("rgb") == "FFFFFF00"
      and bg == {"indexed": "64"}, f"{pt} {fg} {bg}")
pt, fg, bg = fill_of(prot_xlsx, cells["A2"][0])
check("redazione: giallo che ricorda l'azzurro (A2)",
      pt == "solid" and fg.get("rgb") == "FFFFFF00"
      and bg.get("rgb") == "FF99CCFF", f"{pt} {fg} {bg}")
pt, fg, bg = fill_of(prot_xlsx, cells["A3"][0])
check("redazione: giallo che ricorda il giallo dell'utente (A3)",
      pt == "solid" and bg.get("rgb") == "FFFFFF00", f"{pt} {fg} {bg}")
pt, fg, bg = fill_of(prot_xlsx, cells["A4"][0])
check("redazione: giallo che ricorda il fill theme (A4)",
      pt == "solid" and bg.get("theme") == "4"
      and bg.get("tint") == "0.4", f"{pt} {fg} {bg}")
check("redazione: cella gialla dell'utente senza PII intoccata (B1)",
      cells["B1"][0] == "2")

# "modifica del modello": una riga in mezzo (slittamento) — le celle nuove
# citano un placeholder ma NON hanno il giallo
def add_row(xml):
    new = ('<row r="2"><c r="C2" t="inlineStr"><is>'
           '<t>Totali per [ORG_1] calcolati</t></is></c></row>')
    return xml.replace("<row r=\"2\">", new + "<row r=\"2\">", 1)


art_xlsx = edit_part(prot_xlsx, "xl/worksheets/sheet1.xml", add_row)
px = DATA / "artifact.xlsx"
px.write_bytes(art_xlsx)
restore_map = dict(MAPPING)     # [FULLNAME_2] resta ignoto
data, n, left, extra = restore_artifact(px, restore_map)
cells = sheet_cells(data)
check("ripristino: valori nelle celle",
      "Mario Rossi" in cells["A1"][1] and "Acme Corp S.r.l." in cells["A2"][1]
      and "Mario Rossi" in cells["A5"][1], f"n={n} left={left}")


def is_yellow_fill(data, s_idx):
    pt, fg, _bg = fill_of(data, s_idx)
    return pt == "solid" and (fg.get("rgb") or "").upper() in ("FFFFFF00",
                                                               "FFFF00")


check("ripristino: A1 torna senza fill",
      fill_of(data, cells["A1"][0])[0] in (None, "none"),
      str(fill_of(data, cells["A1"][0])))
pt, fg, bg = fill_of(data, cells["A2"][0])
check("ripristino: A2 torna azzurra",
      pt == "solid" and fg.get("rgb") == "FF99CCFF", f"{pt} {fg} {bg}")
check("ripristino: A3 resta gialla (giallo dell'UTENTE)",
      is_yellow_fill(data, cells["A3"][0]),
      str(fill_of(data, cells["A3"][0])))
pt, fg, bg = fill_of(data, cells["A4"][0])
check("ripristino: A4 torna theme",
      pt == "solid" and fg.get("theme") == "4" and fg.get("tint") == "0.4",
      f"{pt} {fg} {bg}")
check("ripristino: B1 (gialla dell'utente, no PII) intoccata",
      is_yellow_fill(data, cells["B1"][0]))
check("ripristino: A6 col TAG ignoto resta gialla",
      "[FULLNAME_2]" in cells["A6"][1]
      and is_yellow_fill(data, cells["A6"][0]),
      f"{cells['A6']}")
check("ripristino: la riga del modello non si colora",
      cells.get("C2", ("0", ""))[0] == "0"
      and "Acme Corp S.r.l." in cells.get("C2", ("0", ""))[1])

# punto fisso: un secondo ripristino non cambia più niente
p2 = DATA / "artifact2.xlsx"
p2.write_bytes(data)
data_bis, _n, _l, _ = restore_artifact(p2, restore_map)
check("ripristino: idempotente (secondo passaggio innocuo)",
      data_bis is not None
      and sheet_cells(data_bis)["A3"] == sheet_cells(data)["A3"]
      and is_yellow_fill(data_bis, sheet_cells(data_bis)["A3"][0]))

# indici rimescolati (quello che fa openpyxl nella sandbox): un fill e uno
# stile NUOVI in testa, tutti i riferimenti slittati di 1
def reshuffle(zip_bytes):
    parts = _parts(zip_bytes)
    # un fill nuovo in testa: tutti gli indici slittano
    root = ET.fromstring(parts["xl/styles.xml"])
    fills = root.find(X + "fills")
    first = ET.Element(X + "fill")
    ET.SubElement(first, X + "patternFill").set("patternType", "gray0625")
    fills.insert(0, first)
    fills.set("count", str(len(list(fills))))
    for xf in root.find(X + "cellXfs"):
        xf.set("fillId", str(int(xf.get("fillId") or 0) + 1))
    parts["xl/styles.xml"] = ET.tostring(root)
    return _zip(parts)


art3 = reshuffle(art_xlsx)
p3 = DATA / "artifact3.xlsx"
p3.write_bytes(art3)
data3, n3, left3, _ = restore_artifact(p3, restore_map)
cells3 = sheet_cells(data3)
pt, fg, bg = fill_of(data3, cells3["A2"][0])
check("indici rimescolati: A2 torna azzurra lo stesso",
      pt == "solid" and fg.get("rgb") == "FF99CCFF", f"{pt} {fg} {bg}")
check("indici rimescolati: A1 senza fill, A3 gialla",
      fill_of(data3, cells3["A1"][0])[0] in (None, "none")
      and is_yellow_fill(data3, cells3["A3"][0]))

# validità LibreOffice del file ripristinato
try:
    pdf_ok = convert.to_pdf(data, suffix=".xlsx")
    check("validità: l'artifact xlsx ripristinato si converte in PDF",
          pdf_ok[:4] == b"%PDF")
except Exception as e:
    check("validità: l'artifact xlsx ripristinato si converte in PDF",
          False, repr(e))

# --------------------------------------------------------------------------- #
# 4. PDF
# --------------------------------------------------------------------------- #
print("\n[4] PDF: marcatori, box giallo rimosso nei pixel, grafica intatta")


def px_at(pdf_bytes, pno, x, y, zoom=3.0):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        pm = doc[pno].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pm.pixel(min(int(x * zoom), pm.width - 1),
                        min(int(y * zoom), pm.height - 1))


def is_yellowish(c):
    return c[0] > 220 and c[1] > 190 and c[2] < 140


def markers_in(pdf_bytes):
    out = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            for a in page.annots() or []:
                if a.info.get("title") == MARKER_TITLE:
                    out.append((a.info.get("subject"), tuple(a.rect)))
    return out


def make_pdf():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Contratto tra Mario Rossi e Acme Corp "
                     "S.r.l. in data odierna.", fontsize=11)
    page.insert_text((72, 140), "Referente: Mario Rossi", fontsize=11)
    page.insert_text((72, 180), "Controparte ignota: Anna Verdi", fontsize=11)
    # una riga di tabella che ATTRAVERSA la zona del primo nome
    page.draw_line((60, 104), (540, 104), color=(0, 0, 0), width=0.7)
    return doc.tobytes()


PDF_MAP = dict(MAPPING)
PDF_MAP["[FULLNAME_2]"] = "Anna Verdi"
prot_pdf, rep = redact_pdf(make_pdf(), PDF_MAP)
boxes = rep["boxes"][0]
check("redazione: box e marcatori allineati",
      len(markers_in(prot_pdf)) == len(boxes) and len(boxes) >= 4,
      f"boxes={len(boxes)} markers={len(markers_in(prot_pdf))}")
b0 = next(b for b in boxes if b["ph"] == "[FULLNAME_1]")
corner = (b0["x0"] + 1.5, b0["y0"] + 1.5)
check("redazione: pixel giallo nel box",
      is_yellowish(px_at(prot_pdf, 0, *corner)),
      str(px_at(prot_pdf, 0, *corner)))
check("redazione: i marcatori sono nascosti (non si disegnano)",
      not is_yellowish(px_at(prot_pdf, 0, b0["x0"] - 4, b0["y0"] - 4)))

# ripristino DIRETTO del protetto (mapping senza [FULLNAME_2])
restored, rrep = restore_pdf(prot_pdf, MAPPING)
rtext = ""
with fitz.open(stream=restored, filetype="pdf") as d:
    rtext = d[0].get_text()
check("ripristino: valori nel testo, TAG noti spariti",
      "Mario Rossi" in rtext and "Acme Corp" in rtext
      and "[FULLNAME_1]" not in rtext and "[ORG_1]" not in rtext,
      f"restored={rrep['restored']}")
check("ripristino: pixel NON più giallo dove c'era il box",
      not is_yellowish(px_at(restored, 0, *corner)),
      str(px_at(restored, 0, *corner)))
check("ripristino: la riga di tabella sopravvive",
      px_at(restored, 0, 500, 104)[0] < 100,
      str(px_at(restored, 0, 500, 104)))
left_marks = markers_in(restored)
check("ripristino: restano solo i marcatori del TAG ignoto",
      {ph for ph, _r in left_marks} == {"[FULLNAME_2]"}, str(left_marks))
b2 = next(b for b in boxes if b["ph"] == "[FULLNAME_2]")
check("ripristino: il box del TAG ignoto resta giallo",
      is_yellowish(px_at(restored, 0, b2["x0"] + 1.5, b2["y0"] + 1.5)))
check("ripristino: il TAG ignoto è dichiarato",
      "[FULLNAME_2]" in rrep["remaining"], str(rrep["remaining"]))

# artifact RIMONTATO: pagine copiate con fitz in un documento nuovo, più
# contenuto del modello (è quello che fa il codice della sandbox coi PDF)
src = fitz.open(stream=prot_pdf, filetype="pdf")
art = fitz.open()
art.insert_pdf(src)
art[0].insert_text((72, 700), "Sezione aggiunta dal modello su [ORG_1].",
                   fontsize=10)
art_pdf = art.tobytes()
art.close()
src.close()
restored2, rrep2 = restore_pdf(art_pdf, MAPPING)
with fitz.open(stream=restored2, filetype="pdf") as d:
    rtext2 = d[0].get_text()
check("artifact rimontato: valori scritti e giallo rimosso",
      "Acme Corp" in rtext2 and "[ORG_1]" not in rtext2
      and not is_yellowish(px_at(restored2, 0, *corner)),
      str(px_at(restored2, 0, *corner)))
# il valore nel testo del MODELLO viene scritto col corpo ridotto (il PDF non
# rifluisce: limite noto e dichiarato in `shrunk`), ma c'è ed è in chiaro
check("artifact rimontato: testo del modello ripristinato in chiaro",
      "Sezione aggiunta dal modello su" in rtext2
      and "Acme Corp S.r.l." in rtext2 and "[ORG_1]" not in rtext2)

# marcatori STRACCIATI (un tool a valle li ha tolti): si ricade nel
# comportamento di prima — valore scritto, giallo che resta, mai un errore
naked = fitz.open(stream=art_pdf, filetype="pdf")
for pg in naked:
    for a in list(pg.annots() or []):
        pg.delete_annot(a)
naked_pdf = naked.tobytes()
naked.close()
restored3, rrep3 = restore_pdf(naked_pdf, MAPPING)
with fitz.open(stream=restored3, filetype="pdf") as d:
    rtext3 = d[0].get_text()
check("senza marcatori: valori comunque scritti (giallo resta, dichiarato)",
      "Mario Rossi" in rtext3 and "[FULLNAME_1]" not in rtext3
      and is_yellowish(px_at(restored3, 0, *corner)))

# --------------------------------------------------------------------------- #
# 5. MediaPool: immagini redatte -> originali
# --------------------------------------------------------------------------- #
print("\n[5] MediaPool: scambio immagini per hash e per firma")

BOX = ((20, 20, 180, 60), "[FULLNAME_1]")


def make_photo():
    im = Image.new("RGB", (200, 120), (250, 250, 250))
    for x in range(20, 180):
        for y in range(20, 60):
            im.putpixel((x, y), (30, 60, 120))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


photo = make_photo()
red_photo = redact_image_bytes(photo, ".png", [BOX])
check("premessa: la redazione dipinge il giallo nei pixel",
      is_yellowish(Image.open(io.BytesIO(red_photo)).getpixel((30, 30))))

# input docx: originale con la foto, protetto con la foto redatta
orig_docx_img = _zip({"[Content_Types].xml": DOCX_CT, "_rels/.rels": RELS,
                      "word/document.xml": _parts(make_docx(
                          [[w_run("Vedi foto di Mario Rossi.")]]))
                      ["word/document.xml"],
                      "word/media/image1.png": photo})
prot_docx_img, _ = redact_docx(orig_docx_img, MAPPING)
prot_docx_img = edit_part(prot_docx_img, "word/document.xml", lambda x: x)
parts = _parts(prot_docx_img)
parts["word/media/image1.png"] = red_photo
prot_docx_img = _zip(parts)
(DATA / "img_orig.docx").write_bytes(orig_docx_img)
(DATA / "img_prot.docx").write_bytes(prot_docx_img)

pool = MediaPool()
_pair_input_media(pool, DATA / "img_orig.docx", DATA / "img_prot.docx",
                  ".docx", has_cache=True)
check("accoppiamento OOXML: la coppia c'è",
      pool.lookup(red_photo, ".png") == photo)
check("accoppiamento OOXML: un'immagine qualunque non aggancia",
      pool.lookup(make_pdf(), ".png") is None)

# senza cache OCR le media non sono mai state redatte: nessun accoppiamento
pool_nc = MediaPool()
_pair_input_media(pool_nc, DATA / "img_orig.docx", DATA / "img_prot.docx",
                  ".docx", has_cache=False)
check("senza cache OCR: pool vuoto",
      pool_nc.lookup(red_photo, ".png") is None)

# artifact docx del modello: incastona la foto REDATTA pari pari + una
# versione RICOMPRESSA in jpeg (ripiego per firma)
rec = io.BytesIO()
Image.open(io.BytesIO(red_photo)).convert("RGB").save(rec, format="JPEG",
                                                      quality=70)
art_media = _zip({
    "[Content_Types].xml": DOCX_CT, "_rels/.rels": RELS,
    "word/document.xml": _parts(make_docx(
        [[w_run("Report con foto per [FULLNAME_1].")]]))["word/document.xml"],
    "word/media/image1.png": red_photo,
    "word/media/image2.jpeg": rec.getvalue()})
pm = DATA / "artifact_media.docx"
pm.write_bytes(art_media)
data, n, left, extra = restore_artifact(pm, MAPPING, images=pool)
out_parts = _parts(data)
check("artifact docx: foto redatta -> originale (sha1)",
      out_parts["word/media/image1.png"] == photo,
      f"swapped={extra.get('images_swapped')}")
swapped_jpeg = out_parts["word/media/image2.jpeg"]
jp = Image.open(io.BytesIO(swapped_jpeg))
check("artifact docx: copia ricompressa -> originale adattato (firma)",
      jp.format == "JPEG" and not is_yellowish(jp.getpixel((30, 30)))
      and extra.get("images_swapped") == 2,
      f"format={jp.format} px={jp.getpixel((30, 30))}")

# input PDF: immagine redatta dentro il protetto, accoppiata per rettangolo
doc = fitz.open()
page = doc.new_page()
page.insert_text((72, 60), "Sopralluogo di Mario Rossi.", fontsize=11)
page.insert_image(fitz.Rect(72, 100, 272, 220), stream=photo)
orig_pdf_img = doc.tobytes()
doc.close()
prot_pdf_img, _rep = redact_pdf(orig_pdf_img, MAPPING)
with fitz.open(stream=prot_pdf_img, filetype="pdf") as d:
    xref = d[0].get_images(full=True)[0][0]
    d[0].replace_image(xref, stream=red_photo)
    prot_pdf_img = d.tobytes(garbage=3, deflate=True)
(DATA / "img_orig.pdf").write_bytes(orig_pdf_img)
(DATA / "img_prot.pdf").write_bytes(prot_pdf_img)
pool_pdf = MediaPool()
_pair_input_media(pool_pdf, DATA / "img_orig.pdf", DATA / "img_prot.pdf",
                  ".pdf", has_cache=True)
with fitz.open(stream=prot_pdf_img, filetype="pdf") as d:
    embedded = d.extract_image(d[0].get_images(full=True)[0][0])["image"]
check("accoppiamento PDF per rettangolo: la coppia c'è",
      pool_pdf.lookup(embedded) is not None)

# artifact PDF che porta l'immagine redatta: al ripristino torna quella vera
restored4, rrep4 = restore_pdf(prot_pdf_img, MAPPING, media=pool_pdf)
with fitz.open(stream=restored4, filetype="pdf") as d:
    out_img = d.extract_image(d[0].get_images(full=True)[0][0])["image"]
    im = Image.open(io.BytesIO(out_img)).convert("RGB")
check("artifact PDF: immagine redatta -> originale",
      not is_yellowish(im.getpixel((30, 30)))
      and rrep4.get("images_swapped") == 1,
      f"px={im.getpixel((30, 30))} swapped={rrep4.get('images_swapped')}")

# immagine CONSEGNATA COME FILE: copia del protetto -> originale
pimg = DATA / "copia_foto.png"
pimg.write_bytes(red_photo)
data, n, left, extra = restore_artifact(pimg, MAPPING, images=pool)
check("artifact immagine singola: consegnato l'originale",
      data == photo and extra.get("image_restored") is True)
# ...e una foto MAI vista resta com'è (dichiarata non ripristinabile)
pimg2 = DATA / "foto_nuova.png"
pimg2.write_bytes(make_photo()[:-4] + b"\x00\x00\x00\x00")
data2b, _n, _l, _ = restore_artifact(pimg2, MAPPING, images=pool)
check("artifact immagine mai vista: nessuno scambio", data2b is None)

# --------------------------------------------------------------------------- #
# 6. Integrazione: load_inputs da righe DB vere
# --------------------------------------------------------------------------- #
print("\n[6] Integrazione: MediaPool.load_inputs e aree sigillate")

db.init_db()
from app.db import Attachment, Conversation                       # noqa: E402

with db.SessionLocal() as s:
    conv = Conversation(owner_id=1, anonymized=1, title="test")
    s.add(conv)
    s.flush()
    chat_dir = DATA / "chats" / conv.id
    chat_dir.mkdir(parents=True, exist_ok=True)
    # allegato docx protetto con immagine redatta + cache OCR
    a1 = Attachment(conv_id=conv.id, direction="in", source="upload",
                    filename="foto.docx", anonymization_status="protected")
    s.add(a1)
    s.flush()
    a1.original_path = str(chat_dir / f"{a1.id}_foto.docx")
    a1.protected_path = str(chat_dir / f"{a1.id}_protected.docx")
    Path(a1.original_path).write_bytes(orig_docx_img)
    Path(a1.protected_path).write_bytes(prot_docx_img)
    (chat_dir / f"{a1.id}_ocr.json").write_text("{}", encoding="utf-8")
    # allegato immagine SIGILLATA: mai nel pool
    a2 = Attachment(conv_id=conv.id, direction="in", source="upload",
                    filename="segreta.png", anonymization_status="protected",
                    sealed_json='[{"n": 1, "page": 0, "all": false, '
                                '"rect": [0, 0, 50, 50]}]')
    s.add(a2)
    s.flush()
    sealed_orig = make_photo()
    sealed_red = redact_image_bytes(sealed_orig, ".png",
                                    [((5, 5, 60, 40), "[ORG_1]")])
    a2.original_path = str(chat_dir / f"{a2.id}_segreta.png")
    a2.protected_path = str(chat_dir / f"{a2.id}_protected.png")
    Path(a2.original_path).write_bytes(sealed_orig)
    Path(a2.protected_path).write_bytes(sealed_red)
    (chat_dir / f"{a2.id}_ocr.json").write_text("{}", encoding="utf-8")
    s.commit()

    pool_db = MediaPool()
    pool_db.load_inputs(s, conv)
    check("load_inputs: la coppia dell'allegato docx c'è",
          pool_db.lookup(red_photo, ".png") == photo)
    check("load_inputs: l'allegato sigillato è ESCLUSO dal pool",
          pool_db.lookup(sealed_red, ".png") is None)
    check("load_inputs: idempotente",
          pool_db.load_inputs(s, conv) is None
          and pool_db.lookup(red_photo, ".png") == photo)

print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)

"""
Widget AcroForm che una redazione del content stream non può ripulire
(engine/pdf_export._strip_widgets): firme digitali visibili e widget
solo-aspetto.

Caso reale d'origine: un ordine d'acquisto con firma PAdES VISIBILE. Il
riquadro "Firmato digitalmente da <nome> <data> <ora> CET" sta
nell'appearance stream del widget /Sig, get_text() lo legge, il detector lo
mappa, redact_pdf piazza la casella ma apply_redactions non tocca i widget e
_scrub_widgets riscrive solo field_value (vuoto per /Sig). Esito: residuo ->
"verifica dei residui fallita". In più il dizionario di firma porta il nome in
/Name e il certificato in /Contents, invisibili a ogni controllo testuale.

Niente modello: PDF costruiti qui con PyMuPDF, mappa data a mano.

  1. FIRMA VISIBILE — il nome nell'aspetto viene redatto senza residui, il
     dizionario /Sig (Name, ByteRange, Contents) esce dal file, /SigFlags
     sparisce, il box giallo con l'etichetta resta e finisce nei boxes;
  2. FIRMA NON COLPITA — un /Sig senza valori mappati esce comunque (guscio
     di firma già rotta = solo PII); il resto della pagina è intatto;
  3. PULSANTE con caption che è un valore — widget solo-aspetto colpito:
     rimosso, nessun residuo;
  4. REGRESSIONE campo di TESTO — resta un widget, con il valore riscritto
     (il percorso _scrub_widgets non cambia);
  5. REGRESSIONE casella NON colpita — un checkbox lontano dalla redazione
     resta al suo posto;
  6. RESIDUI — _readable_text vede /V/Name di una firma: una firma
     sopravvissuta sarebbe dichiarata, non taciuta.

Esecuzione:  python pdf_widget_redact_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz

from app.engine import pdf_export
from app.engine.pdf_export import redact_pdf

NAME = "Mario Rossi"
SIGNER = "MARIO ROSSI"
DATE = "25.11.2025"
MAP = {"[FULLNAME_1]": NAME, "[DATE_1]": DATE}

_ok = _ko = 0


def check(cond, label, extra=""):
    global _ok, _ko
    if cond:
        _ok += 1
        print(f"  PASS  {label}")
    else:
        _ko += 1
        print(f"  FAIL  {label}" + (f"   [{extra}]" if extra else ""))


# --------------------------------------------------------------------------- #
# costruzione
# --------------------------------------------------------------------------- #
def _base():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Ordine di acquisto n. 12345", fontsize=12)
    page.insert_text((72, 100), f"Cliente: {NAME}", fontsize=12)
    return doc, page


def _add_sig_widget(doc, page, rect, ap_text, signer=SIGNER):
    """Campo /Sig FIRMATO (V presente: MuPDF conserva l'aspetto dato invece di
    rigenerarne uno vuoto) con l'aspetto che disegna `ap_text`."""
    sig = doc.get_new_xref()
    doc.update_object(sig, (
        "<</Type/Sig/Filter/Adobe.PPKLite/SubFilter/ETSI.CAdES.detached"
        f"/Name({signer})/M(D:20251125112616+01'00')"
        "/ByteRange[0 100 200 300]/Contents<3082DEADBEEF>>>"))
    ap = doc.get_new_xref()
    w, h = rect.width, rect.height
    doc.update_object(ap, (
        f"<</Type/XObject/Subtype/Form/BBox[0 0 {w} {h}]"
        "/Resources<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>>>"))
    doc.update_stream(ap, f"BT /F1 8 Tf 2 {h / 2:.1f} Td ({ap_text}) Tj ET".encode())
    wx = doc.get_new_xref()
    doc.update_object(wx, (
        "<</Type/Annot/Subtype/Widget/FT/Sig/T(Signature1)/F 132"
        f"/V {sig} 0 R/Rect[{rect.x0} {page.rect.height - rect.y1} "
        f"{rect.x1} {page.rect.height - rect.y0}]/P {page.xref} 0 R"
        f"/AP<</N {ap} 0 R>>>>"))
    doc.xref_set_key(page.xref, "Annots", f"[{wx} 0 R]")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm",
                     f"<</Fields[{wx} 0 R]/SigFlags 3>>")


def _reopen(doc):
    data = doc.tobytes()
    doc.close()
    return data


def _text(pdf_bytes):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        return "\n".join(p.get_text() for p in d)


def _widgets(pdf_bytes):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        return [(w.field_type_string, w.field_name, w.field_value)
                for p in d for w in p.widgets()]


def _sigflags(pdf_bytes):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        return d.xref_get_key(d.pdf_catalog(), "AcroForm/SigFlags")


# --------------------------------------------------------------------------- #
# 1. firma visibile colpita
# --------------------------------------------------------------------------- #
print("[1] firma digitale visibile con nome e data nell'aspetto")
doc, page = _base()
SIG_RECT = fitz.Rect(72, 500, 292, 540)
_add_sig_widget(doc, page, SIG_RECT,
                f"Firmato digitalmente da {NAME} {DATE} 11:26:16 CET")
src = _reopen(doc)
check(NAME in _text(src) and DATE in _text(src),
      "precondizione: l'aspetto della firma è testo leggibile")
check(SIGNER.encode() in src and b"/ByteRange" in src,
      "precondizione: il dizionario /Sig porta nome e ByteRange")

out, rep = redact_pdf(src, dict(MAP))
check(rep["residual"] == [], "nessun residuo", str(rep["residual"]))
check(rep["by_placeholder"]["[FULLNAME_1]"] == 2,
      "il nome è redatto due volte (corpo + firma)",
      str(rep["by_placeholder"]))
check(rep["signatures"] == 1, "report: 1 firma rimossa", str(rep["signatures"]))
txt = _text(out)
check(NAME not in txt and DATE not in txt and "11:26:16" not in txt,
      "nome, data e ora della firma non sono più nel testo")
check(SIGNER.encode() not in out and b"/ByteRange" not in out
      and b"DEADBEEF" not in out,
      "dizionario /Sig fuori dal file (Name, ByteRange, Contents)")
check(_widgets(out) == [], "nessun widget nell'output", str(_widgets(out)))
check(_sigflags(out)[0] == "null", "/SigFlags rimosso", str(_sigflags(out)))
in_sig = [b for b in rep["boxes"].get(0, [])
          if fitz.Rect(b["x0"], b["y0"], b["x1"], b["y1"]).intersects(SIG_RECT)]
check(len(in_sig) >= 2, "i box della firma (nome + data) sono nei boxes",
      str(len(in_sig)))
check("[FULLNAME_1]" in txt and "[DATE_1]" in txt,
      "le etichette dei placeholder sono leggibili in pagina")
check("Ordine di acquisto n. 12345" in txt, "il resto della pagina è intatto")

# --------------------------------------------------------------------------- #
# 2. firma non colpita
# --------------------------------------------------------------------------- #
print("[2] firma digitale senza valori mappati nell'aspetto")
doc, page = _base()
_add_sig_widget(doc, page, fitz.Rect(72, 500, 292, 540),
                "Documento firmato digitalmente", signer="ALTRO FIRMATARIO")
src = _reopen(doc)
out, rep = redact_pdf(src, {"[FULLNAME_1]": NAME})
check(rep["residual"] == [], "nessun residuo")
check(rep["signatures"] == 1, "firma rimossa comunque")
check(b"ALTRO FIRMATARIO" not in out, "il nome nel dizionario /Sig è uscito")
check("Documento firmato digitalmente" not in _text(out),
      "l'aspetto della firma non è più in pagina")
check("Ordine di acquisto n. 12345" in _text(out), "il resto è intatto")

# --------------------------------------------------------------------------- #
# 3. pulsante con caption
# --------------------------------------------------------------------------- #
print("[3] pulsante la cui caption è un valore della mappa")
doc, page = _base()
w = fitz.Widget()
w.field_type = fitz.PDF_WIDGET_TYPE_BUTTON
w.field_name = "btn"
w.rect = fitz.Rect(72, 300, 300, 320)
w.button_caption = NAME
page.add_widget(w)
src = _reopen(doc)
check(_text(src).count(NAME) == 2, "precondizione: la caption è testo leggibile")
out, rep = redact_pdf(src, {"[FULLNAME_1]": NAME})
check(rep["residual"] == [], "nessun residuo", str(rep["residual"]))
check(rep["widgets_removed"] == 1, "report: 1 widget solo-aspetto rimosso",
      str(rep["widgets_removed"]))
check(NAME not in _text(out), "la caption non è più leggibile")
check(_widgets(out) == [], "il pulsante è uscito", str(_widgets(out)))

# --------------------------------------------------------------------------- #
# 4. regressione campo di testo
# --------------------------------------------------------------------------- #
print("[4] campo di testo: valore riscritto, widget conservato")
doc, page = _base()
w = fitz.Widget()
w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
w.field_name = "cliente"
w.rect = fitz.Rect(72, 300, 300, 320)
w.field_value = NAME
page.add_widget(w)
src = _reopen(doc)
out, rep = redact_pdf(src, {"[FULLNAME_1]": NAME})
check(rep["residual"] == [], "nessun residuo", str(rep["residual"]))
ws = _widgets(out)
check(len(ws) == 1 and ws[0][0] == "Text", "il campo di testo resta", str(ws))
check(ws and ws[0][2] == "[FULLNAME_1]", "con il placeholder come valore", str(ws))
check(rep["widgets"] == 1 and rep["widgets_removed"] == 0
      and rep["signatures"] == 0, "report coerente",
      f"{rep['widgets']} {rep['widgets_removed']} {rep['signatures']}")

# --------------------------------------------------------------------------- #
# 5. regressione checkbox lontano
# --------------------------------------------------------------------------- #
print("[5] checkbox non colpito dalla redazione")
doc, page = _base()
w = fitz.Widget()
w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
w.field_name = "accetto"
w.rect = fitz.Rect(72, 400, 90, 418)
w.field_value = True
page.add_widget(w)
src = _reopen(doc)
out, rep = redact_pdf(src, {"[FULLNAME_1]": NAME})
ws = _widgets(out)
check(len(ws) == 1 and ws[0][0] == "CheckBox", "il checkbox resta", str(ws))
check(rep["widgets_removed"] == 0, "nessun widget rimosso")

# --------------------------------------------------------------------------- #
# 6. il controllo residui vede il dizionario di firma
# --------------------------------------------------------------------------- #
print("[6] _readable_text legge /V/Name dei campi firma")
doc, page = _base()
_add_sig_widget(doc, page, fitz.Rect(72, 500, 292, 540), "firma", signer=SIGNER)
src = _reopen(doc)
with fitz.open(stream=src, filetype="pdf") as d:
    readable = pdf_export._readable_text(d)
check(SIGNER in readable, "il nome del firmatario è tra le superfici leggibili")

print(f"\n{_ok} PASS, {_ko} FAIL")
sys.exit(1 if _ko else 0)

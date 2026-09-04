"""
Testo FANTASMA dei PDF (engine/pdf_ghost.py): la trascrizione OCR invisibile
che le scansioni "cercabili" portano sopra i propri pixel.

Niente modello e niente OCR: i PDF sono costruiti qui con PyMuPDF, quindi la
batteria è veloce e deterministica. Si verificano tre famiglie di cose:

  1. DISCRIMINAZIONE — lo strip deve scattare SOLO dove serve. I casi sono
     quelli che si incontrano davvero: testo puro, testo con immagini dentro,
     scansione cercabile, pagina MISTA (documento di testo con una scansione
     incollata che si porta la propria trascrizione), scansione con un timbro
     visibile sopra, scansione nuda;
  2. INTANGIBILITÀ del testo VISIBILE — non deve perdersi un carattere, mai,
     in nessuno dei casi. È la garanzia che i PDF da leggere come testo non si
     rompono;
  3. INTEGRAZIONE — extract_text non restituisce il fantasma e alza un errore
     dedicato quando resta a mani vuote; redact_pdf non lascia il fantasma
     nell'output (sarebbe una fuga di PII: stringhe storpiate che nessun
     controllo vede); text_in_rect non lo restituisce alla selezione manuale.

Esecuzione:  python pdf_ghost_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz

from app.engine import pdf as pdf_mod
from app.engine import pdf_ghost
from app.engine.pdf_export import PdfError, redact_pdf

INVISIBLE = pdf_ghost.INVISIBLE

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
# materiale: una finta "scansione" (un PNG di testo reso da PyMuPDF)
# --------------------------------------------------------------------------- #
BODY = ("Contratto di fornitura tra Acme Forniture SRL, con sede in Via "
        "Roma 10, 20121 Milano (MI), e la controparte. L'accordo entra "
        "in vigore alla data della firma e resta valido ventiquattro mesi. ") * 5

# la trascrizione dello scanner: STORPIATA, come nella realtà
TRANSCRIPT = ("Contratto di fornitura tra Acme Fornlture SRL, con sede in Via "
              "Roma l0, 2012l Miiano (MI), e la controparte.")


def _scan_png():
    """Immagine che imita una pagina scansionata."""
    d = fitz.open()
    p = d.new_page()
    p.insert_textbox(fitz.Rect(40, 40, 555, 780), BODY, fontsize=11)
    png = p.get_pixmap(dpi=96).tobytes("png")
    d.close()
    return png


SCAN = _scan_png()


def _write_invisible(page, rect, text, fontsize=11):
    tw = fitz.TextWriter(page.rect)
    tw.fill_textbox(rect, text, fontsize=fontsize)
    tw.write_text(page, render_mode=INVISIBLE)


def caso_testo_puro():
    d = fitz.open(); p = d.new_page()
    p.insert_textbox(fitz.Rect(50, 50, 545, 750), BODY, fontsize=11)
    return d.tobytes()


def caso_testo_con_immagini():
    d = fitz.open(); p = d.new_page()
    p.insert_textbox(fitz.Rect(50, 50, 545, 380), BODY, fontsize=11)
    p.insert_image(fitz.Rect(120, 400, 470, 740), stream=SCAN)
    return d.tobytes()


def caso_sandwich():
    d = fitz.open(); p = d.new_page()
    p.insert_image(p.rect, stream=SCAN)
    _write_invisible(p, fitz.Rect(50, 50, 545, 750), TRANSCRIPT)
    return d.tobytes()


def caso_misto():
    """Il caso insidioso: testo VERO + una scansione incollata dentro che si
    porta la PROPRIA trascrizione invisibile. Entrambi i canali sulla stessa
    pagina: la decisione deve essere per regione, non per pagina."""
    d = fitz.open(); p = d.new_page()
    p.insert_textbox(fitz.Rect(50, 50, 545, 380), BODY, fontsize=11)
    img = fitz.Rect(120, 400, 470, 740)
    p.insert_image(img, stream=SCAN)
    _write_invisible(p, img + (5, 5, -5, -5), TRANSCRIPT, fontsize=9)
    return d.tobytes()


def caso_sandwich_con_timbro():
    """Scansione cercabile + qualcosa di VISIBILE stampato sopra (numero di
    pagina, protocollo, firma digitale). La prima versione della regola,
    basata sulla PROPORZIONE fra invisibile e visibile, cadeva proprio qui."""
    d = fitz.open(); p = d.new_page()
    p.insert_image(p.rect, stream=SCAN)
    _write_invisible(p, fitz.Rect(50, 50, 545, 750), TRANSCRIPT)
    p.insert_text((280, 800), "pag. 1 di 1", fontsize=9)
    p.insert_text((50, 800), "Prot. 2024/00417 del 06/06/2024", fontsize=9)
    return d.tobytes()


def caso_scansione_nuda():
    d = fitz.open(); p = d.new_page()
    p.insert_image(p.rect, stream=SCAN)
    return d.tobytes()


def caso_invisibile_senza_immagine():
    """Testo invisibile che NON sta sopra pixel: non è una trascrizione, e
    non lo si tocca (la condizione 'sopra un'immagine' è necessaria)."""
    d = fitz.open(); p = d.new_page()
    p.insert_textbox(fitz.Rect(50, 50, 545, 400), BODY, fontsize=11)
    _write_invisible(p, fitz.Rect(50, 450, 545, 700), TRANSCRIPT)
    return d.tobytes()


def caso_multipagina():
    """Sei pagine sandwich, come il file che ha fatto emergere il problema."""
    d = fitz.open()
    for _ in range(6):
        p = d.new_page()
        p.insert_image(p.rect, stream=SCAN)
        _write_invisible(p, fitz.Rect(50, 50, 545, 750), TRANSCRIPT)
    return d.tobytes()


CASI = [
    ("testo puro",                    caso_testo_puro,             False),
    ("testo vero + immagini dentro",  caso_testo_con_immagini,     False),
    ("scansione cercabile",           caso_sandwich,               True),
    ("MISTO testo + scansione",       caso_misto,                  True),
    ("scansione + timbro visibile",   caso_sandwich_con_timbro,    True),
    ("scansione nuda",                caso_scansione_nuda,         False),
    ("invisibile NON su immagine",    caso_invisibile_senza_immagine, False),
    ("sandwich 6 pagine",             caso_multipagina,            True),
]


def visible_text(pdf_bytes):
    """Solo il testo disegnato VISIBILE, letto dal tracciato."""
    out = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            for sp in page.get_texttrace():
                if sp.get("type") != INVISIBLE:
                    out.append("".join(chr(c[0]) for c in sp.get("chars", ())))
    return "".join(out)


def all_text(pdf_bytes):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return "".join(p.get_text() for p in doc)


# --------------------------------------------------------------------------- #
print("=" * 72)
print("1. DISCRIMINAZIONE: lo strip scatta solo dove serve")
print("=" * 72)
casi_out = {}
for nome, fn, atteso in CASI:
    data = fn()
    out, touched = pdf_ghost.strip_ghost_text(data)
    casi_out[nome] = (data, out)
    check(bool(touched) == atteso, f"{nome}: strip={bool(touched)} (atteso {atteso})",
          f"touched={touched}")
    if not atteso:
        check(out is data, f"{nome}: file NON riscritto (bytes identici)")

print()
print("=" * 72)
print("2. Il testo VISIBILE non si perde mai")
print("=" * 72)
for nome, (before, after) in casi_out.items():
    vb, va = visible_text(before), visible_text(after)
    check(vb == va, f"{nome}: visibile intatto ({len(vb)} char)",
          f"{len(vb)} -> {len(va)}")

print()
print("=" * 72)
print("3. Il fantasma esce davvero dal file")
print("=" * 72)
for nome in ("scansione cercabile", "MISTO testo + scansione",
             "scansione + timbro visibile", "sandwich 6 pagine"):
    before, after = casi_out[nome]
    check("Fornlture" in all_text(before),
          f"{nome}: il fantasma è estraibile finché non si toglie")
    check("Fornlture" not in all_text(after),
          f"{nome}: dopo lo strip non si estrae più")

# nel misto il testo vero deve restare estraibile
before, after = casi_out["MISTO testo + scansione"]
check("Acme Forniture SRL" in all_text(after),
      "MISTO: il testo VERO resta estraibile")
# nel sandwich col timbro il timbro deve restare
before, after = casi_out["scansione + timbro visibile"]
check("Prot. 2024/00417" in all_text(after),
      "timbro visibile: sopravvive allo strip")

print()
print("=" * 72)
print("4. extract_text non vede il fantasma")
print("=" * 72)
text, n = pdf_mod.extract_text(caso_sandwich(), allow_empty=True)
check(not text.strip(), "sandwich: extract_text torna vuoto (serve l'OCR)",
      repr(text[:80]))
check(n == 1, "sandwich: il numero di pagine è giusto")

try:
    pdf_mod.extract_text(caso_sandwich(), allow_empty=False)
    check(False, "sandwich senza OCR: deve alzare PdfError")
except PdfError as e:
    check("OCR" in str(e) and "nascosto" in str(e),
          "sandwich senza OCR: errore DEDICATO che parla del testo nascosto",
          str(e)[:90])

text, _ = pdf_mod.extract_text(caso_misto(), allow_empty=True)
check("Acme Forniture SRL" in text and "Prodetti" not in text,
      "MISTO: extract_text dà il testo vero e non il fantasma")

text, _ = pdf_mod.extract_text(caso_testo_puro(), allow_empty=False)
check("Acme Forniture SRL" in text, "testo puro: invariato")

try:
    pdf_mod.extract_text(caso_scansione_nuda(), allow_empty=False)
    check(False, "scansione nuda: deve alzare PdfError")
except PdfError as e:
    check("nascosto" not in str(e),
          "scansione nuda: messaggio di SEMPRE, non quello del fantasma",
          str(e)[:90])

print()
print("=" * 72)
print("5. redact_pdf non lascia il fantasma nell'output (sarebbe una fuga di PII)")
print("=" * 72)
# la mappa è quella che nascerebbe dall'OCR: valori CORRETTI, che nel
# fantasma sono storpiati e quindi non verrebbero mai trovati alla lettera
mapping = {"[ORG_1]": "Acme Forniture SRL", "[CITY_1]": "Milano"}
for nome, fn in (("sandwich", caso_sandwich), ("misto", caso_misto)):
    src = fn()
    out, report = redact_pdf(src, mapping)
    resid = all_text(out)
    check("Prodetti" not in resid,
          f"{nome}: la lettura storpiata NON è più estraibile dall'output")
    check("Miiano" not in resid,
          f"{nome}: nemmeno la città storpiata")
    check(report["residual"] == [], f"{nome}: report['residual'] vuoto",
          str(report["residual"]))
# nel misto la redazione del testo vero deve comunque essere avvenuta
out, report = redact_pdf(caso_misto(), mapping)
check(report["occurrences"] > 0, "misto: il testo vero è stato redatto",
      str(report["occurrences"]))
check("Acme Forniture SRL" not in all_text(out),
      "misto: il valore vero non è più estraibile")

print()
print("=" * 72)
print("6. text_in_rect (selezione manuale) non restituisce il fantasma")
print("=" * 72)
sel = [40, 40, 560, 300]
got = pdf_mod.text_in_rect(caso_sandwich(), 0, sel)
check(not got.strip(), "sandwich: selezione vuota -> il chiamante passa all'OCR",
      repr(got[:80]))
got = pdf_mod.text_in_rect(caso_testo_puro(), 0, sel)
check("Forniture" in got, "testo puro: la selezione restituisce il testo",
      repr(got[:60]))
got = pdf_mod.text_in_rect(caso_misto(), 0, [40, 40, 560, 380])
check("Forniture" in got and "Fornlture" not in got,
      "misto: la selezione sul testo vero funziona", repr(got[:60]))
got = pdf_mod.text_in_rect(caso_misto(), 0, [120, 400, 470, 740])
check(not got.strip(),
      "misto: la selezione sulla scansione è vuota -> fallback OCR",
      repr(got[:80]))

print()
print("=" * 72)
print("7. Lo strip è idempotente e non tocca i pixel")
print("=" * 72)
once, t1 = pdf_ghost.strip_ghost_text(caso_sandwich())
twice, t2 = pdf_ghost.strip_ghost_text(once)
check(not t2, "seconda passata: niente più da togliere")
check(twice is once, "seconda passata: file non riscritto")

orig = caso_sandwich()
stripped, _ = pdf_ghost.strip_ghost_text(orig)
with fitz.open(stream=orig, filetype="pdf") as a, \
        fitz.open(stream=stripped, filetype="pdf") as b:
    pa = a[0].get_pixmap(dpi=72).tobytes("png")
    pb = b[0].get_pixmap(dpi=72).tobytes("png")
check(pa == pb, "la pagina RESA è identica al pixel prima e dopo")

print()
print("=" * 72)
print("8. ghost_report: diagnosi senza modificare")
print("=" * 72)
rows = pdf_ghost.ghost_report(caso_multipagina())
check(len(rows) == 6, "sei righe per sei pagine")
check(all(r["ghost_chars"] > 0 for r in rows), "fantasma su tutte le pagine")
rows = pdf_ghost.ghost_report(caso_testo_puro())
check(rows[0]["ghost_chars"] == 0 and rows[0]["vis_chars"] > 0,
      "testo puro: nessun fantasma, testo visibile contato")

print()
print("=" * 72)
print("9. File illeggibili: lo strip si fa da parte, non ruba il messaggio")
print("=" * 72)
# Lo strip gira PRIMA che extract_text possa controllare la password: se
# sollevasse, l'utente perderebbe i messaggi buoni ("PDF protetto", "file
# danneggiato") in favore di un'eccezione grezza.
spazzatura = b"%PDF-1.4 questo non e' un PDF"
out, touched = pdf_ghost.strip_ghost_text(spazzatura)
check(out is spazzatura and not touched,
      "PDF danneggiato: byte restituiti intatti, nessuna eccezione")
try:
    pdf_mod.extract_text(spazzatura)
    check(False, "PDF danneggiato: extract_text deve alzare PdfError")
except PdfError as e:
    check("danneggiato" in str(e).lower() or "non valido" in str(e).lower(),
          "PDF danneggiato: messaggio di sempre", str(e)[:70])

d = fitz.open()
p = d.new_page()
p.insert_image(p.rect, stream=SCAN)
_write_invisible(p, fitz.Rect(50, 50, 545, 750), TRANSCRIPT)
protetto = d.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,
                     owner_pw="o", user_pw="u")
d.close()
out, touched = pdf_ghost.strip_ghost_text(protetto)
check(out is protetto and not touched,
      "PDF protetto da password: byte intatti, nessuna eccezione")
try:
    pdf_mod.extract_text(protetto)
    check(False, "PDF protetto: extract_text deve alzare PdfError")
except PdfError as e:
    check("password" in str(e).lower(),
          "PDF protetto: messaggio sulla password conservato", str(e)[:70])

# nota: text_in_rect su byte corrotti solleva, ma lo faceva anche prima (apre
# il documento senza rete) e riceve sempre un'anteprima prodotta dall'app:
# non è un caso di questa modifica e non lo si asserisce qui.

print()
print("=" * 72)
print("10. Pagine RUOTATE (gli scanner le producono per davvero)")
print("=" * 72)
# Il dubbio da sciogliere è il sistema di coordinate: i bbox di
# get_texttrace()/get_image_info() e quelli di get_text("words") devono
# restare confrontabili anche con /Rotate diverso da 0, altrimenti il
# fantasma non verrebbe riconosciuto (o, peggio, si toglierebbe testo vero).
for rot in (0, 90, 180, 270):
    d = fitz.open()
    p = d.new_page()
    p.insert_image(p.rect, stream=SCAN)
    _write_invisible(p, fitz.Rect(50, 50, 545, 750), TRANSCRIPT)
    p.set_rotation(rot)
    data = d.tobytes()
    with fitz.open(stream=data, filetype="pdf") as tmp:
        full = list(tmp[0].rect)
    d.close()

    out, touched = pdf_ghost.strip_ghost_text(data)
    check(bool(touched), f"rot {rot}: il fantasma viene riconosciuto")
    check("Prodetti" not in all_text(out), f"rot {rot}: e non è più estraibile")
    check(not pdf_mod.text_in_rect(data, 0, full).strip(),
          f"rot {rot}: la selezione resta vuota -> fallback OCR")

print()
print("=" * 72)
print(f"RISULTATO: {_ok} PASS, {_ko} FAIL")
print("=" * 72)
sys.exit(1 if _ko else 0)

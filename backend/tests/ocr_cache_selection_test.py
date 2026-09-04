"""Selezione manuale letta dalla CACHE OCR (image_ocr.text_in_rect_cache).

Il caso reale: una scansione salvata "di lato" (raster ruotato di 90°, rimesso
dritto dal PDF) con "P.IVA n. 01647730157" in testata. All'invio l'OCR a pagina
intera mette in cache "n. 01647730157"; l'OCR del ritaglio, sulla stessa area,
leggeva "n.01647730157" e anonymize_text lo respingeva come "non presente":
stesso motore, immagine diversa, spaziatura diversa. Qui si verifica che la
selezione restituisca il testo DELLA CACHE — che per costruzione _value_pattern
ritrova — su tre geometrie: immagine ruotata (righe verticali), immagine
dritta, pagina vettoriale (render a PAGE_DPI). Tutto sintetico: niente OCR,
niente modello, nessun documento reale.

Uso:
    python backend/tests/ocr_cache_selection_test.py
"""
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import fitz                                                      # noqa: E402

from app.engine import image_ocr                                 # noqa: E402
from app.engine.pdf_export import _value_pattern                 # noqa: E402

PASS = 0
FAIL = 0
HEADER = "Via Cavour, 22- C.F. e P.IVA n. 01647730157"   # identità inventata
CLIP_READ = "n.01647730157"                                # la lettura del ritaglio


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def _jpeg(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (250, 250, 250)).save(buf, format="JPEG")
    return buf.getvalue()


def _pdf_with_image(w, h, rotate):
    """Pagina A4 con UN'immagine w x h a tutta pagina; con rotate=True
    l'immagine è piazzata ruotata di 90° (come una scansione "di lato")."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=841)
    page.insert_image(page.rect, stream=_jpeg(w, h), rotate=90 if rotate else 0)
    xref = page.get_images(full=True)[0][0]
    out = doc.tobytes()
    doc.close()
    return out, xref


def _to_page(pdf, xref, w, h, px):
    """Il rettangolo pixel `px` dell'immagine riportato in punti pagina con la
    stessa matrice che il PDF usa per piazzarla: è ciò che l'utente
    'vede' e seleziona nell'anteprima."""
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        info = [i for i in doc[0].get_image_info(xrefs=True)
                if i["xref"] == xref][0]
        M = fitz.Matrix(*info["transform"])
    x0, y0, x1, y1 = px
    pts = [fitz.Point(x / w, y / h) * M           # y-down, come PyMuPDF
           for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    return (min(p.x for p in pts), min(p.y for p in pts),
            max(p.x for p in pts), max(p.y for p in pts))


def _cache(key, w, h, line_box, extra=()):
    lines = [{"t": HEADER, "s": 0.93, "b": list(line_box)}]
    lines += [{"t": t, "s": 0.9, "b": list(b)} for t, b in extra]
    img = {"key": key, "ext": "jpeg", "w": w, "h": h, "lines": lines,
           "regions": [], "plan": []}
    if str(key).startswith(image_ocr.PAGE_KEY) or str(key).isdigit():
        img["page"] = 0
    return {"v": 1, "images": [img]}


def _tail(box, frac, horiz):
    """Sotto-box che prende la coda della riga (dal `frac` in poi) lungo
    l'asse di lettura: la selezione tipica 'solo il numero'."""
    x0, y0, x1, y1 = box
    if horiz:
        return (x0 + (x1 - x0) * frac, y0, x1, y1)
    return (x0, y0 + (y1 - y0) * frac, x1, y1)


def main():
    corpus_ok = _value_pattern(CLIP_READ).search(HEADER)
    check("premessa: la lettura del ritaglio NON matcha la cache",
          corpus_ok is None, "(è il bug a monte: spaziatura diversa)")

    # --- 1. immagine ruotata di 90°: righe VERTICALI nel raster --------------
    w, h = 2338, 1653
    pdf, xref = _pdf_with_image(w, h, rotate=True)
    line = (2046, 276, 2089, 726)               # 43 px larga, 450 alta
    neigh = ("Sede Amministrativa: 20063 Cernusco", (2069, 276, 2112, 808))
    cache = _cache(str(xref), w, h, line, extra=[neigh])
    full = _to_page(pdf, xref, w, h, line)
    got = image_ocr.text_in_rect_cache(pdf, 0, full, cache)
    check("ruotata: riga intera -> testo della cache", got == HEADER, repr(got))
    sel = _to_page(pdf, xref, w, h, _tail(line, 0.68, horiz=False))
    got = image_ocr.text_in_rect_cache(pdf, 0, sel, cache)
    check("ruotata: coda della riga -> solo la P.IVA (spaziatura della cache)",
          got in ("n. 01647730157", "01647730157"), repr(got))
    check("il testo restituito si ritrova nella cache con _value_pattern",
          bool(got) and _value_pattern(got).search(HEADER) is not None, repr(got))
    check("la riga accanto non entra nella selezione",
          "Cernusco" not in (got or ""), repr(got))
    check("in_cache: vero per la lettura della cache, falso per quella del ritaglio",
          image_ocr.in_cache(got, cache) and not image_ocr.in_cache(CLIP_READ, cache))

    # --- 2. immagine dritta: righe orizzontali ----------------------------------
    w2, h2 = 1653, 2338
    pdf2, xref2 = _pdf_with_image(w2, h2, rotate=False)
    line2 = (276, 2046, 726, 2089)
    cache2 = _cache(str(xref2), w2, h2, line2)
    sel2 = _to_page(pdf2, xref2, w2, h2, _tail(line2, 0.68, horiz=True))
    got2 = image_ocr.text_in_rect_cache(pdf2, 0, sel2, cache2)
    check("dritta: coda della riga -> P.IVA", got2 in ("n. 01647730157", "01647730157"),
          repr(got2))

    # --- 3. pagina vettoriale: render intero a PAGE_DPI -------------------------
    doc = fitz.open()
    doc.new_page(width=595, height=841)
    pdf3 = doc.tobytes()
    doc.close()
    s = image_ocr.PAGE_DPI / 72.0
    line3 = (100 * s, 90 * s, 260 * s, 105 * s)                # px del render
    cache3 = _cache(f"{image_ocr.PAGE_KEY}0", round(595 * s), round(841 * s), line3)
    got3 = image_ocr.text_in_rect_cache(pdf3, 0, (100 + 160 * 0.68, 90, 260, 105), cache3)
    check("vettoriale: coda della riga -> P.IVA", got3 in ("n. 01647730157", "01647730157"),
          repr(got3))

    # --- 4. contratto None / "" -------------------------------------------------
    check("selezione fuori da ogni immagine -> None (fallback al ritaglio)",
          image_ocr.text_in_rect_cache(pdf3, 0, (10, 700, 50, 720),
                                       _cache(str(xref), w, h, line)) is None)
    check("cache assente -> None", image_ocr.text_in_rect_cache(pdf, 0, full, None) is None)
    empty = image_ocr.text_in_rect_cache(pdf, 0, _to_page(pdf, xref, w, h, (10, 10, 60, 60)), cache)
    check("area coperta ma senza parole -> stringa vuota", empty == "", repr(empty))

    # --- 5. redazione nei pixel della riga VERTICALE (bug gemello) --------------
    # Il valore anonimizzato dalla selezione deve produrre un box lungo
    # l'asse di lettura della riga (Y nel raster ruotato), non una
    # strisciolina lungo X; e l'overlay nell'anteprima deve cadere dove sta
    # la testata sulla pagina.
    mapping = {"[CUSTOM_1]": "n. 01647730157"}
    (bx0, by0, bx1, by1), ph = image_ocr.boxes_for(cache, mapping)[str(xref)][0]
    check("box px lungo l'asse di lettura (tutta la larghezza della riga, coda in Y)",
          bx0 == 2046 and bx1 == 2089 and by0 > 276 + 0.6 * 450 and by1 == 726,
          f"{(bx0, by0, bx1, by1)}")
    _out, overlay, by_ph = image_ocr.redact_pdf_images(pdf, cache, mapping)
    ob = overlay[0][0]
    exp = _to_page(pdf, xref, w, h, (bx0, by0, bx1, by1))
    check("overlay nell'anteprima sulla posizione della testata",
          all(abs(ob[k] - v) < 0.5 for k, v in zip(("x0", "y0", "x1", "y1"), exp)),
          f"{ob} vs {exp}")
    check("overlay dentro l'area della riga sulla pagina",
          ob["x0"] >= full[0] - 0.5 and ob["x1"] <= full[2] + 0.5
          and ob["y0"] >= full[1] - 0.5 and ob["y1"] <= full[3] + 0.5, f"{ob} in {full}")
    check("by_ph conta il box", by_ph.get("[CUSTOM_1]") == 1, str(by_ph))
    # riga orizzontale: comportamento di prima, inalterato
    (hx0, hy0, hx1, hy1), _ph = image_ocr.boxes_for(cache2, mapping)[str(xref2)][0]
    check("riga orizzontale: box lungo X (invariato)",
          hy0 == 2046 and hy1 == 2089 and hx0 > 276 + 0.6 * 450 and hx1 == 726,
          f"{(hx0, hy0, hx1, hy1)}")

    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

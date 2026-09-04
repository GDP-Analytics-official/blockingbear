"""OCR delle immagini: testo a filo del bordo e ordine di lettura.

Il caso reale: il LOGO in testata di un documento è un'immagine piccola in cui
la scritta grande (il nome dell'ente) sta a filo del bordo superiore, con una
riga di testo piccolo sotto. Il rilevatore non chiude una regione che tocca il
bordo del fotogramma: la cache OCR conteneva la riga piccola ma non la scritta
grande, mentre il ritaglio della selezione manuale (renderizzato con margini)
la leggeva. Risultato: la UI mostrava il nome selezionato, l'anonimizzazione
copriva le occorrenze testuali e quella piccola nel logo, e la grande restava
in chiaro. build_cache incornicia ora ogni immagine prima dell'OCR.

Qui l'immagine è SINTETICA (una parola grande a filo del bordo, una riga
piccola sotto), quindi serve lo stack OCR vero; la parte sull'ordine di
lettura non lo richiede.

Uso:
    python backend/tests/ocr_logo_edge_test.py
"""
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import fitz                                                      # noqa: E402

from app.engine import image_ocr                                 # noqa: E402

PASS = 0
FAIL = 0
BIG = "Meridiana"                      # nome inventato: la scritta del "logo"
SMALL = "Fondazione Ospedaliera Meridiana Onlus"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def _logo_png():
    """Immagine tipo logo (~400x90): la parola grande TOCCA il bordo alto,
    la riga piccola sta sotto con il suo margine. Si disegna con MuPDF (font
    incorporato: nessuna dipendenza da font di sistema) e si RITAGLIA il
    render al bbox del testo grande, così il bordo cade sui glifi."""
    from PIL import Image
    doc = fitz.open()
    page = doc.new_page(width=300, height=120)
    page.insert_text((10, 60), BIG, fontsize=48, fontname="helv",
                     color=(0.1, 0.2, 0.5))
    page.insert_text((10, 80), SMALL, fontsize=11, fontname="helv",
                     color=(0.1, 0.2, 0.5))
    png = page.get_pixmap(dpi=144).tobytes("png")
    doc.close()
    with Image.open(io.BytesIO(png)) as im:
        im = im.convert("RGB")
        # bbox dell'inchiostro: il ritaglio parte esattamente dalla riga
        # più alta dei glifi grandi (ascendenti), come nei loghi veri
        bbox = Image.eval(im.convert("L"), lambda v: 255 if v < 200 else 0).getbbox()
        x0, y0, x1, y1 = bbox
        im = im.crop((max(x0 - 6, 0), y0, min(x1 + 6, im.width), min(y1 + 4, im.height)))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue(), im.size


def main():
    # --- ordine di lettura: nessun OCR ------------------------------------------
    words = [(50.5, 14, 84, "Policlinico"), (49.0, 18, 238, "Agostino"),
             (51.0, 13, 6, "Fondazione"), (52.0, 13, 155, "Universitario"),
             (74.0, 13, 75, "Cattolica"), (73.5, 13, 7, "Universita"),
             (20.0, 50, 1, "Grande")]
    got = image_ocr._reading_order(words)
    check("parole di una riga con y0 diverse di pochi px restano in ordine",
          got == "Grande Fondazione Policlinico Universitario Agostino "
                 "Universita Cattolica", repr(got))

    if not image_ocr.available():
        print("  SKIP  stack OCR non installato: salto la parte con l'OCR vero")
        return
    png, (w, h) = _logo_png()
    cache = image_ocr.build_cache([{"key": "17", "ext": "png", "data": png}])
    lines = (cache or {"images": [{"lines": []}]})["images"][0]["lines"]
    texts = [l["t"] for l in lines]
    print(f"  righe OCR ({w}x{h}px): {texts}")
    big = [l for l in lines if l["t"].strip().casefold() == BIG.casefold()
           and (l["b"][3] - l["b"][1]) > 0.3 * h]
    check("la scritta grande a filo del bordo entra in cache", bool(big), repr(texts))
    check("la riga piccola sotto resta letta",
          any("Ospedaliera" in t for t in texts), repr(texts))
    for l in lines:
        b = l["b"]
        check(f"box entro l'immagine: {l['t']!r}",
              0 <= b[0] <= b[2] <= w and 0 <= b[1] <= b[3] <= h, repr(b))
    if big:
        check("il box grande parte dal bordo alto (cornice sottratta)",
              big[0]["b"][1] <= 3, repr(big[0]["b"]))
    boxes = image_ocr.boxes_for(cache, {"[ORG_1]": BIG}) if cache else {}
    got = boxes.get("17", [])
    check("boxes_for copre ENTRAMBE le occorrenze (grande e nella riga piccola)",
          len(got) >= 2 and any((b[3] - b[1]) > 0.3 * h for b, _ph in got),
          repr(got))
    check("in_cache: il nome letto dalla selezione è copribile nei pixel",
          image_ocr.in_cache(BIG, cache))


if __name__ == "__main__":
    main()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

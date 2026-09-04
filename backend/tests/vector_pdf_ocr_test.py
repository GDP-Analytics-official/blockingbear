"""Test del percorso PAGINE VETTORIALI dell'OCR PDF: pagine senza layer
testuale e senza immagini, col testo stampato come tracciati (Microsoft
Print To PDF e simili). OCR e detector PII finti e deterministici: si prova
la MECCANICA (classificazione, enumerazione, piano, redazione distruttiva),
non la qualità di lettura — quella è del percorso OCR già testato.

Esecuzione:  python backend/tests/vector_pdf_ocr_test.py
"""

import io
import re
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import fitz                                   # noqa: E402

from app.engine import image_ocr              # noqa: E402
from app.engine.pdf import anonymize_pdf, rebuild_pdf  # noqa: E402

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


def _png(w, h, color=(200, 30, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _test_pdf():
    """4 pagine: 0 = VETTORIALE (tracciati + logo incorporato), 1 = testuale,
    2 = bianca, 3 = scansione classica (immagine a tutta pagina)."""
    doc = fitz.open()
    p0 = doc.new_page(width=595, height=842)
    for i in range(300):
        y = 40.0 + (i % 100) * 7
        p0.draw_bezier((50, y), (200, y + 3), (350, y - 3), (500, y))
    p0.insert_image(fitz.Rect(50, 750, 114, 814), stream=_png(64, 64))
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((72, 100), "Contratto con Mario Rossi", fontsize=12)
    doc.new_page(width=595, height=842)
    p3 = doc.new_page(width=595, height=842)
    p3.insert_image(p3.rect, stream=_png(600, 850, (240, 240, 240)))
    out = doc.tobytes()
    doc.close()
    return out


class FakeOCR:
    """Due righe fisse su qualunque immagine: una leggibile con PII, una a
    bassa confidenza (destinata a [UNREADABLE_n])."""

    def __call__(self, arr):
        # in proporzione all'array ricevuto (come gli altri fake): build_cache
        # incornicia l'immagine prima dell'OCR e sottrae la cornice ai box,
        # coordinate fisse vicine al bordo finirebbero fuori dall'immagine
        h, w = arr.shape[0], arr.shape[1]

        def band(y0, y1):
            return [[w * .2, h * y0], [w * .8, h * y0],
                    [w * .8, h * y1], [w * .2, h * y1]]
        return types.SimpleNamespace(
            boxes=[band(.30, .34), band(.40, .44)],
            txts=["Mario Rossi", "scarabocchio illeggibile"],
            scores=[0.95, 0.50])


class FakeEngine:
    """Detector deterministico coi contratti veri di analyze(): placeholder
    [LABEL_n], mapping, by_label."""

    def __init__(self, entries):
        self.entries = entries

    def analyze(self, text, excluded=None, custom_terms=None, ctl=None):
        entities, mapping, by_label = [], {}, {}
        phs = {}
        for label, value in self.entries:
            for m in re.finditer(re.escape(value), text):
                key = (label, value.lower())
                if key not in phs:
                    n = by_label.get(label, 0) + 1
                    by_label[label] = n
                    phs[key] = f"[{label}_{n}]"
                    mapping[phs[key]] = value
                entities.append({"label": label, "start": m.start(),
                                 "end": m.end(), "value": m.group(0),
                                 "source": "test", "validated": True,
                                 "ph": phs[key]})
        entities.sort(key=lambda e: e["start"])
        return {"entities": entities, "anonymized_text": text,
                "mapping": mapping, "n_entities": len(entities),
                "n_unique": len(mapping), "by_label": by_label}


def main():
    pdf = _test_pdf()

    # --- classificazione ed enumerazione ---------------------------------
    entries = image_ocr.pdf_images(pdf)
    keys = [e["key"] for e in entries]
    check("pagina vettoriale enumerata come page:0",
          keys and keys[0] == "page:0", str(keys))
    check("logo della pagina vettoriale NON enumerato a parte",
          not any(e.get("page") == 0 and not e["key"].startswith("page:")
                  for e in entries), str(keys))
    check("pagina testuale e bianca fuori dall'enumerazione",
          not any(e.get("page") in (1, 2) for e in entries), str(keys))
    check("scansione classica ancora sul percorso xref",
          any(not e["key"].startswith("page:") and e.get("page") == 3
              for e in entries), str(keys))
    page_entry = entries[0]
    check("render della pagina presente e in PNG",
          page_entry["ext"] == "png" and len(page_entry.get("data") or b"") > 0)
    from PIL import Image
    with Image.open(io.BytesIO(page_entry["data"])) as probe:
        w = probe.size[0]
    check("render a PAGE_DPI (595pt -> ~1240px)", 1235 <= w <= 1245, str(w))

    no_render = image_ocr.pdf_images(pdf, render_pages=False)
    check("render_pages=False: niente data, stesse voci",
          [e["key"] for e in no_render] == keys and
          all("data" not in e for e in no_render if e["key"].startswith("page:")))
    check("count_images conta anche le pagine vettoriali",
          image_ocr.count_images(pdf, ".pdf") == len(keys),
          str(image_ocr.count_images(pdf, ".pdf")))

    # --- OCR finto + fine a fine ------------------------------------------
    image_ocr._ocr = FakeOCR()
    image_ocr._detector = False          # niente YOLO: non è sotto test
    engine = FakeEngine([("FULLNAME", "Mario Rossi")])
    result = anonymize_pdf(pdf, engine, ocr=True)
    mapping = result["analysis"]["mapping"]

    check("PII della pagina vettoriale in mappa",
          "[FULLNAME_1]" in mapping, str(sorted(mapping)))
    check("riga a bassa confidenza coperta come UNREADABLE",
          any(ph.startswith("[" + image_ocr.UNREADABLE) for ph in mapping),
          str(sorted(mapping)))
    check("occorrenze nelle immagini dichiarate nel report",
          result["report"].get("image_occurrences", 0) >= 2,
          str(result["report"].get("image_occurrences")))

    with fitz.open(stream=result["pdf"], filetype="pdf") as out:
        p0 = out[0]
        cont = p0.read_contents()
        check("tracciati vettoriali RIMOSSI dal file (non coperti)",
              cont.count(b" c\n") == 0 and len(cont) < 1000, str(len(cont)))
        check("pagina 0 rasterizzata: una sola immagine a pagina intera",
              len(p0.get_images(full=True)) == 1)
        check("pagina 0 senza testo residuo", not p0.get_text().strip())
        check("pagina testuale redatta dal percorso testo",
              "Mario Rossi" not in out[1].get_text() and
              "[FULLNAME_1]" in out[1].get_text())

    boxes0 = result["anonymized_boxes"].get(0) or []
    check("overlay in punti pagina sul lato anonimizzato",
          any(b.get("ocr") and b["ph"] == "[FULLNAME_1]" and
              0 < b["x0"] < 595 and 0 < b["y0"] < 842 for b in boxes0),
          str(boxes0))
    check("overlay anche sul lato originale",
          any(b["ph"] == "[FULLNAME_1]" for b in
              result["original_boxes"].get(0) or []))

    # --- ri-redazione deterministica dopo una de-anonimizzazione ----------
    kept = {ph: v for ph, v in mapping.items() if ph != "[FULLNAME_1]"}
    redone = rebuild_pdf(pdf, kept, ocr_cache=result["ocr_cache"])
    phs0 = {b["ph"] for b in redone["anonymized_boxes"].get(0) or []}
    check("de-anonimizzazione: via il box del nome, resta l'UNREADABLE",
          "[FULLNAME_1]" not in phs0 and
          any(p.startswith("[" + image_ocr.UNREADABLE) for p in phs0),
          str(phs0))

    print(f"\n{PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

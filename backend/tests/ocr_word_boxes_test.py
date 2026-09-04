"""Valori di più parole su righe OCR "a box per parola".

NIENTE OCR vero: la cache è costruita a mano nel formato di build_cache.
L'OCR restituisce spesso un box PER PAROLA (tipico dei loghi: "Fondazione |
Policlinico | Universitario" sono righe distinte della cache). Il caso
storico: in_cache() cercava il valore nel corpus unito da "\\n" (match, quindi
la selezione manuale rispondeva "già anonimizzato") mentre boxes_for e
redacted_lines cercavano riga per riga (nessun match su una riga a parola
singola) e nei pixel non compariva alcun box.

Invariante verificato qui: se in_cache(valore) è vero, boxes_for copre il
valore (un box per ogni riga toccata) e redacted_lines lo sostituisce.

Uso:
    python backend/tests/ocr_word_boxes_test.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

from app.engine import image_ocr                  # noqa: E402

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


def line(t, x0, y0, x1, y1, s=0.99):
    return {"t": t, "s": s, "b": [x0, y0, x1, y1]}


def cache_of(*lines):
    return {"v": 1, "images": [{"key": "1", "ext": "png", "w": 400, "h": 100,
                                "lines": list(lines), "regions": [],
                                "plan": []}]}


# --- una parola per box, stessa riga visiva -------------------------------
print("\n--- valore di 3 parole su 3 box OCR distinti ---")
cache = cache_of(line("Fondazione", 6, 50, 83, 65),
                 line("Policlinico", 83, 51, 153, 65),
                 line("Universitario", 153, 50, 239, 66),
                 line("Agostino", 238, 51, 300, 67),
                 line("Cuore", 198, 68, 239, 82))
val = "Fondazione Policlinico Universitario"
mapping = {"[ORG_1]": val}
check("in_cache vede il valore", image_ocr.in_cache(val, cache))
boxes = image_ocr.boxes_for(cache, mapping).get("1", [])
check("boxes_for: un box per ciascuna delle 3 righe", len(boxes) == 3, boxes)
check("i box coprono le righe intere (parola = riga)",
      sorted(b for b, _ in boxes) == [[6.0, 50, 83.0, 65], [83.0, 51, 153.0, 65],
                                      [153.0, 50, 239.0, 66]], boxes)
check("tutti i box portano il placeholder", all(ph == "[ORG_1]" for _, ph in boxes))
red = image_ocr.redacted_lines(cache, mapping)[0]["lines"]
check("redacted_lines sostituisce le 3 righe",
      red == ["[ORG_1]", "[ORG_1]", "[ORG_1]", "Agostino", "Cuore"], red)

# --- parola singola: comportamento invariato ------------------------------
print("\n--- parola singola: solo la sua riga ---")
boxes = image_ocr.boxes_for(cache, {"[ORG_2]": "Agostino"}).get("1", [])
check("una sola riga coperta", boxes == [([238.0, 51, 300.0, 67], "[ORG_2]")], boxes)

# --- match parziale dentro una riga + scavalco ----------------------------
print("\n--- valore che inizia a metà riga e finisce sulla successiva ---")
cache = cache_of(line("Dott. Mario", 0, 0, 110, 20),
                 line("Rossi IRCCS", 0, 20, 110, 40))
boxes = image_ocr.boxes_for(cache, {"[PER_1]": "Mario Rossi"}).get("1", [])
check("2 segmenti", len(boxes) == 2, boxes)
(b0, _), (b1, _) = sorted(boxes, key=lambda b: b[0][1])
check("riga 1: solo la porzione 'Mario' (parte da metà)", 0 < b0[0] < 110 and b0[2] == 110.0, b0)
check("riga 2: solo la porzione 'Rossi' (finisce a metà)", b1[0] == 0.0 and 0 < b1[2] < 110, b1)
red = image_ocr.redacted_lines(cache, {"[PER_1]": "Mario Rossi"})[0]["lines"]
check("redacted_lines: placeholder su entrambe le righe",
      red == ["Dott. [PER_1]", "[PER_1] IRCCS"], red)

# --- niente falsi positivi tra parole non contigue ------------------------
print("\n--- la ricerca resta ancorata ai confini di parola ---")
cache = cache_of(line("Mario", 0, 0, 50, 20), line("Rossini", 0, 20, 60, 40))
check("'Mario Rossi' NON combacia con 'Mario Rossini'",
      image_ocr.boxes_for(cache, {"[PER_1]": "Mario Rossi"}) == {})
check("...coerente con in_cache", not image_ocr.in_cache("Mario Rossi", cache))

# --- il piano ha la precedenza: niente doppio box -------------------------
print("\n--- piano già presente su una delle righe ---")
cache = cache_of(line("Fondazione", 6, 50, 83, 65), line("Policlinico", 83, 51, 153, 65))
cache["images"][0]["plan"].append({"line": 0, "s": 0, "e": 10, "ph": "[ORG_1]"})
boxes = image_ocr.boxes_for(cache, {"[ORG_1]": "Fondazione Policlinico"}).get("1", [])
check("la riga già nel piano non si duplica, l'altra si copre comunque",
      sorted(b for b, _ in boxes) == [[6.0, 50, 83.0, 65], [83.0, 51, 153.0, 65]], boxes)

# --- placeholder de-anonimizzato: nessun box ------------------------------
print("\n--- mappa senza il placeholder: in chiaro ---")
check("mappa vuota -> nessun box", image_ocr.boxes_for(cache_of(line("Fondazione", 0, 0, 10, 10),
                                                                 line("Policlinico", 0, 10, 10, 20)), {}) == {})

print(f"\n{PASS} PASS, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)

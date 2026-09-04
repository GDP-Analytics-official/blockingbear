"""Batteria completa su tutti i file di test/:

Per ogni file:
  1. conversione all'ingresso come il worker (doc/odt->docx, ppt/odp->pptx,
     xls/ods->xlsx), anonimizzazione col modello;
  2. CHECK BOX: ogni placeholder redatto (by_placeholder>0) deve avere almeno
     un box interattivo in anonymized_boxes (e ogni box un ph in mappa);
  3. CHECK DEANONIMIZZA: per OGNI placeholder, rebuild con mappa senza di lui:
     - nessuna eccezione, residual vuoto;
     - il valore torna leggibile nell'output (se era stato trovato e non è
       annidato/sovrapposto a un altro valore in mappa);
     - il ph sparisce dai box, gli altri redatti restano nei box.

Output: JSONL di esiti + log leggibile su stdout.
"""
import io
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import fitz

from app.config import MODEL_DIR
from app.engine import PiiEngine

# profilo LibreOffice ISOLATO: il profilo dell'app è conteso col backend
# live (il codice rimuove il .lock "stantio" dell'altro processo e le
# conversioni collassano a vicenda) — la suite non deve toccare quello vero
from app.engine import convert as _convert
_LO = Path(__file__).parent / "data" / "lo_profile_suite"
_LO.mkdir(parents=True, exist_ok=True)
_convert._PROFILE_DIR = _LO
from app.engine import pdf as pdfmod
from app.engine import pdf_export as pe
from app.engine.convert import to_docx, to_pptx, to_xlsx
from app.engine.docx import anonymize_docx, rebuild_docx
from app.engine.docx import extract_text as docx_text
from app.engine.pdf import anonymize_pdf, rebuild_pdf
from app.engine.pptx import anonymize_pptx, rebuild_pptx
from app.engine.pptx import extract_text as pptx_text
from app.engine.txt import anonymize_txt, rebuild_txt
from app.engine.txt import extract_text as txt_text
from app.engine.xlsx import anonymize_xlsx, rebuild_xlsx
from app.engine.xlsx import extract_text as xlsx_text

TEST_DIR = Path(__file__).resolve().parent / "assets"
OUT = Path(__file__).parent / "data" / "smoke" / "deanonymize_suite.jsonl"
# argv[1]: nomi file (separati da ;) da testare, vuoto = tutti
# argv[2]: tetto di placeholder per file (default 80)
ONLY = set((sys.argv[1] or "").split(";")) if len(sys.argv) > 1 and sys.argv[1] else None
MAX_PH_PER_FILE = int(sys.argv[2]) if len(sys.argv) > 2 else 80

results = open(OUT, "a", encoding="utf-8")


def emit(rec):
    results.write(json.dumps(rec, ensure_ascii=False) + "\n")
    results.flush()


def norm(s):
    return pe._norm(s)


def overlapping(value, others):
    """True se value è sottostringa (normalizzata) di un altro valore in
    mappa o viceversa: la sua leggibilità in chiaro non è garantita."""
    v = norm(value)
    for o in others:
        n = norm(o)
        if v and n and (v in n or n in v):
            return True
    return False


def boxed_phs(anonymized_boxes):
    return {b["ph"] for pg in anonymized_boxes.values() for b in pg}


def out_text(kind, blob, res):
    if kind == "pdf":
        with fitz.open(stream=blob, filetype="pdf") as d:
            return "\n".join(p.get_text() for p in d)
    if kind == "docx":
        return docx_text(blob)
    if kind == "pptx":
        return pptx_text(blob, full=True)
    if kind == "xlsx":
        return xlsx_text(blob, full=True)
    return blob.decode("utf-8", "replace")


engine = PiiEngine(MODEL_DIR)
files = sorted(TEST_DIR.iterdir(), key=lambda p: p.name.lower())
print(f"{len(files)} file in {TEST_DIR}", flush=True)

for path in files:
    if not path.is_file():
        continue
    name = path.name
    if ONLY is not None and name not in ONLY:
        continue
    ext = path.suffix.lower()
    t0 = time.time()
    data = path.read_bytes()
    try:
        if ext in (".doc", ".odt"):
            data, kind = to_docx(data, suffix=ext), "docx"
        elif ext in (".ppt", ".odp"):
            data, kind = to_pptx(data, suffix=ext), "pptx"
        elif ext in (".xls", ".xlsm", ".ods"):
            data, kind = to_xlsx(data, suffix=ext), "xlsx"
        elif ext == ".pdf":
            kind = "pdf"
        elif ext == ".docx":
            kind = "docx"
        elif ext == ".pptx":
            kind = "pptx"
        elif ext == ".xlsx":
            kind = "xlsx"
        elif ext in (".txt", ".md"):
            kind = "txt"
        else:
            print(f"\n=== {name}: estensione non gestita, salto ===", flush=True)
            continue

        print(f"\n=== {name} ({kind}) ===", flush=True)
        if kind == "pdf":
            res = anonymize_pdf(data, engine)
            anon_blob = res["pdf"]
        elif kind == "xlsx":
            res = anonymize_xlsx(data, engine, max_chunks=200)
            anon_blob = res["file"]
        elif kind == "docx":
            res = anonymize_docx(data, engine)
            anon_blob = res["file"]
        elif kind == "pptx":
            res = anonymize_pptx(data, engine)
            anon_blob = res["file"]
        else:
            res = anonymize_txt(data, engine)
            anon_blob = res["file"]
    except Exception as e:
        print(f"  ANONIMIZZAZIONE FALLITA: {type(e).__name__}: {e}", flush=True)
        emit({"file": name, "stage": "anonymize", "ok": False, "error": str(e)})
        continue

    mapping = res["analysis"]["mapping"]
    rep = res["report"]
    preview_orig = res.get("preview_pdf_original")
    redacted = {ph for ph, n in rep["by_placeholder"].items() if n > 0}
    boxed = boxed_phs(res["anonymized_boxes"])
    truncated = rep.get("preview_truncated", False)

    print(f"  mappa: {len(mapping)} ph | redatti: {len(redacted)} | "
          f"skipped: {len(rep['skipped'])} | not_found: {len(rep['not_found'])} | "
          f"residual: {rep['residual']} | preview troncata: {truncated}", flush=True)

    # ---- CHECK BOX ----
    no_box = sorted(redacted - boxed)
    spurious = sorted(boxed - set(mapping))
    emit({"file": name, "stage": "boxes", "ok": not no_box and not spurious,
          "no_box": no_box, "spurious": spurious, "truncated": truncated,
          "n_mapping": len(mapping), "n_redacted": len(redacted),
          "skipped": rep["skipped"], "not_found": rep["not_found"],
          "residual": rep["residual"]})
    if no_box:
        print(f"  !! redatti SENZA box interattivo: {no_box}", flush=True)
    if spurious:
        print(f"  !! box con ph fuori mappa: {spurious}", flush=True)

    # ---- CHECK DEANONIMIZZA uno ad uno ----
    phs = list(mapping)
    sampled = False
    if len(phs) > MAX_PH_PER_FILE:
        phs = phs[:MAX_PH_PER_FILE]
        sampled = True
        print(f"  (mappa >{MAX_PH_PER_FILE}: testo i primi {MAX_PH_PER_FILE})",
              flush=True)

    n_ok = 0
    for ph in phs:
        val = mapping[ph]
        new_map = {k: v for k, v in mapping.items() if k != ph}
        if not new_map:
            continue                      # l'API lo vieta comunque
        entry = {"file": name, "stage": "deanonymize", "ph": ph}
        try:
            if kind == "pdf":
                r2 = rebuild_pdf(data, new_map)
                blob2 = r2["pdf"]
            elif kind == "docx":
                r2 = rebuild_docx(data, new_map, preview_pdf_original=preview_orig)
                blob2 = r2["file"]
            elif kind == "pptx":
                r2 = rebuild_pptx(data, new_map, preview_pdf_original=preview_orig)
                blob2 = r2["file"]
            elif kind == "xlsx":
                r2 = rebuild_xlsx(data, new_map, preview_pdf_original=preview_orig,
                                  exact_phs=set())
                blob2 = r2["file"]
            else:
                r2 = rebuild_txt(data, new_map, preview_pdf_original=preview_orig)
                blob2 = r2["file"]
        except Exception as e:
            entry.update(ok=False, error=f"{type(e).__name__}: {e}")
            emit(entry)
            print(f"  !! {ph}: REBUILD FALLITO: {type(e).__name__}: {e}", flush=True)
            continue

        rep2 = r2["report"]
        boxed2 = boxed_phs(r2["anonymized_boxes"])
        problems = []
        if rep2["residual"]:
            problems.append(f"residual={rep2['residual']}")
        if ph in boxed2:
            problems.append("ph ancora nei box")
        # il valore deve tornare leggibile, se era stato trovato e non è
        # annidato in (o contenitore di) un altro valore ancora in mappa
        was_found = ph in redacted
        over = overlapping(val, new_map.values())
        if was_found and not over:
            txt2 = out_text(kind, blob2, r2)
            pat = pe._value_pattern(val)
            if pat and not pat.search(txt2):
                problems.append("valore NON tornato leggibile")
        # gli altri ph redatti prima devono avere ancora un box
        # (salvo quelli che esistevano solo dentro l'area del ph rimosso)
        still_redacted = {p for p, n in rep2["by_placeholder"].items() if n > 0}
        missing_boxes = sorted(still_redacted - boxed2)
        if missing_boxes:
            problems.append(f"redatti senza box: {missing_boxes}")

        entry.update(ok=not problems, problems=problems,
                     overlapping=over, was_found=was_found)
        emit(entry)
        if problems:
            print(f"  !! {ph} ({val!r}): {problems}", flush=True)
        else:
            n_ok += 1
    print(f"  deanonimizzazione: {n_ok}/{len([p for p in phs if len(mapping) > 1])} ok"
          f"{' (campione)' if sampled else ''}  [{time.time()-t0:.0f}s]", flush=True)

results.close()
print("\nFINE", flush=True)

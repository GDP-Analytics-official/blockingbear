"""Smoke test del motore sui PDF di riferimento (test/): anonimizza ciascun PDF
e stampa mappa, report e verifica dei residui. Non tocca il DB né il server.

Uso:  python backend/tests/smoke_test.py [cartella_pdf]
"""

import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.config import MODEL_DIR
from app.engine import PiiEngine
from app.engine.pdf import PdfError, anonymize_pdf

folder = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent / "assets")
out_dir = Path(__file__).parent / "data" / "smoke"
out_dir.mkdir(parents=True, exist_ok=True)
pdfs = sorted(p for p in folder.glob("*.pdf") if not p.stem.endswith("_anonimizzato"))
if not pdfs:
    sys.exit(f"Nessun PDF in {folder}")

engine = PiiEngine(MODEL_DIR)
print(f"Modello: {MODEL_DIR}")

for p in pdfs:
    print(f"\n=== {p.name} ===")
    t0 = time.time()
    try:
        res = anonymize_pdf(p.read_bytes(), engine)
    except PdfError as e:
        print(f"  SCARTATO: {e}")
        continue
    dt = time.time() - t0
    a, rep = res["analysis"], res["report"]
    print(f"  {a['n_entities']} entità, {a['n_unique']} uniche in {dt:.1f}s")
    print(f"  per tag: {a['by_label']}")
    print(f"  redazioni: {rep['occurrences']}  "
          f"not_found: {len(rep['not_found'])}  skipped: {len(rep['skipped'])}  "
          f"RESIDUI: {len(rep['residual'])} {rep['residual'] or ''}")
    print("  mappa:", json.dumps(a["mapping"], ensure_ascii=False)[:600])
    out = out_dir / f"{p.stem}_anonimizzato.pdf"
    out.write_bytes(res["pdf"])
    print(f"  scritto {out.name}")

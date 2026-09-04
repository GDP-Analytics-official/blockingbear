"""Aree SIGILLATE della preview anonimizzata (rettangolo nero, contenuto
rimosso davvero): motore (pdf.seal_pdf, image_ocr.seal_image) e route
(POST/DELETE /api/projects/{id}/files/{fid}/seal-area) con app FastAPI
ridotta e file elaborati direttamente (project_files.process_upload, senza
coda job).

Copre: rimozione vera del testo sotto il rettangolo, replica su tutte le
pagine, etichetta SEALED, box per l'overlay, clamp ai bordi, persistenza
sealed_json attraverso il rebuild, rimozione del sigillo (il contenuto
torna), guardie (formato non sigillabile, area minuscola, pagina fuori
range, sigillo inesistente).

Uso:
    python backend/tests/seal_test.py
"""
import io
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

DATA_DIR = HERE / "data" / "test_seal"
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA_DIR)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

import fitz                                                      # noqa: E402
from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402
from PIL import Image                                            # noqa: E402

from app import jobs, project_files                              # noqa: E402
from app.auth import seed_admin                                  # noqa: E402
from app.db import init_db                                       # noqa: E402
from app.engine import convert, image_ocr                       # noqa: E402
from app.engine.pdf import extract_text, rebuild_pdf, seal_pdf   # noqa: E402
from app.routes import auth_routes, projects                     # noqa: E402

# profilo LibreOffice isolato: le conversioni non devono contendersi il
# profilo di un eventuale backend in esecuzione
convert._PROFILE_DIR = DATA_DIR / "lo_profile"

PASS = 0
FAIL = 0

MAPPING = {"[FULLNAME_1]": "Mario Rossi"}
SECRET = "SEGRETO-XYZ-123"
HEADER = "Intestazione riservata"
# rettangoli generosi attorno alle baseline di insert_text (fontsize 12)
SECRET_RECT = [55.0, 186.0, 330.0, 210.0]
HEADER_RECT = [55.0, 58.0, 330.0, 82.0]


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def make_pdf():
    """Due pagine A4: intestazione su entrambe, il 'segreto' solo sulla 1."""
    doc = fitz.open()
    for i in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), HEADER, fontsize=12)
        page.insert_text((72, 140), f"Cliente: Mario Rossi (pagina {i + 1})",
                         fontsize=12)
        if i == 0:
            page.insert_text((72, 200), SECRET, fontsize=12)
    out = doc.tobytes()
    doc.close()
    return out


# --------------------------------------------------------------------------- #
print("== motore: seal_pdf ==")
raw = make_pdf()
res = rebuild_pdf(raw, MAPPING)
text0, _ = extract_text(res["pdf"], allow_empty=True)
check("prima del sigillo il segreto è leggibile", SECRET in text0)

sealed = [{"n": 1, "page": 0, "all": False, "rect": SECRET_RECT},
          {"n": 2, "page": 0, "all": True, "rect": HEADER_RECT}]
out, boxes = seal_pdf(res["pdf"], sealed)
text, _ = extract_text(out, allow_empty=True)
check("segreto RIMOSSO dal layer testuale", SECRET not in text)
check("intestazione rimossa su TUTTE le pagine", HEADER not in text)
check("il resto del testo resta", "pagina 2" in text)
check("la redazione della mappa resta", "[FULLNAME_1]" in text)
check("etichetta SEALED disegnata (bianca)", "SEALED" in text)
check("box: 2 in pagina 1 e 1 in pagina 2",
      set(boxes) == {0, 1} and len(boxes[0]) == 2 and len(boxes[1]) == 1,
      f"{ {k: len(v) for k, v in boxes.items()} }")
check("box marcati sealed/label",
      all(b.get("sealed") and b["label"] == "SEALED"
          for bl in boxes.values() for b in bl))
same, _ = seal_pdf(res["pdf"], [])
check("nessun sigillo = PDF invariato", same == res["pdf"])

# --------------------------------------------------------------------------- #
print("== motore: seal_image ==")
im = Image.new("RGB", (400, 300), "white")
for x in range(100, 200):
    for y in range(100, 150):
        im.putpixel((x, y), (200, 30, 30))          # blocco 'da nascondere'
buf = io.BytesIO()
im.save(buf, format="PNG")
img_data = buf.getvalue()

out_img, iboxes = image_ocr.seal_image(
    img_data, ".png", [{"n": 1, "page": 0, "all": False,
                        "rect": [90.0, 90.0, 210.0, 160.0]}])
with Image.open(io.BytesIO(out_img)) as sealed_im:
    px_in = sealed_im.getpixel((95, 155))            # dentro il box, non al centro
    px_out = sealed_im.getpixel((300, 200))
check("pixel sotto il sigillo neri", px_in[:3] == (0, 0, 0), str(px_in))
check("pixel fuori dal sigillo intatti", px_out[:3] == (255, 255, 255))
check("box overlay dell'immagine",
      iboxes.get(0) and iboxes[0][0]["sealed"] == 1
      and iboxes[0][0]["label"] == "SEALED")

# rect degenere (tutto fuori dai bordi): nessuna modifica
same_img, noboxes = image_ocr.seal_image(
    img_data, ".png", [{"n": 1, "page": 0, "all": False,
                        "rect": [500.0, 500.0, 600.0, 600.0]}])
check("rect fuori dall'immagine ignorato", same_img == img_data and not noboxes)

# --------------------------------------------------------------------------- #
print("== route: seal-area su un file di progetto ==")
SessionLocal = init_db()
with SessionLocal() as s:
    seed_admin(s)

app = FastAPI()
app.include_router(auth_routes.router)
app.include_router(projects.router)
client = TestClient(app)
tok = client.post("/api/auth/login",
                  json={"username": "admin", "password": "admin"}).json()["token"]
H = {"Authorization": f"Bearer {tok}"}

pid = client.post("/api/projects", json={
    "name": "Sigilli", "anonymized": True}, headers=H).json()["id"]

# elaborazione diretta (stessa strada del worker, senza coda né SSE)
with SessionLocal() as s:
    pf = project_files.process_upload(raw, "contratto.pdf", pid,
                                      jobs.ENGINES[0], s)
    fid = pf.id
BASE = f"/api/projects/{pid}/files/{fid}"
d0 = client.get(BASE, headers=H).json()
rev0 = d0["rev"]

r = client.post(f"{BASE}/seal-area", headers=H,
                json={"page": 0, "rect": SECRET_RECT, "all_pages": False})
check("POST seal-area risponde 200", r.status_code == 200, str(r.status_code))
d = r.json()
check("descriptor: un'area sigillata", len(d.get("sealed", [])) == 1
      and d.get("sealed_added") == 1)
check("rev incrementata", d["rev"] == rev0 + 1)
sealed_boxes = [b for bl in d["anonymized_boxes"].values() for b in bl
                if b.get("sealed")]
check("box sealed nell'overlay", len(sealed_boxes) == 1
      and sealed_boxes[0]["label"] == "SEALED")
dl = client.get(f"{BASE}/download", headers=H)
t, _ = extract_text(dl.content, allow_empty=True)
check("download: segreto rimosso", SECRET not in t)
check("download: il resto resta redatto", "Mario Rossi" not in t)

r = client.post(f"{BASE}/seal-area", headers=H,
                json={"page": 1, "rect": HEADER_RECT, "all_pages": True})
d = r.json()
check("secondo sigillo su tutte le pagine", r.status_code == 200
      and len(d["sealed"]) == 2 and d["sealed_added"] == 2)
dl = client.get(f"{BASE}/download", headers=H)
t, _ = extract_text(dl.content, allow_empty=True)
check("intestazione rimossa ovunque (download)", HEADER not in t)
png = client.get(f"{BASE}/pages/anonymized/0.png", headers=H)
check("pagina PNG renderizzabile", png.status_code == 200
      and png.content[:8] == b"\x89PNG\r\n\x1a\n")

r = client.delete(f"{BASE}/seal-area/1", headers=H)
d = r.json()
check("DELETE sigillo 1", r.status_code == 200 and len(d["sealed"]) == 1
      and d["sealed_removed"] == 1)
dl = client.get(f"{BASE}/download", headers=H)
t, _ = extract_text(dl.content, allow_empty=True)
check("il segreto TORNA dopo la rimozione", SECRET in t)
check("l'altro sigillo resta", HEADER not in t)

# guardie
r = client.post(f"{BASE}/seal-area", headers=H,
                json={"page": 9, "rect": SECRET_RECT})
check("pagina fuori range -> 422", r.status_code == 422)
r = client.post(f"{BASE}/seal-area", headers=H,
                json={"page": 0, "rect": [10, 10, 11, 11]})
check("area minuscola -> 422", r.status_code == 422)
r = client.delete(f"{BASE}/seal-area/99", headers=H)
check("sigillo inesistente -> 404", r.status_code == 404)

with SessionLocal() as s:
    txt_pf = project_files.process_upload(
        b"Nota per Mario Rossi: nulla di sigillabile qui.", "note.txt", pid,
        jobs.ENGINES[0], s)
    txt_fid = txt_pf.id
r = client.post(f"/api/projects/{pid}/files/{txt_fid}/seal-area", headers=H,
                json={"page": 0, "rect": SECRET_RECT})
check("formato non sigillabile -> 422", r.status_code == 422,
      r.json().get("detail", "")[:60])

print(f"\n{PASS} PASS, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)

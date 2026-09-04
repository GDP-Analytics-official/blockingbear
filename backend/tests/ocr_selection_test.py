"""Fallback OCR della selezione manuale + rielaborazione con OCR.

Copre il percorso "seleziono un'area immagine nella preview e il layer
testuale non dà nulla": extract con fallback OCR (flag ocr/ocr_tried/
ocr_redactable), rifiuto dell'anonimizza-in-più senza cache OCR,
reprocess_ocr (il rilevamento rifatto con ocr=True sul file già elaborato),
selezione che dopo la rielaborazione diventa redigibile e il box OCR che
compare davvero, coda job (kind=project_reprocess, doc_busy, annullamento).

OCR e detector firme FINTI e deterministici (si prova la meccanica, non la
qualità di lettura); modello PII vero, app FastAPI ridotta come in
projects_test.py.

Uso:
    python backend/tests/ocr_selection_test.py
"""
import io
import json
import os
import shutil
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

DATA_DIR = HERE / "data" / "test_ocr_selection"
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA_DIR)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

import fitz                                                      # noqa: E402
from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import jobs, project_files, settings_store              # noqa: E402
from app.auth import seed_admin                                  # noqa: E402
from app.db import Job, init_db                                  # noqa: E402
from app.engine import image_ocr                                 # noqa: E402
from app.routes import auth_routes, chat_routes, projects        # noqa: E402

PASS = 0
FAIL = 0

# le stesse identità inventate degli asset (vedi make_assets.py): questo test
# non legge un documento, si scrive da sé le righe che l'OCR "avrebbe letto".
LINE1 = "Acme Forniture SRL"
LINE2 = "Mario Rossi"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class FakeOCR:
    """Due righe fisse ad alta confidenza su qualunque immagine: la meccanica
    (fallback, corpus, piano, box) è identica a una lettura vera.

    I box sono RELATIVI all'array ricevuto, come li darebbe un motore vero:
    il fallback della selezione riporta le letture in punti pagina e tiene
    solo le PAROLE dentro il rettangolo scelto (image_ocr._SEL_WORD), quindi
    coordinate fisse direbbero «testo nell'angolo del ritaglio» e verrebbero
    giustamente buttate. Le due righe stanno ben dentro il centro: l'array
    del fallback comprende anche il margine di contesto e la cornice neutra,
    e una banda larga sborderebbe dall'area selezionata — perdendo le parole
    di bordo, che è il comportamento voluto ma non quello sotto esame."""

    def __call__(self, arr):
        h, w = arr.shape[0], arr.shape[1]

        def band(y0, y1):
            return [[w * 0.42, h * y0], [w * 0.58, h * y0],
                    [w * 0.58, h * y1], [w * 0.42, h * y1]]

        return types.SimpleNamespace(
            boxes=[band(0.40, 0.46), band(0.54, 0.60)],
            txts=[LINE1, LINE2],
            scores=[0.95, 0.95])


def _png(w, h, color=(230, 230, 240)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


IMG_RECT = [50, 600, 250, 700]        # dove sta l'immagine nella pagina


def _test_pdf():
    """Una pagina con una riga di testo normale + un'immagine incorporata
    (200x100 px, sopra MIN_SIDE): il file si carica anche SENZA OCR perché
    il layer testuale non è vuoto."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Contratto quadro tra le parti n. 4471.",
                     fontsize=12)
    page.insert_image(fitz.Rect(IMG_RECT), stream=_png(200, 100))
    out = doc.tobytes()
    doc.close()
    return out


def local_deanonymize_case(client, auth, SessionLocal, engine, pid):
    """Progetti: deanonimizzare [UNREADABLE_n]/[SIGNATURE_n] (voci della
    cache OCR, non del registro) deve togliere il box invece di rispondere
    404; la riga illeggibile riselezionata torna coperta come tale."""
    unreadable_line = "prodotto in offerta"

    class LocalOCR:
        def __call__(self, arr):
            h, w = arr.shape[0], arr.shape[1]

            def band(y0, y1):
                return [[w * .2, h * y0], [w * .8, h * y0],
                        [w * .8, h * y1], [w * .2, h * y1]]
            return types.SimpleNamespace(
                boxes=[band(.10, .30), band(.45, .60)],
                txts=["Nota di consegna merce", unreadable_line],
                scores=[0.95, 0.40])

    image_ocr._ocr = LocalOCR()
    image_ocr.detect_regions = lambda pil: [
        {"b": [int(pil.width * .05), int(pil.height * .80),
               int(pil.width * .30), int(pil.height * .98)], "s": 0.9}]
    try:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "Documento di prova.", fontsize=12)
        page.insert_image(fitz.Rect(60, 300, 460, 500), stream=_png(400, 200))
        pdf = doc.tobytes()
        doc.close()
        with SessionLocal() as s:
            pf = project_files.process_upload(pdf, "scan.pdf", pid, engine, s,
                                              ocr=True)
            fid = pf.id
        base = f"/api/projects/{pid}/files/{fid}"
        d = client.get(base, headers=auth).json()

        def boxes_of(desc):
            return [b for bl in (desc.get("anonymized_boxes") or {}).values()
                    for b in bl]

        phs = {b["ph"] for b in boxes_of(d)}
        unr = next((p for p in phs if p.startswith("[UNREADABLE_")), None)
        sig = next((p for p in phs if p.startswith("[SIGNATURE_")), None)
        check("progetto: box [UNREADABLE_n] e [SIGNATURE_n]",
              unr is not None and sig is not None, str(sorted(phs)))
        if not (unr and sig):
            return
        check("progetto: la mappa mostra il valore delle voci locali",
              d["mapping"].get(unr) == unreadable_line
              and d["mapping"].get(sig) == image_ocr.SIG_VALUE,
              json.dumps(d["mapping"])[:200])
        r = client.post(f"{base}/deanonymize", headers=auth,
                        json={"placeholder": sig})
        d = client.get(base, headers=auth).json()
        check("progetto: deanonimizza [SIGNATURE_n] -> box sparito",
              r.status_code == 200 and not any(b["ph"] == sig for b in boxes_of(d))
              and any(b["ph"] == unr for b in boxes_of(d)),
              f"{r.status_code} {r.text[:120]}")
        r = client.post(f"{base}/deanonymize", headers=auth,
                        json={"label": "UNREADABLE"})
        d = client.get(base, headers=auth).json()
        check("progetto: deanonimizza categoria UNREADABLE -> box sparito",
              r.status_code == 200 and not any(b["ph"] == unr for b in boxes_of(d)),
              f"{r.status_code} {r.text[:120]}")
        r = client.post(f"{base}/anonymize-text", headers=auth,
                        json={"text": unreadable_line})
        d = client.get(base, headers=auth).json()
        check("progetto: riga illeggibile riselezionata -> di nuovo [UNREADABLE_n]",
              r.status_code == 200 and r.json().get("added") == unr
              and any(b["ph"] == unr for b in boxes_of(d))
              and not any(p.startswith("[CUSTOM_") for p in d["mapping"]),
              f"{r.status_code} {r.text[:120]}")
    finally:
        image_ocr._ocr = FakeOCR()
        image_ocr.detect_regions = lambda pil: []


def main():
    # OCR e detector finti: _get_ocr() ritorna il singleton già "caldo"
    image_ocr._ocr = FakeOCR()
    image_ocr.detect_regions = lambda pil: []

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(chat_routes.router)
    app.include_router(projects.router)
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)
        settings_store.set_anon_defaults(s, [], [])

    client = TestClient(app)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    engine = jobs.ENGINES[0]

    p = client.post("/api/projects", json={
        "name": "OCR selezione", "anonymized": True}, headers=auth).json()
    pid = p["id"]

    # --- upload SENZA OCR (il caso che genera il problema) --------------------
    with SessionLocal() as s:
        pf = project_files.process_upload(
            _test_pdf(), "contratto.pdf", pid, engine, s, ocr=False)
        fid = pf.id
    base = f"/api/projects/{pid}/files/{fid}"
    with SessionLocal() as s:
        pf_row = s.get(type(pf), fid)
        check("nessuna cache OCR dopo l'upload senza OCR",
              not project_files.ocr_cache_path(pf_row).is_file())

    # --- selezione su area TESTO: percorso invariato ---------------------------
    r = client.post(f"{base}/extract", headers=auth, json={
        "source": "original", "page": 0, "rect": [60, 85, 540, 110]})
    j = r.json()
    check("selezione su testo: layer testuale, niente OCR",
          r.status_code == 200 and "Contratto" in j["text"]
          and j["ocr"] is False and j["ocr_tried"] is False, r.text[:120])

    # --- selezione su area IMMAGINE: fallback OCR ------------------------------
    r = client.post(f"{base}/extract", headers=auth, json={
        "source": "anonymized", "page": 0, "rect": IMG_RECT})
    j = r.json()
    check("fallback OCR sull'area immagine",
          r.status_code == 200 and j["text"] == f"{LINE1} {LINE2}"
          and j["ocr"] is True and j["ocr_tried"] is True, r.text[:160])
    check("ocr_redactable=False senza cache (file elaborato senza OCR)",
          j.get("ocr_redactable") is False, r.text[:160])

    # selezione degenere: nemmeno l'OCR ha qualcosa da leggere
    r = client.post(f"{base}/extract", headers=auth, json={
        "source": "anonymized", "page": 0, "rect": [0, 0, 1, 1]})
    j = r.json()
    check("selezione degenere: testo vuoto, ocr_tried dichiarato",
          r.status_code == 200 and j["text"] == ""
          and j["ocr_tried"] is True, r.text[:120])

    # --- anonimizza-in-più del valore letto: rifiutato senza cache ------------
    r = client.post(f"{base}/anonymize-text", headers=auth,
                    json={"text": LINE1})
    check("anonimizza-in-più rifiutato senza cache OCR (valore non nel file)",
          r.status_code == 422, f"{r.status_code} {r.text[:120]}")

    # --- coda job: 202, file occupato, annullamento -----------------------------
    r = client.post(f"{base}/reprocess-ocr", headers=auth)
    job_desc = r.json()
    check("reprocess-ocr accodato (202, kind=project_reprocess)",
          r.status_code == 202 and job_desc.get("kind") == "project_reprocess",
          r.text[:160])
    check("file occupato durante il job (doc_busy)", jobs.doc_busy(fid))
    r = client.post(f"{base}/anonymize-text", headers=auth,
                    json={"text": "quadro tra le parti"})
    check("modifiche rifiutate a file occupato", r.status_code == 409,
          f"{r.status_code} {r.text[:120]}")
    r = client.post(f"{base}/reprocess-ocr", headers=auth)
    check("secondo reprocess rifiutato a file occupato", r.status_code == 409,
          f"{r.status_code} {r.text[:120]}")
    with SessionLocal() as s:
        job = s.get(Job, job_desc["id"])
        jobs.cancel(job, s)             # nessun worker attivo nel test
    check("annullamento libera il file", not jobs.doc_busy(fid))

    # --- rielaborazione con OCR (direttamente, senza coda) ----------------------
    with SessionLocal() as s:
        pf2 = project_files.reprocess_ocr(
            {"project_id": pid, "file_id": fid}, engine, s)
        check("reprocess_ocr rielabora lo STESSO file", pf2.id == fid)
        check("cache OCR scritta dalla rielaborazione",
              project_files.ocr_cache_path(pf2).is_file())
        cache = json.loads(
            project_files.ocr_cache_path(pf2).read_text(encoding="utf-8"))
        lines = [ln["t"] for img in cache["images"] for ln in img["lines"]]
        check("il corpus della cache contiene le righe lette",
              LINE1 in lines and LINE2 in lines, str(lines))

    d = client.get(base, headers=auth).json()
    check("rev del file avanzata (anteprime rigenerate)", d["rev"] >= 2,
          str(d["rev"]))

    # --- ora la selezione è redigibile -----------------------------------------
    r = client.post(f"{base}/extract", headers=auth, json={
        "source": "anonymized", "page": 0, "rect": IMG_RECT})
    j = r.json()
    check("dopo la rielaborazione: ocr_redactable=True",
          j.get("ocr") is True and j.get("ocr_redactable") is True,
          r.text[:160])

    # --- anonimizza-in-più del valore OCR: box nei pixel ------------------------
    ph = next((k for k, v in d["mapping"].items() if v == LINE1), None)
    if ph is None:
        r = client.post(f"{base}/anonymize-text", headers=auth,
                        json={"text": LINE1})
        check("anonimizza-in-più del valore letto dall'OCR",
              r.status_code == 200
              and r.json().get("added", "").startswith("[CUSTOM_"),
              f"{r.status_code} {r.text[:160]}")
        d = r.json()
        ph = d.get("added")
    else:
        # il modello l'aveva già riconosciuto al reprocess: va bene lo stesso
        check("valore già in mappa dopo la rielaborazione", True, ph)
    boxes = [b for blist in (d.get("anonymized_boxes") or {}).values()
             for b in blist]
    hit = next((b for b in boxes if b.get("ph") == ph and b.get("ocr")), None)
    check("box OCR disegnato per il valore nell'immagine", hit is not None,
          json.dumps(boxes)[:200])

    # --- guardie della route -----------------------------------------------------
    pc = client.post("/api/projects", json={
        "name": "In chiaro", "anonymized": False}, headers=auth).json()
    rc = client.post(f"/api/projects/{pc['id']}/files", headers=auth,
                     files={"file": ("nota.txt", b"solo testo", "text/plain")})
    fid_clear = rc.json()["file"]["id"]
    r = client.post(f"/api/projects/{pc['id']}/files/{fid_clear}"
                    "/reprocess-ocr", headers=auth)
    check("reprocess rifiutato su progetto in chiaro", r.status_code == 422,
          f"{r.status_code} {r.text[:120]}")

    local_deanonymize_case(client, auth, SessionLocal, engine, pid)

    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

"""Sonda pre-upload col BIGLIETTO (app/probe_store.py + le route di
app/routes/projects.py): il file attraversa la rete una volta sola.

Prima, caricare un file in un progetto anonimizzato significava mandarlo due
volte — una alla sonda (che conta immagini e testo del PDF) e una all'upload.
Ora la sonda mette i byte da parte e risponde con un `probe_id`; l'upload cita
il biglietto. Qui si prova che il biglietto funziona, che vale una volta sola,
che nessuno può usare quello di un altro e che il ripiego (mandare davvero il
file) resta valido — il frontend ci casca sopra quando il biglietto scade.

App FastAPI ridotta (auth + progetti), nessun modello PII e nessuna coda: il
progetto in chiaro salva in modo sincrono, e per quello anonimizzato si
intercetta jobs.submit_project per leggere cosa arriverebbe al worker.

Uso:
    python backend/tests/probe_upload_test.py
"""
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_probe")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import jobs, probe_store, settings_store                # noqa: E402
from app.auth import seed_admin                                  # noqa: E402
from app.db import init_db                                       # noqa: E402
from app.routes import auth_routes, projects                     # noqa: E402

PASS = 0
FAIL = 0

CONTENT = ("Cliente: Mario Rossi\n"
           "Email: mario.rossi@example.com\n"
           "Nota: preventivo di massima.\n").encode()


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def probe_files_on_disk():
    return sorted(p.name for p in (probe_store._DIR).glob("*.bin"))


def main():
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(projects.router)
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)
        settings_store.set_anon_defaults(s, [], [])

    client = TestClient(app)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    p_clear = client.post("/api/projects", json={
        "name": "In chiaro", "anonymized": False}, headers=auth).json()
    p_anon = client.post("/api/projects", json={
        "name": "Anonimizzato", "anonymized": True}, headers=auth).json()

    # --- la sonda risponde col biglietto ------------------------------------
    r = client.post("/api/projects/probe", headers=auth,
                    files={"file": ("preventivo.txt", CONTENT, "text/plain")})
    pr = r.json()
    check("sonda 200 col biglietto",
          r.status_code == 200 and isinstance(pr.get("probe_id"), str)
          and len(pr["probe_id"]) == 32, str(pr)[:120])
    check("sonda: campi del popup OCR ancora al loro posto",
          "images" in pr and "pdf_no_text" in pr and "ocr_available" in pr)
    check("byte messi da parte su disco",
          probe_files_on_disk() == [f"{pr['probe_id']}.bin"],
          str(probe_files_on_disk()))

    # --- l'upload cita il biglietto, il file NON riparte ---------------------
    r = client.post(f"/api/projects/{p_clear['id']}/files", headers=auth,
                    data={"probe_id": pr["probe_id"]})
    body = r.json()
    check("upload col solo biglietto accettato",
          r.status_code == 202 and "file" in body, str(body)[:120])
    fid = body["file"]["id"]
    check("nome del file conservato dal biglietto",
          body["file"]["filename"] == "preventivo.txt", str(body["file"]))
    r = client.get(f"/api/projects/{p_clear['id']}/files/{fid}/download",
                   headers=auth)
    check("byte salvati identici all'originale", r.content == CONTENT)
    check("biglietto consumato: niente residui su disco",
          probe_files_on_disk() == [], str(probe_files_on_disk()))

    # --- vale UNA volta sola -------------------------------------------------
    r = client.post(f"/api/projects/{p_clear['id']}/files", headers=auth,
                    data={"probe_id": pr["probe_id"]})
    check("biglietto già usato: 410", r.status_code == 410, r.text[:120])
    r = client.post(f"/api/projects/{p_clear['id']}/files", headers=auth,
                    data={"probe_id": "0" * 32})
    check("biglietto inventato: 410", r.status_code == 410, r.text[:120])

    # --- sonda annullata (popup OCR chiuso) ---------------------------------
    pr2 = client.post("/api/projects/probe", headers=auth,
                      files={"file": ("scarta.txt", CONTENT,
                                      "text/plain")}).json()
    r = client.delete(f"/api/projects/probe/{pr2['probe_id']}", headers=auth)
    check("annullamento della sonda 200", r.status_code == 200)
    check("byte liberati subito", probe_files_on_disk() == [],
          str(probe_files_on_disk()))
    r = client.post(f"/api/projects/{p_clear['id']}/files", headers=auth,
                    data={"probe_id": pr2["probe_id"]})
    check("biglietto annullato: 410", r.status_code == 410)

    # --- il ripiego: mandare davvero il file (biglietto scaduto) -------------
    r = client.post(f"/api/projects/{p_clear['id']}/files", headers=auth,
                    files={"file": ("classico.txt", CONTENT, "text/plain")})
    check("upload col file (senza biglietto) ancora valido",
          r.status_code == 202 and r.json()["file"]["filename"]
          == "classico.txt", r.text[:120])

    # --- né file né biglietto ---------------------------------------------
    r = client.post(f"/api/projects/{p_clear['id']}/files", headers=auth,
                    data={"options": "{}"})
    check("nessuno dei due: 422", r.status_code == 422, r.text[:120])

    # --- progetto ANONIMIZZATO: cosa arriva al worker ------------------------
    captured = {}
    real_submit = jobs.submit_project

    def fake_submit(job_id, owner_id, project_id, data, filename, options=None):
        captured.update({"data": data, "filename": filename,
                         "options": options, "project_id": project_id})

    jobs.submit_project = fake_submit
    try:
        pr3 = client.post("/api/projects/probe", headers=auth,
                          files={"file": ("scansione.txt", CONTENT,
                                          "text/plain")}).json()
        r = client.post(f"/api/projects/{p_anon['id']}/files", headers=auth,
                        data={"probe_id": pr3["probe_id"],
                              "options": '{"ocr": true}'})
        job = r.json()
        check("progetto anonimizzato: 202 col descrittore del job",
              r.status_code == 202 and job.get("kind") == "project_upload"
              and job.get("status") == "queued", str(job)[:140])
        check("al worker arrivano i byte del biglietto",
              captured.get("data") == CONTENT
              and captured.get("filename") == "scansione.txt",
              str(captured.get("filename")))
        check("l'opzione OCR del popup sopravvive al biglietto",
              captured.get("options", {}).get("ocr") is True,
              str(captured.get("options")))
    finally:
        jobs.submit_project = real_submit

    # --- biglietto di un ALTRO utente ---------------------------------------
    pr4 = client.post("/api/projects/probe", headers=auth,
                      files={"file": ("altrui.txt", CONTENT,
                                      "text/plain")}).json()
    check("biglietto di un altro utente non si consuma",
          probe_store.take(pr4["probe_id"], owner_id=999) is None)
    check("e resta valido per il suo proprietario",
          probe_store.take(pr4["probe_id"], owner_id=1) is not None)

    # --- scadenza ------------------------------------------------------------
    ttl = probe_store.PROBE_TTL
    probe_store.PROBE_TTL = 0.05
    try:
        pid5 = probe_store.put(1, "vecchia.txt", "text/plain", CONTENT)
        time.sleep(0.1)
        check("biglietto scaduto: non si consuma",
              probe_store.take(pid5, owner_id=1) is None)
        check("e i suoi byte non restano su disco",
              probe_files_on_disk() == [], str(probe_files_on_disk()))
    finally:
        probe_store.PROBE_TTL = ttl

    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

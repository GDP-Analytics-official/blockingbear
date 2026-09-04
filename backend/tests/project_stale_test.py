"""Allineamento dei file di progetto alla MAPPA CORRENTE del registro.

Lo scenario: il file A viene caricato quando "inventario ricambi" è una
frase qualunque; più tardi il registro impara che va anonimizzata (da un
altro file o da una chat) e A resta indietro — lo stesso valore risulta
protetto in un file e in chiaro nell'altro. La pagina del progetto deve
accorgersene e offrire la ri-redazione.

Copre: gate sulla versione (un file appena scritto non è mai disallineato),
cache del testo redigibile, rilevazione del disallineamento con i valori
trovati, verdetto in memoria sulla riga, controllo incrementale, corpus OCR
(le scansioni, dove la copia protetta è fatta di pixel), tetto
sull'estrazione in linea + verifica esplicita, ri-redazione in coda
(kind=project_realign) ammessa sui file CONFERMATI e dopo il primo messaggio,
`/realign` sincrono lasciato com'era, conferma bloccata da un file indietro.

Uso:
    python backend/tests/project_stale_test.py
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_stale")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

from fastapi import FastAPI                                     # noqa: E402
from fastapi.testclient import TestClient                       # noqa: E402

from app import jobs, project_files                             # noqa: E402
from app.auth import seed_admin                                 # noqa: E402
from app.db import (ChatMessage, Conversation, Job, Project,    # noqa: E402
                    ProjectFile, init_db)
from app.routes import auth_routes, projects                    # noqa: E402

PASS = 0
FAIL = 0

FILE_A = ("Cliente: Mario Rossi\n"
          "Nota: inventario ricambi da consegnare in sede.\n")
FILE_B = ("Fornitore: Beta Costruzioni Srl\n"
          "Nota: inventario ricambi in magazzino.\n")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def row_of(client, auth, pid, fid):
    files = client.get(f"/api/projects/{pid}", headers=auth).json()["files"]
    return next(f for f in files if f["id"] == fid)


def run_job(SessionLocal, job_id, pid, fid):
    """Qui nessun worker gira: si annulla il job accodato (così il file non
    resta bloccato) e si fa il suo lavoro chiamando direttamente il codice
    che il worker chiamerebbe."""
    with SessionLocal() as s:
        jobs.cancel(s.get(Job, job_id), s)
    with SessionLocal() as s:
        project_files.realign({"project_id": pid, "file_id": fid}, s)


def main():
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(projects.router)
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)

    client = TestClient(app)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    engine = jobs.ENGINES[0]

    pid = client.post("/api/projects", json={
        "name": "Allineamento", "anonymized": True},
        headers=auth).json()["id"]

    # --- file A: appena scritto è allineato per definizione ------------------
    with SessionLocal() as s:
        pfa_id = project_files.process_upload(
            FILE_A.encode(), "clienti.txt", pid, engine, s).id
    row = row_of(client, auth, pid, pfa_id)
    check("file appena elaborato: allineato", row["stale"] is False,
          json.dumps(row["stale_values"]))
    with SessionLocal() as s:
        pfa = s.get(ProjectFile, pfa_id)
        cache = project_files.text_cache_path(pfa)
        blob = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else {}
        check("cache del testo scritta dalla redazione",
              blob.get("rev") == pfa.rev
              and "inventario ricambi" in (blob.get("text") or ""))
        v_written = pfa.mapping_version
    r = client.post(f"/api/projects/{pid}/files/{pfa_id}/confirm", headers=auth)
    check("file A confermato", r.status_code == 200, r.text[:120])

    # --- il registro impara qualcosa DOPO ------------------------------------
    with SessionLocal() as s:
        pfb_id = project_files.process_upload(
            FILE_B.encode(), "fornitori.txt", pid, engine, s).id
    r = client.post(f"/api/projects/{pid}/files/{pfb_id}/anonymize-text",
                    json={"text": "inventario ricambi"}, headers=auth)
    custom_ph = r.json().get("added", "")
    check("registro aggiornato dopo la scrittura di A",
          custom_ph.startswith("[CUSTOM_"), r.text[:120])
    with SessionLocal() as s:
        check("la versione del progetto è avanzata",
              (s.get(Project, pid).mapping_version or 0) > v_written)

    row = row_of(client, auth, pid, pfa_id)
    check("file A segnalato come non allineato", row["stale"] is True)
    check("il valore rimasto in chiaro è quello giusto",
          row["stale_values"] == ["inventario ricambi"],
          json.dumps(row["stale_values"]))
    rowb = row_of(client, auth, pid, pfb_id)
    check("file B (riscritto col registro nuovo) resta allineato",
          rowb["stale"] is False, json.dumps(rowb["stale_values"]))

    # verdetto in memoria: la seconda apertura non ricalcola
    with SessionLocal() as s:
        pfa = s.get(ProjectFile, pfa_id)
        project = s.get(Project, pid)
        check("verdetto memorizzato alla versione corrente",
              pfa.stale_version == (project.mapping_version or 0)
              and json.loads(pfa.stale_json)[0][1] == "inventario ricambi",
              f"{pfa.stale_version} vs {project.mapping_version}")
        # il controllo incrementale non rilegge ciò che ha già scartato:
        # senza testo (cache cancellata) il verdetto noto resta in piedi
        project_files.text_cache_path(pfa).unlink(missing_ok=True)
        again = project_files.stale_leaks(s, project, pfa)
        check("verdetto riletto senza riaprire il file",
              [tuple(x) for x in again] == [(custom_ph, "inventario ricambi")],
              json.dumps(again))

    # --- conferma e riallineamento -------------------------------------------
    r = client.post(f"/api/projects/{pid}/files/{pfa_id}/realign", headers=auth)
    check("il /realign sincrono resta vietato sui confermati",
          r.status_code == 409, r.text[:80])
    r = client.post(f"/api/projects/{pid}/files/{pfa_id}/rebuild", headers=auth)
    check("ri-redazione accodata (202, kind=project_realign)",
          r.status_code == 202 and r.json().get("kind") == "project_realign",
          r.text[:120])
    row = row_of(client, auth, pid, pfa_id)
    check("file in coda: allineamento non dichiarato",
          row["stale"] is None and row["busy"] is True)
    run_job(SessionLocal, r.json()["id"], pid, pfa_id)
    r = client.get(f"/api/projects/{pid}/files/{pfa_id}/download", headers=auth)
    check("file A ri-redatto col registro corrente",
          "inventario ricambi" not in r.text and custom_ph in r.text,
          r.text[:80])
    row = row_of(client, auth, pid, pfa_id)
    check("dopo la ri-redazione il file risulta allineato",
          row["stale"] is False and row["confirmed"] is True)

    # --- dopo il primo messaggio la ri-redazione resta ammessa (additiva) -----
    with SessionLocal() as s:
        conv = Conversation(owner_id=1, title="c", project_id=pid,
                            anonymized=1)
        s.add(conv)
        s.flush()
        s.add(ChatMessage(conv_id=conv.id, seq=1, role="user", content="ciao"))
        s.commit()
        check("il progetto ha messaggi",
              project_files.project_has_messages(s, pid))
    r = client.post(f"/api/projects/{pid}/files/{pfa_id}/rebuild", headers=auth)
    check("ri-redazione ammessa anche dopo il primo messaggio",
          r.status_code == 202, r.text[:120])
    run_job(SessionLocal, r.json()["id"], pid, pfa_id)

    # --- corpus OCR: le scansioni, dove la copia protetta è pixel ------------
    # si finge la cache OCR di un file già elaborato: il valore vive solo lì
    with SessionLocal() as s:
        pfb = s.get(ProjectFile, pfb_id)
        project_files.ocr_cache_path(pfb).write_text(json.dumps({
            "images": [{"lines": [{"t": "Perizia di Carla Fumagalli"}],
                        "plan": []}]}), encoding="utf-8")
        project_files.text_cache_path(pfb).unlink(missing_ok=True)
    r = client.post(f"/api/projects/{pid}/files/{pfb_id}/anonymize-text",
                    json={"text": "Carla Fumagalli", "term_tag": "FULLNAME"},
                    headers=auth)
    check("valore delle immagini imparato dal registro",
          r.status_code == 200, r.text[:120])
    with SessionLocal() as s:
        pfa = s.get(ProjectFile, pfa_id)
        pfa.mapping_version = v_written        # A torna "vecchio" per il test
        s.commit()
    with SessionLocal() as s:
        pfb = s.get(ProjectFile, pfb_id)
        pfb.mapping_version = v_written
        pfb.stale_version = -1
        s.commit()
    row = row_of(client, auth, pid, pfb_id)
    check("il testo dentro le immagini entra nel controllo",
          row["stale"] is True and "Carla Fumagalli" in row["stale_values"],
          json.dumps(row["stale_values"]))

    # --- tetto sull'estrazione in linea + verifica esplicita ------------------
    saved = project_files._INLINE_EXTRACT_MAX
    project_files._INLINE_EXTRACT_MAX = 1      # qualunque file supera il tetto
    try:
        with SessionLocal() as s:
            pfa = s.get(ProjectFile, pfa_id)
            project_files.text_cache_path(pfa).unlink(missing_ok=True)
            pfa.stale_version = -1
            s.commit()
        row = row_of(client, auth, pid, pfa_id)
        check("file troppo grosso: non verificato all'apertura",
              row["stale"] is None, json.dumps(row["stale"]))
        r = client.post(f"/api/projects/{pid}/files/{pfa_id}/stale-check",
                        headers=auth)
        check("la verifica esplicita non ha tetto",
              r.status_code == 200 and r.json()["file"]["stale"] is True,
              r.text[:140])
    finally:
        project_files._INLINE_EXTRACT_MAX = saved

    # --- la conferma di un file indietro resta bloccata -----------------------
    with SessionLocal() as s:
        pfb = s.get(ProjectFile, pfb_id)
        pfb.confirmed = 0
        s.commit()
    r = client.post(f"/api/projects/{pid}/files/{pfb_id}/confirm", headers=auth)
    check("conferma bloccata da un file non allineato", r.status_code == 409,
          r.text[:140])

    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

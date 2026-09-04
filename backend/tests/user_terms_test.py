"""Termini di anonimizzazione a DUE livelli.

  - lista GLOBALE  (settings.anon_custom_terms): la scrive solo l'admin dal
    pannello, vale per tutti e nessuno se la può togliere;
  - lista PERSONALE (User.anon_terms_json): ognuno gestisce la sua, admin
    compreso, e non la vede nessun altro.

Quella che arriva al motore è sempre l'UNIONE (settings_store.merge_terms),
calcolata in un punto solo: chat_anonymization.anon_options. La lista personale
che si applica è quella del PROPRIETARIO DEL REGISTRO (progetto o chat), non
di chi fa la richiesta — i job in background girano senza utente collegato, e
un admin che apre il progetto di un altro non deve ri-redigerlo coi propri
termini.

Copre: merge_terms/user_terms/set_user_terms, endpoint GET /anonymization e
PUT /my-terms (permessi inclusi), la risoluzione per-proprietario in
anon_options, l'isolamento fra due utenti e la redazione end-to-end di un file
di progetto (il termine personale deve sparire davvero dal testo protetto).

Uso:  python backend/tests/user_terms_test.py
"""
import io
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DATA_DIR = HERE / "data" / "test_user_terms"
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA_DIR)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import (chat_anonymization as ca, jobs, project_files,  # noqa: E402
                 settings_store)
from app.auth import seed_admin                                  # noqa: E402
from app.db import Project, User, init_db                        # noqa: E402
from app.engine import convert                                   # noqa: E402
from app.routes import auth_routes, projects, settings_routes    # noqa: E402
from app.routes import users                                     # noqa: E402

convert._PROFILE_DIR = DATA_DIR / "lo_profile"

PASS = 0
FAIL = 0

TXT = ("Cliente: Mario Rossi\n"
       "Nota interna: il dossier Cormorano segue la pratica.\n"
       "Riferimento: commessa Belvedere, consegna a settembre.\n"
       "Sigla operativa: zona franca est.\n")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def user_by(session, username):
    return session.query(User).filter_by(username=username).one()


def main():
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)

    # --- 1. merge_terms: unione, dedup, precedenza del globale ---------------
    print("\n[1] settings_store.merge_terms")
    g = [{"text": "Ragione Sociale", "tag": "AZIENDA"}]
    p = [{"text": "ragione   sociale", "tag": "MIO"},
         {"text": "dossier Cormorano", "tag": "DOSSIER"}]
    merged = settings_store.merge_terms(g, p)
    check("unione delle due liste",
          merged == [{"text": "Ragione Sociale", "tag": "AZIENDA"},
                     {"text": "dossier Cormorano", "tag": "DOSSIER"}], merged)
    check("collisione: vince il globale (tag AZIENDA, non MIO)",
          merged[0]["tag"] == "AZIENDA")
    check("il dedup normalizza gli spazi come clean_terms",
          len(merged) == 2, merged)
    scoped = settings_store.merge_terms(g, p, scoped=True)
    check("scoped annota la provenienza",
          [t["scope"] for t in scoped] == ["global", "personal"], scoped)
    check("senza scoped nessuna chiave in più (va al motore)",
          all(set(t) == {"text", "tag"} for t in merged))
    check("liste vuote / None", settings_store.merge_terms(None, None) == []
          and settings_store.merge_terms([], p) == p)

    # --- 2. user_terms: letture difensive ------------------------------------
    print("\n[2] settings_store.user_terms")
    with SessionLocal() as s:
        u = user_by(s, "admin")
        settings_store.set_user_terms(s, u, [{"text": "Foo Bar", "tag": "x"}])
        check("salva e rilegge normalizzato",
              settings_store.user_terms(u) == [{"text": "Foo Bar", "tag": "X"}])
        u.anon_terms_json = "{non è json"
        check("JSON corrotto -> lista vuota, non un'eccezione",
              settings_store.user_terms(u) == [])
        check("utente None -> lista vuota", settings_store.user_terms(None) == [])
        settings_store.set_user_terms(s, u, [])
        try:
            settings_store.set_user_terms(s, u, [{"text": "a", "tag": "X"}])
            check("termine troppo corto -> ValueError", False)
        except ValueError:
            check("termine troppo corto -> ValueError", True)

    # --- 3. endpoint ----------------------------------------------------------
    print("\n[3] GET /api/settings/anonymization e PUT /api/settings/my-terms")
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(projects.router)
    app.include_router(settings_routes.router)
    app.include_router(users.router)
    client = TestClient(app)

    def login(username, password):
        r = client.post("/api/auth/login",
                        json={"username": username, "password": password})
        return {"Authorization": f"Bearer {r.json()['token']}"}

    admin = login("admin", "admin")
    r = client.post("/api/users", json={"username": "mario",
                                        "password": "mario12345",
                                        "role": "standard"}, headers=admin)
    check("utente standard creato", r.status_code in (200, 201), r.text[:150])
    mario = login("mario", "mario12345")

    # l'admin fissa un termine globale
    r = client.put("/api/settings/anonymization",
                   json={"excluded_tags": [],
                         "custom_terms": [{"text": "commessa Belvedere",
                                           "tag": "COMMESSA"}]},
                   headers=admin)
    check("admin salva la lista globale", r.status_code == 200, r.text[:150])

    r = client.put("/api/settings/anonymization",
                   json={"excluded_tags": [], "custom_terms": []},
                   headers=mario)
    check("utente standard NON può scrivere la lista globale",
          r.status_code == 403, f"{r.status_code} {r.text[:80]}")

    r = client.put("/api/settings/my-terms",
                   json={"custom_terms": [{"text": "dossier Cormorano",
                                           "tag": "dossier"}]}, headers=mario)
    check("utente standard salva i PROPRI termini", r.status_code == 200,
          r.text[:150])
    body = r.json()
    check("payload: globale + mia + effettiva",
          body["custom_terms"] == [{"text": "commessa Belvedere",
                                    "tag": "COMMESSA"}]
          and body["my_custom_terms"] == [{"text": "dossier Cormorano",
                                           "tag": "DOSSIER"}]
          and [t["scope"] for t in body["effective_custom_terms"]]
          == ["global", "personal"], body)

    r = client.get("/api/settings/anonymization", headers=admin)
    check("i termini di mario NON si vedono dall'admin",
          r.json()["my_custom_terms"] == []
          and r.json()["custom_terms"] == [{"text": "commessa Belvedere",
                                            "tag": "COMMESSA"}], r.json())

    r = client.put("/api/settings/my-terms",
                   json={"custom_terms": [{"text": "a", "tag": "X"}]},
                   headers=mario)
    check("termine invalido -> 422", r.status_code == 422, r.text[:100])
    r = client.get("/api/settings/anonymization", headers=mario)
    check("dopo il 422 la lista di mario è intatta",
          r.json()["my_custom_terms"] == [{"text": "dossier Cormorano",
                                           "tag": "DOSSIER"}])

    # --- 4. anon_options: la lista è quella del PROPRIETARIO ----------------
    print("\n[4] chat_anonymization.anon_options (per proprietario del registro)")
    pid = client.post("/api/projects", json={"name": "Pratiche",
                                             "anonymized": True},
                      headers=mario).json()["id"]
    with SessionLocal() as s:
        project = s.get(Project, pid)
        terms = ca.anon_options(s, project)["custom_terms"]
        texts = [t["text"] for t in terms]
        check("il progetto di mario vede globale + termini di mario",
              texts == ["commessa Belvedere", "dossier Cormorano"], texts)
        check("nessuna chiave 'scope' verso il motore",
              all(set(t) == {"text", "tag"} for t in terms))
        # l'admin si aggiunge un termine suo: non deve entrare nel progetto di mario
        settings_store.set_user_terms(s, user_by(s, "admin"),
                                      [{"text": "zona franca", "tag": "AREA"}])
    with SessionLocal() as s:
        texts = [t["text"] for t in
                 ca.anon_options(s, s.get(Project, pid))["custom_terms"]]
        check("i termini dell'admin NON entrano nel progetto di mario",
              "zona franca" not in texts, texts)

    # la vista annota la provenienza (pagina progetto / pastiglia chat)
    r = client.get(f"/api/projects/{pid}", headers=mario)
    view = r.json()["anon_options"]["custom_terms"]
    check("la vista del progetto annota lo scope",
          [(t["text"], t["scope"]) for t in view]
          == [("commessa Belvedere", "global"),
              ("dossier Cormorano", "personal")], view)

    # --- 5. redazione end-to-end --------------------------------------------
    print("\n[5] redazione di un file di progetto con i due livelli")
    with SessionLocal() as s:
        pf = project_files.process_upload(TXT.encode("utf-8"), "pratica.txt",
                                          pid, jobs.ENGINES[0], s)
        fid = pf.id
    doc = client.get(f"/api/projects/{pid}/files/{fid}", headers=mario).json()
    mapping = doc.get("mapping", {})
    values = {v for v in mapping.values()}
    check("il termine GLOBALE è stato coperto",
          any(v == "commessa Belvedere" for v in values), sorted(values))
    check("il termine PERSONALE di mario è stato coperto",
          any(v == "dossier Cormorano" for v in values), sorted(values))
    check("i placeholder usano i tag scelti",
          any(p.startswith("[COMMESSA_") for p in mapping)
          and any(p.startswith("[DOSSIER_") for p in mapping), sorted(mapping))
    # il termine dell'admin aveva tag AREA: se fosse stato applicato ci sarebbe
    # un [AREA_n]. ("zona franca est" lo copre comunque il modello come STREET:
    # controllare il valore non distinguerebbe le due cose.)
    check("il termine personale dell'ADMIN non è stato applicato",
          not any(p.startswith("[AREA_") for p in mapping), sorted(mapping))

    print(f"\nRISULTATO: {PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

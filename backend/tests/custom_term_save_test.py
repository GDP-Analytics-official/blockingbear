"""Checkbox «anonimizza anche in futuro» del popup di selezione manuale:
il flag save_term di POST /api/projects/{id}/files/{fid}/anonymize-text
aggiunge il testo ai termini fissi DOPO la ri-redazione riuscita.

Il termine finisce nella lista PERSONALE di chi clicca
(User.anon_terms_json), non più in quella globale dell'amministratore: la
lista globale resta scrivibile solo dal pannello admin. Il dedup però guarda
la lista EFFETTIVA (globale + personale), così un termine già imposto
dall'admin non viene ricopiato tra i personali.

Copre: settings_store.add_custom_term (aggiunta, dedup case-insensitive contro
entrambi i livelli, conservazione della lista esistente, tag, validazione) e
l'endpoint dei file di progetto end-to-end (elaborazione txt sincrona via
project_files.process_upload, senza coda). Il lato chat
(staged/anonymize-text) è coperto da chat_staging_test; i due livelli e la
loro fusione da user_terms_test.

Uso:  python backend/tests/custom_term_save_test.py
"""
import io
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DATA_DIR = HERE / "data" / "test_custom_term"
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA_DIR)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import jobs, project_files, settings_store              # noqa: E402
from app.auth import seed_admin                                  # noqa: E402
from app.db import User, init_db                                 # noqa: E402
from app.engine import convert                                   # noqa: E402
from app.routes import auth_routes, projects, settings_routes    # noqa: E402

# profilo LibreOffice isolato: le conversioni non devono contendersi il
# profilo di un eventuale backend in esecuzione
convert._PROFILE_DIR = DATA_DIR / "lo_profile"

PASS = 0
FAIL = 0

TXT = ("Cliente: Mario Rossi\n"
       "Email: mario.rossi@example.com\n"
       "Nota: inventario ricambi da consegnare al cliente.\n"
       "Progetto Aurora, consegna prevista a settembre.\n"
       "Sede operativa: zona franca est.\n")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def admin_of(session):
    return session.query(User).filter_by(username="admin").one()


def terms_of(session_factory):
    """La lista PERSONALE dell'admin: è lì che la checkbox salva."""
    with session_factory() as s:
        return settings_store.user_terms(admin_of(s))


def global_terms_of(session_factory):
    with session_factory() as s:
        return settings_store.anon_defaults(s)["custom_terms"]


def main():
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)

    # --- add_custom_term (unità) --------------------------------------------
    print("\n[1] settings_store.add_custom_term")
    with SessionLocal() as s:
        u = admin_of(s)
        settings_store.set_anon_defaults(s, [], [])
        settings_store.set_user_terms(s, u, [{"text": "Foo Bar", "tag": "CUSTOM"}])
        check("aggiunta nuova",
              settings_store.add_custom_term(s, u, "Progetto Aurora"))
        got = settings_store.user_terms(u)
        check("lista esistente conservata + nuova voce",
              got == [{"text": "Foo Bar", "tag": "CUSTOM"},
                      {"text": "Progetto Aurora", "tag": "CUSTOM"}], got)
        check("dedup case-insensitive -> False",
              settings_store.add_custom_term(s, u, "  progetto   AURORA ") is False)
        check("dedup non duplica", len(settings_store.user_terms(u)) == 2)
        check("tag esplicito",
              settings_store.add_custom_term(s, u, "Sigla XY", tag="codice interno")
              and settings_store.user_terms(u)[-1]
              == {"text": "Sigla XY", "tag": "CODICEINTERNO"})
        check("la lista GLOBALE non viene toccata",
              settings_store.anon_defaults(s)["custom_terms"] == [],
              settings_store.anon_defaults(s)["custom_terms"])
        # già imposto dall'admin a tutti: non si ricopia tra i personali
        settings_store.set_user_terms(s, u, [])
        settings_store.set_anon_defaults(
            s, [], [{"text": "Ragione Sociale", "tag": "AZIENDA"}])
        check("già nella lista globale -> False, personale vuota",
              settings_store.add_custom_term(s, u, "ragione  sociale") is False
              and settings_store.user_terms(u) == [])
        try:
            settings_store.add_custom_term(s, u, "a")
            check("termine troppo corto -> ValueError", False)
        except ValueError:
            check("termine troppo corto -> ValueError", True)
        # tabula rasa per la parte endpoint
        settings_store.set_anon_defaults(s, [], [])
        settings_store.set_user_terms(s, u, [])

    # --- endpoint file di progetto ---------------------------------------------
    print("\n[2] POST /api/projects/{id}/files/{fid}/anonymize-text con save_term")
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(projects.router)
    app.include_router(settings_routes.router)
    client = TestClient(app)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    pid = client.post("/api/projects", json={
        "name": "Termini fissi", "anonymized": True},
        headers=auth).json()["id"]
    # elaborazione sincrona (stessa strada del worker, senza coda né SSE)
    with SessionLocal() as s:
        pf = project_files.process_upload(TXT.encode("utf-8"), "clienti.txt",
                                          pid, jobs.ENGINES[0], s)
        fid = pf.id
    base = f"/api/projects/{pid}/files/{fid}"
    print(f"  (file di progetto {fid} creato)")

    r = client.post(f"{base}/anonymize-text",
                    json={"text": "inventario ricambi", "save_term": True},
                    headers=auth)
    check("anonimizza + salva termine", r.status_code == 200
          and r.json().get("added", "").startswith("[CUSTOM_")
          and r.json().get("saved_term") is True, r.text[:120])
    saved = terms_of(SessionLocal)
    check("termine nella lista PERSONALE di chi ha cliccato",
          saved == [{"text": "inventario ricambi", "tag": "CUSTOM"}], saved)
    check("la lista globale resta vuota", global_terms_of(SessionLocal) == [],
          global_terms_of(SessionLocal))
    r = client.get("/api/settings/anonymization", headers=auth)
    check("visibile da GET /api/settings/anonymization come my_custom_terms",
          r.status_code == 200 and r.json()["my_custom_terms"] == saved
          and r.json()["custom_terms"] == []
          and [t["scope"] for t in r.json()["effective_custom_terms"]]
          == ["personal"], r.text[:200])

    r = client.post(f"{base}/anonymize-text",
                    json={"text": "Progetto Aurora"}, headers=auth)
    check("senza save_term: anonimizza e basta", r.status_code == 200
          and "saved_term" not in r.json(), r.text[:120])
    check("termini invariati", terms_of(SessionLocal) == saved)

    r = client.post(f"{base}/anonymize-text",
                    json={"text": "consegna prevista", "save_term": False},
                    headers=auth)
    check("save_term esplicito False: non salva", r.status_code == 200
          and terms_of(SessionLocal) == saved, r.text[:120])

    # anonimizzazione fallita (testo non nel file) -> termine NON salvato
    r = client.post(f"{base}/anonymize-text",
                    json={"text": "testo che non esiste da nessuna parte",
                          "save_term": True}, headers=auth)
    check("redazione fallita (422): termine non salvato",
          r.status_code == 422 and terms_of(SessionLocal) == saved, r.text[:100])

    # dedup dall'endpoint: valore già nei termini (ma non in mappa)
    with SessionLocal() as s:
        settings_store.add_custom_term(s, admin_of(s), "consegna prevista")
    r = client.post(f"{base}/anonymize-text",
                    json={"text": "Consegna  prevista", "save_term": True},
                    headers=auth)
    check("già nei termini -> saved_term False, nessun doppione",
          r.status_code == 409 or (r.status_code == 200
          and r.json().get("saved_term") is False), r.text[:120])
    check("nessun duplicato in lista",
          len(terms_of(SessionLocal)) == len(saved) + 1)

    # --- tag manuale del termine fisso (term_tag) ------------------------------
    r = client.post(f"{base}/anonymize-text",
                    json={"text": "consegnare al cliente", "save_term": True,
                          "term_tag": "codice progetto"}, headers=auth)
    check("term_tag normalizzato (clean_tag)", r.status_code == 200
          and r.json().get("saved_term") is True
          and terms_of(SessionLocal)[-1]
          == {"text": "consegnare al cliente", "tag": "CODICEPROGETTO"},
          r.text[:200])
    # il tag scelto vale ANCHE per il segnaposto di questa ri-redazione: se il
    # registro tenesse [CUSTOM_n] l'utente vedrebbe nell'anteprima un tag
    # diverso da quello che ha appena scritto (e diverso da quello che i file
    # successivi useranno per lo stesso termine)
    doc = client.get(base, headers=auth).json()
    check("il segnaposto usa il tag scelto, non CUSTOM",
          r.json().get("added") == "[CODICEPROGETTO_1]"
          and doc["mapping"].get("[CODICEPROGETTO_1]") == "consegnare al cliente",
          r.json().get("added"))

    before = terms_of(SessionLocal)
    doc_before = client.get(base, headers=auth).json()
    r = client.post(f"{base}/anonymize-text",
                    json={"text": "Sede operativa", "save_term": True,
                          "term_tag": "!!!"}, headers=auth)
    doc_after = client.get(base, headers=auth).json()
    check("tag invalido -> 422 senza effetti collaterali",
          r.status_code == 422 and terms_of(SessionLocal) == before
          and doc_after["mapping"] == doc_before["mapping"], r.text[:100])

    print(f"\nRISULTATO: {PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

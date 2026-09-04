"""Ricerca nelle chat (GET /api/chats/search): la lente nella
sidebar. Substring case-insensitive per termine + ripescaggio fuzzy dei typo
(LIKE sul prefisso in SQL, conferma difflib in Python sui soli candidati).

Copre: match nei messaggi e nei titoli, ranking per copertura dei termini,
typo in coda alla parola, escape dei metacaratteri LIKE, isolamento per
utente e dalle chat di progetto, ruoli esclusi (system/tool), messaggi
anonimizzati cercati sull'originale (display_content), snippet con finestra,
query vuote o troppo corte, ordine di registrazione della route (search non
deve finire in GET /api/chats/{conv_id}).

Uso:  python backend/tests/chat_search_test.py
"""
import datetime
import io
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

DATA_DIR = HERE / "data" / "test_chat_search"
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA_DIR)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

from fastapi import FastAPI                                       # noqa: E402
from fastapi.testclient import TestClient                         # noqa: E402

from app.auth import hash_password, seed_admin                    # noqa: E402
from app.db import ChatMessage, Conversation, User, init_db       # noqa: E402
from app.routes import auth_routes, chat_routes                   # noqa: E402

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


def _when(days_ago):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=days_ago))


def seed(SessionLocal):
    """Le chat del test. L'admin ha quattro chat libere + una di progetto;
    «carla» ha una chat sua che all'admin non deve mai comparire."""
    with SessionLocal() as s:
        admin = s.query(User).filter_by(username="admin").one()
        carla = User(username="carla", password_hash=hash_password("pw"),
                     role="user")
        s.add(carla)
        s.flush()

        def conv(owner, title, days_ago, project_id=None, anonymized=0):
            c = Conversation(owner_id=owner.id, title=title,
                             project_id=project_id, anonymized=anonymized,
                             created_at=_when(days_ago),
                             updated_at=_when(days_ago))
            s.add(c)
            s.flush()
            return c

        def msg(c, seq, role, content, display=None, days_ago=0):
            s.add(ChatMessage(conv_id=c.id, seq=seq, role=role,
                              content=content, display_content=display,
                              created_at=_when(days_ago)))

        # 1. la chat "fattura": termine nei messaggi, per snippet e n_hits
        fatture = conv(admin, "Contabilità fornitori", 1)
        msg(fatture, 0, "user",
            "Mi controlli la fattura 2026/041 del fornitore Rossi? "
            "L'imponibile non torna con l'ordine.", days_ago=1)
        msg(fatture, 1, "assistant",
            "Certo: nella fattura il totale imponibile è 1.200 euro.",
            days_ago=1)

        # 2. termine SOLO nel titolo, messaggi che non c'entrano
        bilancio = conv(admin, "Bozza bilancio 2026", 2)
        msg(bilancio, 0, "user", "Riassumi il documento allegato.",
            days_ago=2)

        # 3. copre DUE termini (fattura+bilancio): deve battere le chat che
        #    ne coprono uno solo, anche se è la più vecchia
        entrambe = conv(admin, "Chiusura trimestre", 9)
        msg(entrambe, 0, "user",
            "Prepara il bilancio del trimestre partendo da ogni fattura "
            "registrata a marzo.", days_ago=9)

        # 4. chat anonimizzata: content coi TAG, display_content in chiaro;
        #    typo-bersaglio "anonimizzazione" per il fuzzy
        anon = conv(admin, "Pratica riservata", 3, anonymized=1)
        msg(anon, 0, "user",
            "Scrivi una lettera per [FULLNAME_1] di [ORG_1].",
            display="Scrivi una lettera per Mario Verdi di Acme SpA.",
            days_ago=3)
        msg(anon, 1, "assistant",
            "Ecco la lettera per [FULLNAME_1]: l'anonimizzazione resta "
            "attiva.", days_ago=3)

        # rumore che NON deve mai uscire: system/tool, progetto, altro utente
        msg(fatture, 2, "system", "Il glossario segreto: fattura, bilancio.")
        msg(fatture, 3, "tool", '{"esito": "fattura elaborata"}')
        proj = conv(admin, "Fatture del progetto", 0, project_id="p1")
        msg(proj, 0, "user", "Analizza questa fattura di progetto.")
        di_carla = conv(carla, "Le fatture di Carla", 0)
        msg(di_carla, 0, "user", "La mia fattura personale.")

        # per l'escape dei metacaratteri LIKE
        strana = conv(admin, "Note varie", 5)
        msg(strana, 0, "user", "Lo sconto è del 25%_netto sul listino.",
            days_ago=5)

        s.commit()
        return {"fatture": fatture.id, "bilancio": bilancio.id,
                "entrambe": entrambe.id, "anon": anon.id,
                "proj": proj.id, "carla": di_carla.id,
                "strana": strana.id}


def main():
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(chat_routes.router)
    ids = seed(SessionLocal)

    client = TestClient(app)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    def search(q):
        r = client.get("/api/chats/search", params={"q": q}, headers=auth)
        assert r.status_code == 200, r.text
        return r.json()

    # --- 1. match di base nei messaggi ---------------------------------------
    print("\n[1] substring nei messaggi")
    rs = search("fattura")
    got = {r["id"] for r in rs}
    check("trova le chat coi messaggi giusti",
          {ids["fatture"], ids["entrambe"]} <= got, str(got))
    check("niente chat di progetto", ids["proj"] not in got)
    check("niente chat di altri utenti", ids["carla"] not in got)
    check("system e tool non si cercano",
          ids["bilancio"] not in got)  # 'fattura' sta solo nel system/tool
    fat = next(r for r in rs if r["id"] == ids["fatture"])
    check("n_hits conta i messaggi che matchano", fat["n_hits"] == 2,
          str(fat["n_hits"]))
    check("lo snippet contiene il termine col contesto",
          "fattura" in fat["snippet"].lower()
          and "fornitore" in fat["snippet"].lower(), repr(fat["snippet"]))
    check("il descrittore dice se la chat è anonimizzata",
          fat["anonymized"] is False)

    # --- 2. case-insensitive ---------------------------------------------------
    print("\n[2] maiuscole/minuscole")
    check("FATTURA come fattura",
          {r["id"] for r in search("FATTURA")} == got)

    # --- 3. titoli --------------------------------------------------------------
    print("\n[3] match sul titolo")
    rs = search("bilancio")
    got = {r["id"] for r in rs}
    check("il titolo basta anche senza messaggi",
          ids["bilancio"] in got, str(got))
    bil = next(r for r in rs if r["id"] == ids["bilancio"])
    check("solo titolo: nessun messaggio contato e niente snippet",
          bil["n_hits"] == 0 and bil["snippet"] == "", repr(bil))

    # --- 4. ranking per copertura dei termini ----------------------------------
    print("\n[4] due termini battono uno")
    rs = search("fattura bilancio")
    check("la chat che copre entrambi sta in cima (anche se vecchia)",
          rs and rs[0]["id"] == ids["entrambe"],
          str([r["title"] for r in rs]))
    check("le chat con un termine solo seguono",
          {ids["fatture"], ids["bilancio"]} <= {r["id"] for r in rs[1:]})

    # --- 5. fuzzy sui typo -------------------------------------------------------
    print("\n[5] typo in coda alla parola")
    rs = search("anonimizazione")     # manca una 'z'
    check("il typo trova comunque la parola giusta",
          ids["anon"] in {r["id"] for r in rs}, str([r["title"] for r in rs]))
    rs = search("fatura")             # manca una 't': prefisso 'fatu' non c'è
    check("typo dentro il prefisso: non ripescato (limite accettato)",
          ids["fatture"] not in {r["id"] for r in rs})

    # --- 6. chat anonimizzate: si cerca l'originale ------------------------------
    print("\n[6] chat anonimizzate")
    rs = search("mario verdi")
    check("il nome in chiaro si trova nel display_content",
          ids["anon"] in {r["id"] for r in rs}, str([r["title"] for r in rs]))
    anon = next(r for r in rs if r["id"] == ids["anon"])
    check("lo snippet mostra l'originale, non i TAG",
          "Mario Verdi" in anon["snippet"]
          and "[FULLNAME_1]" not in anon["snippet"], repr(anon["snippet"]))

    # --- 7. escape dei metacaratteri LIKE ----------------------------------------
    print("\n[7] metacaratteri LIKE")
    rs = search("25%_netto")
    check("percento e underscore sono letterali",
          {r["id"] for r in rs} == {ids["strana"]},
          str([r["title"] for r in rs]))
    check("un pattern-tutto non matcha tutto", search("%%") == [])

    # --- 8. query degeneri ---------------------------------------------------------
    print("\n[8] query vuote o corte")
    check("query vuota: lista vuota", search("") == [])
    check("un solo carattere: lista vuota", search("a") == [])
    check("'search' non finisce nella route {conv_id}",
          client.get("/api/chats/search", headers=auth).status_code == 200)

    print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

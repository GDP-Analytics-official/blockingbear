"""Lingua dell'interfaccia: il giro completo dal profilo.

La lingua NON sta nel localStorage del browser: l'unico
posto dove vive è `User.lang`. La schermata di accesso è scritta per non
dipendere da nessuna delle due lingue e la domanda si fa DOPO il login, così
c'è un profilo su cui salvarla e non la si chiede mai più.

Il perno di quel flusso è un valore solo: `lang` = null nella risposta di
/login e /me significa «mai scelta, chiediglielo». È un caso che prima
serviva a poco (il browser aveva comunque la sua copia) e che adesso è l'unica
cosa che fa comparire il popup: se qualcuno lo facesse diventare "it" per
comodità, l'utente nuovo si troverebbe l'italiano d'ufficio e nessuna domanda.
Da qui in giù è quel contratto che si controlla.

Copre: clean_lang/user_lang/set_user_lang, `lang` in /login e /me prima e dopo
la scelta, PUT /my-language (codici regionali, valori rifiutati, senza
sessione) e l'isolamento fra due profili.

Uso:  python backend/tests/lang_flow_test.py
"""
import io
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DATA_DIR = HERE / "data" / "test_lang_flow"
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA_DIR)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import settings_store                                   # noqa: E402
from app.auth import hash_password, seed_admin                   # noqa: E402
from app.db import User, init_db                                 # noqa: E402
from app.routes import auth_routes, settings_routes              # noqa: E402

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


def login(client, username, password):
    return client.post("/api/auth/login",
                       json={"username": username, "password": password})


def main():
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)
        for name in ("mario", "anna"):
            s.add(User(username=name, password_hash=hash_password("segreto1"),
                       role="user"))
        s.commit()

    # --- 1. logica pura ------------------------------------------------------
    print("\n[1] settings_store: clean_lang / user_lang / set_user_lang")
    check("clean_lang tiene le lingue previste",
          settings_store.clean_lang("en") == "en")
    check("clean_lang normalizza le forme regionali",
          settings_store.clean_lang("it-IT") == "it"
          and settings_store.clean_lang("en_GB") == "en")
    for bad in ("", None, "fr", "italiano", "  "):
        try:
            settings_store.clean_lang(bad)
            check(f"clean_lang rifiuta {bad!r}", False, "accettato")
        except ValueError:
            check(f"clean_lang rifiuta {bad!r}", True)

    with SessionLocal() as s:
        mario = s.query(User).filter_by(username="mario").one()
        check("profilo nuovo: user_lang è None, non il default",
              settings_store.user_lang(mario) is None,
              repr(settings_store.user_lang(mario)))
        check("set_user_lang torna il codice normalizzato",
              settings_store.set_user_lang(s, mario, "EN-us") == "en")
    with SessionLocal() as s:
        mario = s.query(User).filter_by(username="mario").one()
        check("la scelta è finita in colonna", mario.lang == "en", mario.lang)
        # si riporta indietro: il giro dalle route riparte da un profilo vergine
        mario.lang = None
        s.commit()

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(settings_routes.router)
    client = TestClient(app)

    # --- 2. prima della scelta: il null che fa comparire il popup ------------
    print("\n[2] /login e /me su un profilo senza lingua")
    res = login(client, "mario", "segreto1").json()
    tok_m = {"Authorization": "Bearer " + res["token"]}
    check("/login dice lang = null", "lang" in res and res["lang"] is None,
          repr(res.get("lang")))
    me = client.get("/api/auth/me", headers=tok_m).json()
    check("/me dice lang = null", me["lang"] is None, repr(me["lang"]))

    # --- 3. la risposta al popup --------------------------------------------
    print("\n[3] PUT /api/settings/my-language")
    r = client.put("/api/settings/my-language", json={"lang": "en"},
                   headers=tok_m)
    check("il salvataggio risponde 200 col codice", r.status_code == 200
          and r.json()["lang"] == "en", r.text[:120])
    check("/me la ritorna subito",
          client.get("/api/auth/me", headers=tok_m).json()["lang"] == "en")
    check("al prossimo accesso arriva col login (niente popup)",
          login(client, "mario", "segreto1").json()["lang"] == "en")

    r = client.put("/api/settings/my-language", json={"lang": "it-IT"},
                   headers=tok_m)
    check("accetta anche la forma regionale e la normalizza",
          r.status_code == 200 and r.json()["lang"] == "it", r.text[:120])

    # --- 4. quello che non deve passare --------------------------------------
    print("\n[4] valori rifiutati e accessi senza sessione")
    for bad in ("fr", "", "it it", None):
        r = client.put("/api/settings/my-language", json={"lang": bad},
                       headers=tok_m)
        check(f"lang={bad!r} rifiutato", r.status_code >= 400,
              f"HTTP {r.status_code}")
    check("un valore rifiutato non tocca la scelta precedente",
          client.get("/api/auth/me", headers=tok_m).json()["lang"] == "it")
    r = client.put("/api/settings/my-language", json={"lang": "en"})
    check("senza sessione non si salva niente", r.status_code in (401, 403),
          f"HTTP {r.status_code}")

    # --- 5. due profili sulla stessa postazione ------------------------------
    # È il motivo per cui la lingua ha lasciato il localStorage: la scelta di
    # mario non deve diventare quella di chi si collega dopo di lui.
    print("\n[5] isolamento fra profili")
    res_a = login(client, "anna", "segreto1").json()
    tok_a = {"Authorization": "Bearer " + res_a["token"]}
    check("anna entra ancora senza lingua", res_a["lang"] is None,
          repr(res_a["lang"]))
    client.put("/api/settings/my-language", json={"lang": "en"}, headers=tok_a)
    check("anna ha la sua",
          client.get("/api/auth/me", headers=tok_a).json()["lang"] == "en")
    check("mario ha la propria, invariata",
          client.get("/api/auth/me", headers=tok_m).json()["lang"] == "it")

    print(f"\nRISULTATO: {PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

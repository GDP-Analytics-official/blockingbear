"""Cambio password: dalle impostazioni e obbligatorio al primo accesso.

Due pezzi, nessuna email di conferma (app locale):
- POST /api/auth/password: chiunque cambia la propria conoscendo quella
  attuale. Vecchia sbagliata = 403 (NON 401: per il frontend un 401 con token
  in mano è «sessione morta» e fa il logout d'ufficio); nuova uguale alla
  vecchia = 422 (col cambio obbligatorio, riconfermare quella d'ufficio
  vanificherebbe il flag).
- User.must_change_password: l'admin lo alza alla creazione dell'account.
  Finché è vero la sessione vale SOLO per cambiare la password: tutto il
  resto risponde 403 password_change_required, tranne /me e /password (anche
  la lingua è bloccata: il popup compare DOPO, al primo ingresso nell'app).
  Il flag cade dentro il cambio password stesso.

Uso:  python backend/tests/password_change_test.py
"""
import io
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DATA_DIR = HERE / "data" / "test_password_change"
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA_DIR)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

from fastapi import FastAPI                                       # noqa: E402
from fastapi.responses import JSONResponse                        # noqa: E402
from fastapi.testclient import TestClient                         # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from app.auth import seed_admin, verify_password                  # noqa: E402
from app.db import User, init_db                                  # noqa: E402
from app.routes import auth_routes, settings_routes, users        # noqa: E402

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


def change_pw(client, headers, old, new):
    return client.post("/api/auth/password",
                       json={"old_password": old, "new_password": new},
                       headers=headers)


def main():
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(users.router)

    # stesso gestore di main.py: senza, le risposte d'errore non avrebbero
    # `code` e qui i codici sono metà del contratto
    @app.exception_handler(StarletteHTTPException)
    async def _api_error(request, exc):
        body = {"detail": exc.detail}
        if getattr(exc, "code", None):
            body["code"] = exc.code
        return JSONResponse(status_code=exc.status_code, content=body)

    client = TestClient(app)

    # --- 1. il seed non impone niente ----------------------------------------
    print("\n[1] admin/admin dal seed")
    res = login(client, "admin", "admin").json()
    admin = {"Authorization": "Bearer " + res["token"]}
    check("il login espone must_change_password",
          res.get("must_change_password") is False, repr(res))
    check("/me pure",
          client.get("/api/auth/me", headers=admin)
                .json()["must_change_password"] is False)

    # --- 2. creazione con e senza flag ----------------------------------------
    print("\n[2] creazione account (admin)")
    r = client.post("/api/users", headers=admin,
                    json={"username": "carla", "password": "benvenuta1",
                          "role": "standard", "must_change_password": True})
    check("creato col flag alzato", r.status_code == 201
          and r.json()["must_change_password"] is True, r.text[:120])
    r = client.post("/api/users", headers=admin,
                    json={"username": "dario", "password": "benvenuto1",
                          "role": "standard"})
    check("senza flag il default è False", r.status_code == 201
          and r.json()["must_change_password"] is False, r.text[:120])
    rows = {u["username"]: u for u in
            client.get("/api/users", headers=admin).json()}
    check("la lista utenti riporta il flag",
          rows["carla"]["must_change_password"] is True
          and rows["dario"]["must_change_password"] is False)

    # --- 3. sessione col cambio in sospeso ------------------------------------
    print("\n[3] carla: prima del cambio la sessione vale solo per cambiarla")
    res = login(client, "carla", "benvenuta1").json()
    carla = {"Authorization": "Bearer " + res["token"]}
    check("il login lo dice subito", res["must_change_password"] is True)
    r = client.put("/api/settings/my-terms", json={"custom_terms": []},
                   headers=carla)
    check("un endpoint qualsiasi risponde 403 password_change_required",
          r.status_code == 403
          and r.json().get("code") == "password_change_required", r.text[:120])
    check("/me resta raggiungibile",
          client.get("/api/auth/me", headers=carla).status_code == 200)
    r = client.put("/api/settings/my-language", json={"lang": "it"},
                   headers=carla)
    check("anche la lingua è bloccata (il popup compare dopo, nell'app)",
          r.status_code == 403
          and r.json().get("code") == "password_change_required", r.text[:120])

    # --- 4. il cambio: quello che non deve passare -----------------------------
    print("\n[4] POST /api/auth/password, rifiuti")
    r = change_pw(client, carla, "sbagliata", "nuovanuova1")
    check("vecchia sbagliata: 403 wrong_password (mai 401, o il frontend "
          "farebbe logout)", r.status_code == 403
          and r.json().get("code") == "wrong_password", r.text[:120])
    r = change_pw(client, carla, "benvenuta1", "corta")
    check("nuova sotto gli 8 caratteri: 422", r.status_code == 422,
          f"HTTP {r.status_code}")
    r = change_pw(client, carla, "benvenuta1", "benvenuta1")
    check("nuova uguale alla vecchia: 422 password_same",
          r.status_code == 422 and r.json().get("code") == "password_same",
          r.text[:120])
    check("dopo i rifiuti il flag è ancora alzato",
          client.get("/api/auth/me", headers=carla)
                .json()["must_change_password"] is True)
    r = change_pw(client, {}, "benvenuta1", "nuovanuova1")
    check("senza sessione non si cambia niente", r.status_code == 401,
          f"HTTP {r.status_code}")

    # --- 5. il cambio buono abbassa il flag ------------------------------------
    print("\n[5] il cambio buono")
    r = change_pw(client, carla, "benvenuta1", "lamiapassword2")
    check("risponde ok", r.status_code == 200 and r.json()["ok"] is True,
          r.text[:120])
    check("il flag è caduto",
          client.get("/api/auth/me", headers=carla)
                .json()["must_change_password"] is False)
    check("lo STESSO token ora apre il resto dell'app",
          client.put("/api/settings/my-terms", json={"custom_terms": []},
                     headers=carla).status_code == 200)
    check("e la lingua si salva (è il momento del popup)",
          client.put("/api/settings/my-language", json={"lang": "it"},
                     headers=carla).status_code == 200)
    check("la vecchia password non entra più",
          login(client, "carla", "benvenuta1").status_code == 401)
    res = login(client, "carla", "lamiapassword2").json()
    check("la nuova entra, senza flag",
          res["must_change_password"] is False, repr(res))
    with SessionLocal() as s:
        u = s.query(User).filter_by(username="carla").one()
        check("in colonna c'è l'hash della nuova (pbkdf2, mai in chiaro)",
              u.password_hash.startswith("pbkdf2$")
              and verify_password("lamiapassword2", u.password_hash)
              and not u.must_change_password)

    # --- 6. cambio volontario dalle impostazioni (senza flag) ------------------
    print("\n[6] dario: cambio dalle impostazioni, nessun obbligo")
    res = login(client, "dario", "benvenuto1").json()
    dario = {"Authorization": "Bearer " + res["token"]}
    check("entra senza obblighi", res["must_change_password"] is False)
    r = change_pw(client, dario, "benvenuto1", "sceltadame3")
    check("cambia quando vuole lui", r.status_code == 200, r.text[:120])
    check("e la sessione corrente resta aperta",
          client.get("/api/auth/me", headers=dario).status_code == 200)
    check("al prossimo accesso vale la nuova",
          login(client, "dario", "sceltadame3").status_code == 200
          and login(client, "dario", "benvenuto1").status_code == 401)

    # --- 7. vale anche per un admin --------------------------------------------
    print("\n[7] admin creato col flag: bloccato anche lui")
    client.post("/api/users", headers=admin,
                json={"username": "boss2", "password": "ancheladmin1",
                      "role": "admin", "must_change_password": True})
    res = login(client, "boss2", "ancheladmin1").json()
    boss2 = {"Authorization": "Bearer " + res["token"]}
    r = client.get("/api/users", headers=boss2)
    check("niente pannello utenti prima del cambio", r.status_code == 403
          and r.json().get("code") == "password_change_required", r.text[:120])
    change_pw(client, boss2, "ancheladmin1", "oradavvero4")
    check("dopo il cambio amministra",
          client.get("/api/users", headers=boss2).status_code == 200)

    print(f"\nRISULTATO: {PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

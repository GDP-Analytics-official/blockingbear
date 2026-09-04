"""Modelli visibili agli utenti non admin (white list): app/model_access.py.

Tre cose da provare: la logica pura (la lista + il modello predefinito che
entra da solo), le route (chi legge, chi scrive, cosa vede l'utente nel
catalogo) e i tre punti in cui il server rifiuta un modello proibito
(creazione, cambio, invio), più la regola personale risolta sul sottoinsieme.

Uso:
    python backend/tests/model_access_test.py
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import chat_mock_openrouter as mock                              # noqa: E402

# PRIMA di ogni import di app.*: dati isolati e OpenRouter finto
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_model_access")
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_OPENROUTER_BASE_URL"] = mock.BASE_URL

from fastapi import FastAPI                                      # noqa: E402
from fastapi.responses import JSONResponse                       # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from app import model_access, settings_store                     # noqa: E402
from app.auth import hash_password, seed_admin                   # noqa: E402
from app.db import User, init_db                                 # noqa: E402
from app.openrouter import catalog                               # noqa: E402
from app.routes import auth_routes, chat_routes, settings_routes  # noqa: E402

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


def storage():
    print("== validazione della white list ==")
    ok = settings_store.clean_model_access(True, [" a/b ", "a/b", "c/d:free"])
    check("id ripuliti e senza doppioni, ordine conservato",
          ok == {"enabled": True, "models": ["a/b", "c/d:free"]}, str(ok))
    check("enabled è un booleano",
          settings_store.clean_model_access(1, [])["enabled"] is True)
    for bad in (["a b"], [""], [3], "a/b", ["x" * 129]):
        try:
            settings_store.clean_model_access(True, bad)
            check(f"rifiutato {bad!r}", False)
        except ValueError:
            check(f"rifiutato {bad!r}", True)


def routes():
    print("\n== route, catalogo e blocchi ==")
    db_file = Path(os.environ["BLOCKINGBEAR_DATA_DIR"]) / "blockingbear.db"
    db_file.unlink(missing_ok=True)
    catalog.invalidate()
    stop_mock = mock.serve()

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(chat_routes.router)

    # stesso gestore di main.py: senza, le risposte d'errore non avrebbero
    # `code`, e qui i codici sono metà del contratto
    @app.exception_handler(StarletteHTTPException)
    async def _api_error(request, exc):
        body = {"detail": exc.detail}
        if getattr(exc, "code", None):
            body["code"] = exc.code
        return JSONResponse(status_code=exc.status_code, content=body)

    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)
        s.add(User(username="mario", password_hash=hash_password("mario"),
                   role="standard"))
        s.commit()
    mock.grant_key(SessionLocal, "mario")

    client = TestClient(app)

    def login(u, p):
        r = client.post("/api/auth/login", json={"username": u, "password": p})
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def new_chat(auth, model=""):
        return client.post("/api/chats", json={"model": model}, headers=auth)

    try:
        admin = login("admin", "admin")
        mario = login("mario", "mario")

        # --- senza white list: tutti vedono tutto ---------------------------
        r = client.get("/api/settings/model-access", headers=admin)
        check("stato iniziale: tutti i modelli",
              r.status_code == 200
              and r.json() == {"enabled": False, "models": [], "locked": ""},
              str(r.json()))
        r = client.get("/api/settings/model-access", headers=mario)
        check("la white list la legge solo l'admin", r.status_code == 403)
        r = client.put("/api/settings/model-access", headers=mario,
                       json={"enabled": True, "models": ["test/plain-model"]})
        check("la white list la scrive solo l'admin", r.status_code == 403)
        r = client.get("/api/openrouter/models", headers=mario).json()
        check("catalogo dell'utente: allowed nullo = tutti",
              r["allowed"] is None and len(r["models"]) > 2)

        # una chat aperta PRIMA della restrizione, su un modello che poi
        # sparirà dalla lista
        old = new_chat(mario, "test/happy-model")
        check("prima della restrizione ogni modello va bene",
              old.status_code == 200 and old.json()["model"] == "test/happy-model")
        old_id = old.json()["id"]

        # --- la white list si accende ---------------------------------------
        r = client.put("/api/settings/model-access", headers=admin,
                       json={"enabled": True, "models": ["test/plain-model"]})
        check("l'admin salva la white list",
              r.status_code == 200 and r.json()["enabled"] is True
              and r.json()["models"] == ["test/plain-model"]
              and r.json()["locked"] == "", str(r.json()))
        r = client.put("/api/settings/model-access", headers=admin,
                       json={"enabled": True, "models": ["con spazio"]})
        check("id non valido rifiutato con 422", r.status_code == 422)

        r = client.get("/api/openrouter/models", headers=mario).json()
        check("catalogo dell'utente: intero, ma con gli id consentiti",
              r["allowed"] == ["test/plain-model"] and len(r["models"]) > 2,
              str(r["allowed"]))
        r = client.get("/api/openrouter/models", headers=admin).json()
        check("l'admin non è limitato", r["allowed"] is None)

        r = new_chat(mario, "test/happy-model")
        check("creazione su un modello proibito: 403 model_not_allowed",
              r.status_code == 403 and r.json()["code"] == "model_not_allowed",
              r.text[:80])
        r = new_chat(mario, "test/plain-model")
        check("creazione su un modello consentito", r.status_code == 200)
        conv_id = r.json()["id"]
        r = new_chat(admin, "test/happy-model")
        check("l'admin crea su qualsiasi modello", r.status_code == 200)

        r = client.patch(f"/api/chats/{conv_id}", headers=mario,
                         json={"model": "test/happy-model"})
        check("cambio verso un modello proibito: 403", r.status_code == 403)
        r = client.patch(f"/api/chats/{conv_id}", headers=mario,
                         json={"title": "ok"})
        check("le altre modifiche passano", r.status_code == 200)

        r = client.post(f"/api/chats/{old_id}/messages", headers=mario,
                        json={"content": "ciao"})
        check("invio su una chat già sul modello proibito: 403",
              r.status_code == 403 and r.json()["code"] == "model_not_allowed",
              r.text[:80])
        r = client.post(f"/api/chats/{conv_id}/messages", headers=mario,
                        json={"content": "ciao", "model": "test/happy-model"})
        check("invio cambiando verso un modello proibito: 403",
              r.status_code == 403)

        # --- il modello predefinito entra da solo ---------------------------
        r = client.put("/api/settings/default-model", headers=admin,
                       json={"rule": {"kind": "fixed", "model": "test/happy-model"}})
        check("l'admin fissa il default su un modello fuori lista",
              r.status_code == 200)
        r = client.get("/api/settings/model-access", headers=admin).json()
        check("locked = il default fisso", r["locked"] == "test/happy-model",
              str(r))
        r = client.get("/api/openrouter/models", headers=mario).json()
        check("il default entra fra i consentiti dell'utente",
              r["allowed"] == ["test/happy-model", "test/plain-model"],
              str(r["allowed"]))
        r = new_chat(mario, "test/happy-model")
        check("ora il default si può scegliere", r.status_code == 200)
        r = new_chat(mario)
        check("la chat senza modello nasce sul default",
              r.json()["model"] == "test/happy-model")
        r = client.post(f"/api/chats/{old_id}/messages", headers=mario,
                        json={"content": "ciao", "model": "test/error-model"})
        check("un altro modello resta proibito", r.status_code == 403)

        # la regola guidata: il locked cambia col catalogo (qui: con la deroga
        # ZDR spenta la guidata «coding» sceglie happy-model, non web-model)
        r = client.put("/api/settings/default-model", headers=admin,
                       json={"rule": {"kind": "guided", "use": "coding",
                                      "budget": "best", "images": False}})
        r = client.get("/api/settings/model-access", headers=admin).json()
        check("locked = il risolto della guidata",
              r["locked"] == "test/happy-model", str(r["locked"]))
        r = client.get("/api/openrouter/models", headers=mario).json()
        check("il risolto della guidata è consentito",
              "test/happy-model" in r["allowed"], str(r["allowed"]))

        # --- la regola personale vive nel sottoinsieme ----------------------
        r = client.put("/api/settings/my-default-model", headers=mario,
                       json={"rule": {"kind": "fixed", "model": "test/error-model"}})
        check("regola personale fissa su un modello proibito: 403",
              r.status_code == 403 and r.json()["code"] == "model_not_allowed")
        r = client.put("/api/settings/my-default-model", headers=mario,
                       json={"rule": {"kind": "fixed", "model": "test/plain-model"}})
        check("regola personale fissa su un modello consentito",
              r.status_code == 200 and r.json()["resolved"] == "test/plain-model",
              str(r.json()))
        r = new_chat(mario)
        check("la chat nasce sul modello personale", r.json()["model"] == "test/plain-model")
        r = client.put("/api/settings/default-model", headers=admin,
                       json={"rule": None})
        r = client.put("/api/settings/my-default-model", headers=mario,
                       json={"rule": {"kind": "guided", "use": "coding",
                                      "budget": "best", "images": False}})
        check("regola personale guidata: si risolve solo fra i consentiti",
              r.status_code == 200 and r.json()["resolved"] == "",
              "plain-model non ha strumenti/punteggi: nessun default")
        r = client.post("/api/settings/default-model/resolve", headers=mario,
                        json={"rule": {"kind": "guided", "use": "coding",
                                       "budget": "best", "images": False}})
        check("anteprima dell'utente: stesso sottoinsieme",
              r.json()["model"] == "" and r.json()["detail"] == {"pool": 0},
              str(r.json()))
        r = client.post("/api/settings/default-model/resolve", headers=admin,
                        json={"rule": {"kind": "guided", "use": "coding",
                                       "budget": "best", "images": False}})
        check("anteprima dell'admin: catalogo intero",
              r.json()["model"] == "test/happy-model", str(r.json()))
        r = client.put("/api/settings/my-default-model", headers=mario,
                       json={"rule": None})

        # --- logica pura: catalogo irraggiungibile --------------------------
        with SessionLocal() as s:
            settings_store.set_default_model_rule(
                s, {"kind": "fixed", "model": "x/fisso"})
            check("senza catalogo il default FISSO entra comunque",
                  model_access.allowed_ids(s, None) == {"test/plain-model", "x/fisso"})
            settings_store.set_default_model_rule(
                s, {"kind": "guided", "use": "coding", "budget": "best",
                    "images": False})
            check("senza catalogo la guidata non si risolve: resta la lista",
                  model_access.allowed_ids(s, None) == {"test/plain-model"})
            settings_store.set_default_model_rule(s, None)

        # --- si spegne -------------------------------------------------------
        r = client.put("/api/settings/model-access", headers=admin,
                       json={"enabled": False, "models": ["test/plain-model"]})
        check("spenta: l'elenco resta salvato",
              r.json()["enabled"] is False and r.json()["models"] == ["test/plain-model"])
        r = client.get("/api/openrouter/models", headers=mario).json()
        check("spenta: di nuovo tutti", r["allowed"] is None)
        r = new_chat(mario, "test/error-model")
        check("spenta: nessun blocco", r.status_code == 200)
    finally:
        stop_mock()


if __name__ == "__main__":
    storage()
    routes()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)

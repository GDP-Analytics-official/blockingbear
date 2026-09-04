"""Wizard di installazione (routes/setup_routes.py) sul finto OpenRouter:
stato iniziale, rifiuto di una chiave di inferenza al posto della management
key, creazione dell'admin con la sua chiave, lingua sul profilo, blocco delle
route pubbliche a installazione conclusa, sostituzione della management key
da admin. Niente Docker e niente costi.

Uso:
    python backend/tests/setup_test.py
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import chat_mock_openrouter as mock                              # noqa: E402

os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_setup")
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_OPENROUTER_BASE_URL"] = mock.BASE_URL

from fastapi import FastAPI                                      # noqa: E402
from fastapi.responses import JSONResponse                       # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from app.db import User, init_db                                 # noqa: E402
from app.openrouter import client as or_client                   # noqa: E402
from app.routes import (auth_routes, chat_routes, settings_routes,  # noqa: E402
                        setup_routes)

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


def main():
    db_file = Path(os.environ["BLOCKINGBEAR_DATA_DIR"]) / "blockingbear.db"
    db_file.unlink(missing_ok=True)
    or_client.delete_management_key()
    stop_mock = mock.serve()
    mock.KEYS.clear()

    app = FastAPI()
    app.include_router(setup_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(chat_routes.router)

    # stesso gestore di main.py: senza, le risposte d'errore non avrebbero
    # `code` e qui i codici sono metà del contratto
    @app.exception_handler(StarletteHTTPException)
    async def _api_error(request, exc):
        body = {"detail": exc.detail}
        if getattr(exc, "code", None):
            body["code"] = exc.code
        return JSONResponse(status_code=exc.status_code, content=body)

    SessionLocal = init_db()
    client = TestClient(app)

    try:
        st = client.get("/api/setup/status").json()
        check("configurazione da fare, senza management key",
              st == {"needed": True, "management_key": False, "admin": False},
              str(st))

        r = client.post("/api/setup/admin", json={"password": "password8"})
        check("admin prima della management key: 409",
              r.status_code == 409 and r.json()["code"] == "setup_key_missing",
              r.text[:80])

        r = client.post("/api/setup/management-key",
                        json={"key": "sk-or-test-123456789"})
        check("chiave di inferenza rifiutata come management key",
              r.status_code == 422
              and r.json()["code"] == "key_not_management"
              and or_client.get_management_key() is None, r.text[:100])

        r = client.post("/api/setup/management-key",
                        json={"key": "chiave-sbagliata"})
        check("chiave sconosciuta rifiutata", r.status_code == 422
              and r.json()["code"] == "key_refused", r.text[:100])

        r = client.post("/api/setup/management-key",
                        json={"key": mock.MGMT_KEY})
        check("management key validata e salvata", r.status_code == 200
              and or_client.get_management_key() == mock.MGMT_KEY
              and mock.MGMT_KEY not in r.json()["management_masked"],
              r.text[:100])
        st = client.get("/api/setup/status").json()
        check("stato: chiave presente, admin ancora da creare",
              st == {"needed": True, "management_key": True, "admin": False},
              str(st))

        r = client.post("/api/setup/admin", json={"password": "corta"})
        check("password sotto gli 8 caratteri: 422", r.status_code == 422)

        r = client.post("/api/setup/admin",
                        json={"password": "password8", "lang": "en"})
        body = r.json()
        check("admin creato, senza token", r.status_code == 201
              and body["username"] == "admin" and body["role"] == "admin"
              and body["lang"] == "en" and "token" not in body, r.text[:120])
        with SessionLocal() as s:
            admin = s.query(User).filter_by(username="admin").one()
            check("chiave di inferenza dell'admin creata dalla management key",
                  (admin.openrouter_key or "").startswith("sk-or-test-")
                  and admin.openrouter_key_hash in mock.KEYS
                  and mock.KEYS[admin.openrouter_key_hash]["name"]
                  == "blockingbear-admin")
            check("lingua salvata sul profilo", admin.lang == "en")

        # F5 a metà wizard: la configurazione è ancora da finire, l'admin no
        st = client.get("/api/setup/status").json()
        check("stato: admin creato, wizard ancora aperto",
              st == {"needed": True, "management_key": True, "admin": True},
              str(st))
        r = client.post("/api/setup/admin", json={"password": "password8"})
        check("secondo admin rifiutato", r.status_code == 409
              and r.json()["code"] == "admin_exists")
        with SessionLocal() as s:
            old_hash = s.query(User).filter_by(username="admin").one().openrouter_key_hash
        r = client.post("/api/setup/admin",
                        json={"password": "password8", "replace": True})
        with SessionLocal() as s:
            admins = s.query(User).filter_by(username="admin").all()
        check("ricrea admin: uno solo, chiave vecchia revocata e nuova creata",
              r.status_code == 201 and len(admins) == 1
              and old_hash not in mock.KEYS
              and admins[0].openrouter_key_hash in mock.KEYS
              and len(mock.KEYS) == 1, r.text[:100])

        r = client.get("/api/setup/models")
        check("catalogo durante il wizard, senza login", r.status_code == 200
              and any(m["id"] == "test/happy-model" for m in r.json()["models"]),
              r.text[:80])

        r = client.post("/api/setup/finish", json={
            "excluded_tags": ["AGE", "DATE"],
            "custom_terms": [{"text": "Acme S.p.A.", "tag": "CUSTOM_ORG"}],
            "chat_anonymization_policy": "required",
            "chat_allow_non_zdr": True,
            "default_model_rule": {
                "kind": "fixed", "model": "test/happy-model",
                "options": {"reasoning": {"enabled": True, "effort": "high"},
                            "params": {"temperature": 0.3},
                            "web_search": False}},
            "model_access": {"enabled": True,
                             "models": ["test/plain-model", "test/plain-model"]}})
        check("finish: scelte salvate", r.status_code == 200, r.text[:100])
        st = client.get("/api/setup/status").json()
        check("configurazione conclusa", st["needed"] is False)

        r = client.post("/api/auth/login", json={
            "username": "admin", "password": "password8"})
        check("login con la password scelta", r.status_code == 200)
        auth = {"Authorization": f"Bearer {r.json()['token']}"}
        r = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin"})
        check("nessuna password predefinita", r.status_code == 401)

        acc = client.get("/api/settings/model-access", headers=auth).json()
        check("finish: white list salvata, col default come locked",
              acc == {"enabled": True, "models": ["test/plain-model"],
                      "locked": "test/happy-model"}, str(acc))
        d = client.get("/api/settings/anonymization", headers=auth).json()
        st2 = client.get("/api/settings", headers=auth).json()
        zdr = next(x for x in st2 if x["key"] == "chat_allow_non_zdr")
        check("finish: categorie, termini, policy e ZDR applicati",
              d["excluded_tags"] == ["AGE", "DATE"]
              and d["custom_terms"][0]["tag"] == "CUSTOM_ORG"
              and d["chat_anonymization_policy"] == "required"
              and zdr["value"] == 1, str(d)[:120])

        dm = client.get("/api/settings/default-model", headers=auth).json()
        check("finish: modello predefinito con opzioni (senza web_search)",
              dm["rule"] == {"kind": "fixed", "model": "test/happy-model",
                             "options": {"reasoning": {"enabled": True,
                                                       "effort": "high"},
                                         "params": {"temperature": 0.3}}},
              str(dm["rule"]))
        conv = client.post("/api/chats", headers=auth, json={}).json()
        check("nuova chat: modello e opzioni della regola",
              conv["model"] == "test/happy-model"
              and conv["options"] == {"reasoning": {"enabled": True,
                                                    "effort": "high"},
                                      "params": {"temperature": 0.3}},
              str(conv.get("options")))
        r = client.get("/api/setup/models")
        check("route pubbliche chiuse: catalogo", r.status_code == 409)

        r = client.post("/api/setup/admin", json={"password": "password8"})
        check("route pubbliche chiuse: admin", r.status_code == 409
              and r.json()["code"] == "setup_done")
        r = client.post("/api/setup/management-key",
                        json={"key": mock.MGMT_KEY})
        check("route pubbliche chiuse: management key", r.status_code == 409)
        r = client.post("/api/setup/finish", json={
            "excluded_tags": [], "custom_terms": []})
        check("route pubbliche chiuse: finish", r.status_code == 409)

        st = client.get("/api/openrouter/status", headers=auth).json()
        check("status admin: management key mascherata",
              st["configured"] and st["provisioning"]
              and st["management_masked"]
              and mock.MGMT_KEY not in st["management_masked"], str(st)[:160])

        # sostituzione da admin: solo una management key valida
        r = client.put("/api/settings/management-key", headers=auth,
                       json={"key": "sk-or-test-123456789"})
        check("sostituzione con chiave di inferenza rifiutata",
              r.status_code == 422
              and or_client.get_management_key() == mock.MGMT_KEY)
        r = client.put("/api/settings/management-key", headers=auth,
                       json={"key": mock.MGMT_KEY})
        check("sostituzione accettata", r.status_code == 200)
        r = client.put("/api/settings/management-key",
                       json={"key": mock.MGMT_KEY})
        check("sostituzione senza login: 401", r.status_code == 401)

        # admin non creato se OpenRouter rifiuta la chiave (wizard riaperto)
        from app.db import Setting
        with SessionLocal() as s:
            s.delete(s.query(User).filter_by(username="admin").one())
            s.delete(s.get(Setting, "setup_completed"))
            s.commit()
        or_client.set_management_key("mgmt-sbagliata")
        r = client.post("/api/setup/admin", json={"password": "password8"})
        with SessionLocal() as s:
            n = s.query(User).count()
        check("OpenRouter rifiuta la chiave: admin non creato",
              r.status_code == 502 and n == 0, f"{r.status_code} utenti={n}")
    finally:
        stop_mock()

    print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

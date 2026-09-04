"""Chiavi OpenRouter per-utente da un capo all'altro, sul finto OpenRouter:
bootstrap dell'admin, creazione utente con limite, rotazione, PATCH dei
limiti, revoca alla cancellazione, dashboard (/api/usage/*) e scelta della
chiave in send_message. Niente Docker e niente costi.

Uso:
    python backend/tests/userkeys_test.py
"""
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import chat_mock_openrouter as mock                              # noqa: E402

# PRIMA di ogni import di app.*: dati isolati e OpenRouter finto
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_userkeys")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_OPENROUTER_BASE_URL"] = mock.BASE_URL

from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app.auth import seed_admin                                  # noqa: E402
from app.db import User, init_db                                 # noqa: E402
from app.openrouter import client as or_client                   # noqa: E402
from app.openrouter import provisioning                          # noqa: E402
from app.routes import auth_routes, chat_routes, usage_routes, users  # noqa: E402

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
    # stato pulito tra un run e l'altro (DB del run precedente); la management
    # key la mette il wizard: qui si scrive direttamente il file
    db_file = Path(os.environ["BLOCKINGBEAR_DATA_DIR"]) / "blockingbear.db"
    db_file.unlink(missing_ok=True)
    or_client.set_management_key(mock.MGMT_KEY)
    stop_mock = mock.serve()
    mock.KEYS.clear()

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(users.router)
    app.include_router(chat_routes.router)
    app.include_router(usage_routes.router)
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)

    client = TestClient(app)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    try:
        # --- bootstrap: l'admin seminato riceve la sua chiave --------------
        asyncio.run(provisioning.bootstrap(SessionLocal))
        with SessionLocal() as s:
            admin = s.query(User).filter_by(username="admin").one()
            check("bootstrap: chiave dell'admin creata",
                  (admin.openrouter_key or "").startswith("sk-or-test-")
                  and admin.openrouter_key_hash in mock.KEYS,
                  admin.openrouter_key_hash)
            check("bootstrap: nome col pattern blockingbear-<username>",
                  mock.KEYS[admin.openrouter_key_hash]["name"]
                  == "blockingbear-admin")
        # ...e un secondo bootstrap non ne crea altre
        asyncio.run(provisioning.bootstrap(SessionLocal))
        check("bootstrap idempotente", len(mock.KEYS) == 1, len(mock.KEYS))

        # --- creazione utente con tetto di spesa ---------------------------
        r = client.post("/api/users", headers=auth, json={
            "username": "carla", "password": "password8", "role": "standard",
            "key_limit": 15.5, "key_limit_reset": "monthly"})
        check("utente creato con chiave", r.status_code == 201
              and r.json()["has_key"] and not r.json().get("key_error"),
              r.text[:120])
        carla_hash = r.json()["key_hash"]
        check("limite e cadenza passati a OpenRouter",
              mock.KEYS[carla_hash]["limit"] == 15.5
              and mock.KEYS[carla_hash]["limit_reset"] == "monthly")

        rows = client.get("/api/users", headers=auth).json()
        check("lista utenti con stato chiave",
              all(u["has_key"] for u in rows) and len(rows) == 2)

        # --- PATCH del limite ----------------------------------------------
        carla_id = next(u["id"] for u in rows if u["username"] == "carla")
        r = client.patch(f"/api/users/{carla_id}/key", headers=auth,
                         json={"limit": 30, "limit_reset": "weekly"})
        check("patch limite", r.status_code == 200
              and mock.KEYS[carla_hash]["limit"] == 30
              and mock.KEYS[carla_hash]["limit_reset"] == "weekly", r.text[:120])
        r = client.patch(f"/api/users/{carla_id}/key", headers=auth,
                         json={"clear_limit": True})
        check("rimozione limite", r.status_code == 200
              and mock.KEYS[carla_hash]["limit"] is None)
        # niente disable via API: per bloccare si elimina l'utente
        r = client.patch(f"/api/users/{carla_id}/key", headers=auth,
                         json={"disabled": True})
        check("disable non esiste più (nessuna modifica -> 422)",
              r.status_code == 422, r.text[:80])

        # --- rotazione: revoca la vecchia, ne salva una nuova ---------------
        r = client.post(f"/api/users/{carla_id}/key", headers=auth, json={})
        new_hash = r.json()["key_hash"]
        check("rotazione", r.status_code == 200 and new_hash != carla_hash
              and carla_hash not in mock.KEYS and new_hash in mock.KEYS,
              f"{carla_hash} -> {new_hash}")

        # --- status: chiave personale, saldo account per l'admin ------------
        st = client.get("/api/openrouter/status", headers=auth).json()
        check("status: personale + saldo globale",
              st["configured"] and st["personal_key"] and st["provisioning"]
              and st["credits"].get("total_credits") == 120.0
              and st["management_masked"]
              and mock.MGMT_KEY not in st["management_masked"], str(st)[:160])

        # --- il turno usa la chiave DELL'utente ------------------------------
        conv = client.post("/api/chats", headers=auth,
                           json={"model": "test/plain-model"}).json()
        with client.stream("POST", f"/api/chats/{conv['id']}/messages",
                           headers=auth, json={"content": "ciao"}) as resp:
            resp.read()
        sent = [r for r in __import__("httpx").get(mock.DEBUG_URL).json()
                if "messages" in r]
        with SessionLocal() as s:
            admin_key = s.query(User).filter_by(username="admin").one().openrouter_key
        # il finto server non registra l'header: verifichiamo la scelta a monte
        with SessionLocal() as s:
            u = s.query(User).filter_by(username="admin").one()
            check("send_message: chiave personale scelta",
                  provisioning.key_for(u) == admin_key and sent,
                  f"{len(sent)} richieste inviate")

        # --- dashboard -------------------------------------------------------
        # una chiave di un ALTRO progetto sullo stesso account: non deve
        # comparire da nessuna parte
        mock.KEYS["hash-alien"] = {
            "hash": "hash-alien", "name": "altro-progetto", "disabled": False,
            "limit": 5, "limit_reset": None, "limit_remaining": 5,
            "usage": 99.0, "usage_daily": 1.0, "usage_weekly": 2.0,
            "usage_monthly": 3.0, "created_at": "2026-01-01T00:00:00Z"}

        ov = client.get("/api/usage/overview", headers=auth).json()
        check("overview: saldo + SOLO chiavi dell'app",
              ov["credits"]["total_usage"] == 34.5
              and len(ov["keys"]) == 2
              and all(k["user"] for k in ov["keys"])
              and all(k["name"].startswith("blockingbear-")
                      for k in ov["keys"])
              and ov["users_without_key"] == [], str(ov)[:160])

        r = client.post("/api/usage/query", headers=auth, json={
            "metrics": ["total_usage", "request_count", "tokens_total"],
            "dimensions": ["api_key_id"], "granularity": "day", "days": 30})
        rows = r.json()["data"]
        check("analytics: serie per giorno e chiave",
              r.status_code == 200 and rows
              and all("date__day" in x and "api_key_id" in x for x in rows),
              f"{len(rows)} righe")
        sent_q = [r for r in __import__("httpx").get(mock.DEBUG_URL).json()
                  if "analytics" in r][-1]["analytics"]
        check("analytics: time_range costruito dal server",
              "time_range" in sent_q
              and sent_q["time_range"]["start"].endswith("T00:00:00Z"))
        with SessionLocal() as s:
            db_hashes = {u.openrouter_key_hash for u in s.query(User)
                         if u.openrouter_key_hash}
        flt = [f for f in sent_q.get("filters", [])
               if f["field"] == "api_key_id"]
        check("analytics: filtro hash dell'app SEMPRE iniettato",
              flt and flt[0]["operator"] == "in"
              and set(flt[0]["value"]) == db_hashes
              and "hash-alien" not in flt[0]["value"], str(flt)[:120])
        # ...anche se il client prova a filtrare su un'altra chiave
        r = client.post("/api/usage/query", headers=auth, json={
            "metrics": ["total_usage"], "days": 7,
            "filters": [{"field": "api_key_id", "operator": "eq",
                         "value": "hash-alien"}]})
        sent_q = [r for r in __import__("httpx").get(mock.DEBUG_URL).json()
                  if "analytics" in r][-1]["analytics"]
        values = [f["value"] for f in sent_q["filters"]
                  if f["field"] == "api_key_id"]
        check("analytics: filtro del client su api_key_id sostituito",
              r.status_code == 200 and len(values) == 1
              and set(values[0]) == db_hashes
              and "hash-alien" not in values[0], str(values)[:120])
        del mock.KEYS["hash-alien"]

        r = client.post("/api/usage/query", headers=auth, json={
            "metrics": ["total_usage"], "dimensions": ["model"], "days": 7})
        check("analytics: spaccato per modello", r.status_code == 200
              and r.json()["data"][0]["model"] == "test/happy-model")

        meta = client.get("/api/usage/meta", headers=auth).json()
        check("analytics: meta", "metrics" in meta, str(meta)[:80])

        # gli utenti standard non vedono la dashboard
        tok2 = client.post("/api/auth/login", json={
            "username": "carla", "password": "password8"}).json()["token"]
        r = client.get("/api/usage/overview",
                       headers={"Authorization": f"Bearer {tok2}"})
        check("dashboard riservata all'admin", r.status_code == 403)

        # --- cancellazione utente = revoca della chiave ----------------------
        r = client.delete(f"/api/users/{carla_id}", headers=auth)
        check("delete utente revoca la chiave", r.status_code == 200
              and new_hash not in mock.KEYS and len(mock.KEYS) == 1,
              r.text[:80])
    finally:
        stop_mock()

    print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

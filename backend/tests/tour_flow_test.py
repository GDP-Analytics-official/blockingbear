"""Tutorial del primo accesso: il flag `tour_done` sul profilo.

Copre: il default falso per un profilo nuovo (in /login e /me), PUT
/api/settings/my-tour con done true/false, il rifiuto senza sessione e
l'isolamento fra due profili.

Uso:  python backend/tests/tour_flow_test.py
"""
import io
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DATA_DIR = HERE / "data" / "test_tour_flow"
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA_DIR)
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

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

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(settings_routes.router)
    client = TestClient(app)

    print("\n[1] profilo nuovo: tour_done falso")
    res = login(client, "mario", "segreto1").json()
    tok_m = {"Authorization": "Bearer " + res["token"]}
    check("/login dice tour_done = false", res.get("tour_done") is False, repr(res.get("tour_done")))
    me = client.get("/api/auth/me", headers=tok_m).json()
    check("/me dice tour_done = false", me.get("tour_done") is False, repr(me.get("tour_done")))

    print("\n[2] PUT /api/settings/my-tour")
    r = client.put("/api/settings/my-tour", json={"done": True}, headers=tok_m)
    check("done=true risponde 200", r.status_code == 200 and r.json()["tour_done"] is True, r.text[:120])
    check("/me lo ritorna subito", client.get("/api/auth/me", headers=tok_m).json()["tour_done"] is True)
    check("al prossimo accesso arriva col login",
          login(client, "mario", "segreto1").json()["tour_done"] is True)
    r = client.put("/api/settings/my-tour", json={}, headers=tok_m)
    check("body vuoto vale done=true", r.status_code == 200 and r.json()["tour_done"] is True, r.text[:120])
    r = client.put("/api/settings/my-tour", json={"done": False}, headers=tok_m)
    check("done=false lo fa ricomparire", r.status_code == 200 and r.json()["tour_done"] is False, r.text[:120])
    check("/me di nuovo false", client.get("/api/auth/me", headers=tok_m).json()["tour_done"] is False)

    print("\n[3] senza sessione e isolamento fra profili")
    r = client.put("/api/settings/my-tour", json={"done": True})
    check("senza sessione non si salva niente", r.status_code in (401, 403), f"HTTP {r.status_code}")
    client.put("/api/settings/my-tour", json={"done": True}, headers=tok_m)
    res_a = login(client, "anna", "segreto1").json()
    check("anna entra ancora col tutorial da fare", res_a["tour_done"] is False, repr(res_a["tour_done"]))

    print(f"\n{PASS} passati, {FAIL} falliti")
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

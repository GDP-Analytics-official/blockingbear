"""Aree SIGILLATE nell'anteprima pre-invio della chat (chat_staging.seal_area/
remove_seal + endpoint staged + applicazione in anonymize_turn): app FastAPI
ridotta (auth + chat), finto OpenRouter, modello PII vero, allegato PDF.

Copre: sigillo dall'anteprima (contenuto rimosso dalla copia protetta),
replica su tutte le pagine, box solo sul lato anonimizzato, rimozione (il
contenuto torna), persistenza sull'allegato attraverso l'INVIO DIRETTO senza
anteprima (anonymize_turn riapplica i sigilli), guardie (item non sigillabile,
pagina fuori range, sigillo inesistente).

Uso:
    python backend/tests/seal_chat_test.py
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import chat_mock_openrouter as mock                              # noqa: E402

os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_seal_chat")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_OPENROUTER_BASE_URL"] = mock.BASE_URL

import fitz                                                      # noqa: E402
import httpx                                                     # noqa: E402
from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import settings_store                                  # noqa: E402
from app.auth import seed_admin                                  # noqa: E402
from app.db import init_db                                       # noqa: E402
from app.engine.pdf import extract_text                          # noqa: E402
from app.openrouter import client as or_client                   # noqa: E402
from app.routes import auth_routes, chat_routes                  # noqa: E402

PASS = 0
FAIL = 0

SECRET = "SEGRETO-XYZ-123"
HEADER = "Intestazione riservata"
SECRET_RECT = [55.0, 186.0, 330.0, 210.0]
HEADER_RECT = [55.0, 58.0, 330.0, 82.0]
PROMPT = "Riassumi il contratto allegato per Mario Rossi."


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def make_pdf():
    doc = fitz.open()
    for i in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), HEADER, fontsize=12)
        page.insert_text((72, 140), "Cliente: Mario Rossi "
                                    f"(mario.rossi@example.com, pagina {i + 1})",
                         fontsize=12)
        if i == 0:
            page.insert_text((72, 200), SECRET, fontsize=12)
    out = doc.tobytes()
    doc.close()
    return out


def sse_events(response):
    events = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def of_type(events, t):
    return [e for e in events if e["type"] == t]


def main():
    stop_mock = mock.serve()

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(chat_routes.router)
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)
        settings_store.set_anon_defaults(s, [], [])

    client = TestClient(app)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    mock.grant_key(SessionLocal)

    conv = client.post("/api/chats", json={"anonymized": True},
                       headers=auth).json()
    cid = conv["id"]
    client.patch(f"/api/chats/{cid}", json={"model": "test/plain-model"},
                 headers=auth)
    att = client.post(
        f"/api/chats/{cid}/attachments",
        files={"file": ("contratto.pdf", make_pdf(), "application/pdf")},
        headers=auth).json()
    aid = att["id"]

    # --- anteprima ------------------------------------------------------------
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": PROMPT, "preview": True},
                       headers=auth) as resp:
        events = sse_events(resp)
    staged = of_type(events, "staged")[0]
    file_item = next(i for i in staged["items"] if i["kind"] == "attachment")
    check("item allegato con sealed vuoto", file_item.get("sealed") == [])

    def protected_text():
        r = client.get(f"/api/chats/{cid}/attachments/{aid}/anonymized",
                       headers=auth)
        return extract_text(r.content, allow_empty=True)[0]

    t = protected_text()
    check("prima del sigillo il segreto è nella copia protetta", SECRET in t)

    # --- sigillo singolo -------------------------------------------------------
    r = client.post(f"/api/chats/{cid}/staged/{aid}/seal-area", headers=auth,
                    json={"page": 0, "rect": SECRET_RECT})
    check("POST staged seal-area 200", r.status_code == 200, r.text[:80])
    staged = r.json()
    check("sealed_added e rev incrementata",
          staged.get("sealed_added") == 1 and staged["rev"] == 1)
    file_item = next(i for i in staged["items"] if i["kind"] == "attachment")
    check("item con un'area sigillata", len(file_item["sealed"]) == 1)
    sealed_boxes = [b for bl in file_item["anonymized_boxes"].values()
                    for b in bl if b.get("sealed")]
    check("box sealed sul lato anonimizzato",
          len(sealed_boxes) == 1 and sealed_boxes[0]["label"] == "SEALED")
    check("nessun box sealed sul lato originale",
          not any(b.get("sealed") for bl in file_item["original_boxes"].values()
                  for b in bl))
    t = protected_text()
    check("segreto RIMOSSO dalla copia protetta", SECRET not in t)
    check("etichetta SEALED nella copia protetta", "SEALED" in t)
    check("la redazione della mappa resta", "Mario Rossi" not in t)

    # --- replica su tutte le pagine --------------------------------------------
    r = client.post(f"/api/chats/{cid}/staged/{aid}/seal-area", headers=auth,
                    json={"page": 1, "rect": HEADER_RECT, "all_pages": True})
    check("secondo sigillo (tutte le pagine)", r.status_code == 200
          and r.json().get("sealed_added") == 2)
    t = protected_text()
    check("intestazione rimossa su TUTTE le pagine", HEADER not in t)

    # --- rimozione --------------------------------------------------------------
    r = client.delete(f"/api/chats/{cid}/staged/{aid}/seal-area/1",
                      headers=auth)
    check("DELETE sigillo 1", r.status_code == 200
          and r.json().get("sealed_removed") == 1)
    t = protected_text()
    check("il segreto TORNA dopo la rimozione", SECRET in t)
    check("l'altro sigillo resta", HEADER not in t)

    # --- guardie ----------------------------------------------------------------
    r = client.post(f"/api/chats/{cid}/staged/prompt/seal-area", headers=auth,
                    json={"page": 0, "rect": SECRET_RECT})
    check("sigillo sul messaggio -> 422", r.status_code == 422)
    r = client.post(f"/api/chats/{cid}/staged/{aid}/seal-area", headers=auth,
                    json={"page": 9, "rect": SECRET_RECT})
    check("pagina fuori range -> 422", r.status_code == 422)
    r = client.delete(f"/api/chats/{cid}/staged/{aid}/seal-area/99",
                      headers=auth)
    check("sigillo inesistente -> 404", r.status_code == 404)

    # --- invio DIRETTO senza anteprima: i sigilli persistono ---------------------
    client.delete(f"/api/chats/{cid}/staged", headers=auth)
    n_reqs = len(httpx.get(mock.DEBUG_URL).json())
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": PROMPT}, headers=auth) as resp:
        events = sse_events(resp)
    check("invio diretto completato", bool(of_type(events, "done")),
          json.dumps([e["type"] for e in events])[:120])
    check("richiesta partita al finto OpenRouter",
          len(httpx.get(mock.DEBUG_URL).json()) == n_reqs + 1)
    t = protected_text()
    check("sigillo riapplicato dall'invio diretto (anonymize_turn)",
          HEADER not in t and "SEALED" in t)
    check("il sigillo rimosso resta rimosso", SECRET in t)

    stop_mock()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

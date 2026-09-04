"""Turno sganciato dalla connessione + riaggancio (chat_routes: _pump_turn /
_follow_turn / GET /messages/live) da un capo all'altro, con app FastAPI
ridotta (auth + chat), finto OpenRouter e modello PII vero per lo scenario
anonimizzato. Niente Docker: si usa test/plain-model (senza tool).

Copre: il refresh (chiusura della richiesta) NON uccide più il turno, che
completa e persiste da solo; il riaggancio rigioca gli eventi dall'inizio
(evento `turn` col testo del messaggio compreso) sia a turno in corso sia a
turno appena concluso; /stop ferma un turno senza nessun lettore; /live 404
dove non c'è niente da rigiocare; il refresh durante l'ANONIMIZZAZIONE non
perde più il messaggio (prima il messaggio si persisteva solo dopo).

Il TestClient va usato col context manager (`with TestClient(app)`): è il
modo che tiene UN portal (un event loop) per tutta la durata, come uvicorn.
Senza, ogni richiesta avrebbe il suo loop e i task in background morirebbero
con la richiesta che li ha creati — che è esattamente il comportamento che
questa versione toglie di mezzo.

Uso:
    python backend/tests/chat_live_stream_test.py
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import chat_mock_openrouter as mock                              # noqa: E402

os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_live")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_OPENROUTER_BASE_URL"] = mock.BASE_URL

import httpx                                                     # noqa: E402
from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app.auth import seed_admin                                  # noqa: E402
from app.db import init_db                                       # noqa: E402
from app.openrouter import client as or_client                   # noqa: E402
from app.routes import auth_routes, chat_routes                  # noqa: E402

PASS = 0
FAIL = 0

# "LENTO" nel testo fa streammare il finto modello a rate umano (~3 s):
# è la finestra in cui staccarsi e riagganciarsi
SLOW = "Dammi una risposta LENTO per favore."
FULL_SLOW = "pezzo0 pezzo1 pezzo2 pezzo3 pezzo4 pezzo5 fine."


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def sse_events(response):
    events = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def of_type(events, t):
    return [e for e in events if e["type"] == t]


def read_until(resp, types):
    """Legge lo stream fino al primo evento di uno dei tipi dati (compreso):
    chiudere subito dopo simula il refresh del browser a quel punto."""
    got = []
    for line in resp.iter_lines():
        if not line.startswith("data: "):
            continue
        ev = json.loads(line[6:])
        got.append(ev)
        if ev["type"] in types:
            break
    return got


def open_stream(client, cid, auth, body):
    """Manda il messaggio e consuma lo stream in un THREAD: il TestClient
    consegna il corpo SSE solo alla fine del turno, quindi le verifiche
    "mentre il turno è in corso" (busy, /live, /stop) si fanno dal thread
    principale — è anche la prova che il turno non dipende dal lettore."""
    out = {"events": [], "done": False, "error": None}

    def run():
        try:
            with client.stream("POST", f"/api/chats/{cid}/messages",
                               json=body, headers=auth) as resp:
                out["events"] = sse_events(resp)
        except Exception as e:                              # noqa: BLE001
            out["error"] = repr(e)
        finally:
            out["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return out


def wait_stream(out, timeout=15):
    """Aspetta che il thread dello stream abbia consegnato gli eventi."""
    deadline = time.time() + timeout
    while not out["done"] and time.time() < deadline:
        time.sleep(0.1)
    return out["done"]


def wait_busy(client, auth, cid, want, timeout=30):
    """Aspetta che getChat dica busy=want e ritorna l'ultimo stato."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = client.get(f"/api/chats/{cid}", headers=auth).json()
        if c["busy"] is want:
            return c
        time.sleep(0.1)
    return client.get(f"/api/chats/{cid}", headers=auth).json()


def user_text(request):
    msg = [m for m in request["messages"] if m["role"] == "user"][-1]
    if isinstance(msg["content"], str):
        return msg["content"]
    return "".join(p.get("text", "") for p in msg["content"]
                   if p["type"] == "text")


def main():
    stop_mock = mock.serve()

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(chat_routes.router)
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)

    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin"}).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        mock.grant_key(SessionLocal)

        cid = client.post("/api/chats", json={}, headers=auth).json()["id"]
        client.patch(f"/api/chats/{cid}", json={"model": "test/plain-model"},
                     headers=auth)

        # --- A. il turno vive da solo, la connessione è solo un lettore ----
        stream = open_stream(client, cid, auth, {"content": SLOW})
        c = wait_busy(client, auth, cid, True, 10)
        check("turno in corso: getChat dice busy", c["busy"] is True)
        c = wait_busy(client, auth, cid, False)
        roles = [m["role"] for m in c["messages"]]
        check("turno completato e persistito",
              c["busy"] is False and roles == ["user", "assistant"],
              str(roles))
        check("risposta integrale persistita",
              c["messages"][-1]["content"] == FULL_SLOW,
              repr(c["messages"][-1]["content"])[:100])
        check("usage sul messaggio finale",
              (c["messages"][-1].get("usage") or {}).get("cost") == 0.001)
        wait_stream(stream)
        got = stream["events"]
        check("evento turn in testa allo stream dell'invio",
              got and got[0]["type"] == "turn"
              and got[0]["content"] == SLOW
              and got[0]["anonymized"] is False
              and got[0]["preview"] is False,
              json.dumps(got[0] if got else {}, ensure_ascii=False)[:120])

        # --- B. riaggancio a turno CONCLUSO: replay completo ----------------
        with client.stream("GET", f"/api/chats/{cid}/messages/live",
                           headers=auth) as resp:
            check("live risponde SSE", resp.status_code == 200
                  and "text/event-stream" in resp.headers["content-type"])
            events = sse_events(resp)
        text = "".join(e["delta"] for e in of_type(events, "text"))
        check("replay dall'inizio: turn, start, testo, done",
              events[0]["type"] == "turn"
              and events[0]["content"] == SLOW
              and len(of_type(events, "start")) == 1
              and text == FULL_SLOW
              and events[-1]["type"] == "done",
              f"{[e['type'] for e in events][:6]}... testo={text!r}"[:160])

        # --- C. riaggancio a turno IN CORSO ---------------------------------
        stream = open_stream(client, cid, auth, {"content": SLOW})
        wait_busy(client, auth, cid, True, 10)
        with client.stream("GET", f"/api/chats/{cid}/messages/live",
                           headers=auth) as resp:
            live = sse_events(resp)          # rigioca e segue fino al done
        text = "".join(e["delta"] for e in of_type(live, "text"))
        check("riaggancio a metà: niente perso, si segue fino in fondo",
              live and live[0]["type"] == "turn" and text == FULL_SLOW
              and live[-1]["type"] == "done",
              f"testo={text!r}"[:120])
        c = wait_busy(client, auth, cid, False, 10)
        check("secondo turno persistito",
              [m["role"] for m in c["messages"]] == ["user", "assistant"] * 2)

        # --- D. /stop ferma il turno (l'unico modo, ora) ---------------------
        stream = open_stream(client, cid, auth, {"content": SLOW})
        wait_busy(client, auth, cid, True, 10)
        r = client.post(f"/api/chats/{cid}/stop", headers=auth).json()
        check("stop accettato a turno in corso", r["stopping"] is True)
        c = wait_busy(client, auth, cid, False, 15)
        check("turno fermato", c["busy"] is False)
        with client.stream("GET", f"/api/chats/{cid}/messages/live",
                           headers=auth) as resp:
            ev = sse_events(resp)
        check("il replay del turno fermato chiude col done",
              ev and ev[-1]["type"] == "done"
              and ev[-1]["finish_reason"] in ("canceled", "stop"),
              ev[-1].get("finish_reason") if ev else "nessun evento")

        # --- E. /live senza turni da rigiocare -------------------------------
        cid2 = client.post("/api/chats", json={}, headers=auth).json()["id"]
        r = client.get(f"/api/chats/{cid2}/messages/live", headers=auth)
        check("live 404 dove non c'è niente", r.status_code == 404
              and "rigiocare" in r.json().get("detail", ""), r.text[:80])

        # --- F. chat anonimizzata: refresh DURANTE l'anonimizzazione ---------
        # L'anonimizzazione è la parte lenta del turno, ed è proprio lì che un
        # refresh capita: il messaggio utente va persistito PRIMA, non a valle,
        # o quella finestra se lo porta via. Il turno prosegue per conto suo e
        # /live rigioca anche gli eventi anon_start/anon_done.
        cid3 = client.post("/api/chats", json={"anonymized": True},
                           headers=auth).json()["id"]
        client.patch(f"/api/chats/{cid3}",
                     json={"model": "test/plain-model"}, headers=auth)
        msg = "Il cliente Mario Rossi ci ha scritto. LENTO"
        n_reqs = len(httpx.get(mock.DEBUG_URL).json())
        stream = open_stream(client, cid3, auth, {"content": msg})
        wait_busy(client, auth, cid3, True, 30)
        # il primo giro carica il modello PII: tempi larghi
        c = wait_busy(client, auth, cid3, False, timeout=300)
        wait_stream(stream, 30)
        got = stream["events"]
        check("turn di una chat anonimizzata",
              got and got[0]["type"] == "turn"
              and got[0]["anonymized"] is True)
        msgs = c["messages"]
        roles = [m["role"] for m in msgs]
        check("messaggio sopravvissuto al refresh in anonimizzazione",
              c["busy"] is False and roles == ["user", "assistant"],
              str(roles) + json.dumps(got[-1] if got else {},
                                      ensure_ascii=False)[:120])
        check("bolla utente col testo originale",
              bool(msgs) and msgs[0]["content"] == msg,
              repr(msgs[0].get("content"))[:80] if msgs else "nessun messaggio")
        check("turno marcato anonimizzato",
              bool(msgs) and bool(msgs[0]["anonymized"]))
        req = httpx.get(mock.DEBUG_URL).json()[n_reqs]
        txt = user_text(req)
        check("verso il modello è partito il testo redatto",
              "Mario Rossi" not in txt and "LENTO" in txt, txt[:100])
        with client.stream("GET", f"/api/chats/{cid3}/messages/live",
                           headers=auth) as resp:
            ev = sse_events(resp)
        check("il replay racconta anche l'anonimizzazione",
              len(of_type(ev, "anon_start")) == 1
              and len(of_type(ev, "anon_done")) == 1
              and ev[0]["type"] == "turn" and ev[-1]["type"] == "done",
              str([e["type"] for e in ev])[:160])

        # --- G. il buffer è del TURNO: il successivo lo sostituisce ---------
        with client.stream("POST", f"/api/chats/{cid}/messages",
                           json={"content": "Ciao."}, headers=auth) as resp:
            sse_events(resp)                  # letto fino in fondo
        with client.stream("GET", f"/api/chats/{cid}/messages/live",
                           headers=auth) as resp:
            ev = sse_events(resp)
        check("live rigioca solo l'ultimo turno",
              ev[0]["type"] == "turn" and ev[0]["content"] == "Ciao."
              and ev[-1]["type"] == "done", ev[0].get("content"))

    stop_mock()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

"""E2E su doppioni e falso avviso: riproduce SUL SERIO la chat
incriminata — un'esecuzione sandbox che salva png+svg e POI fallisce
(NameError), seguita dalla riesecuzione corretta che risalva gli stessi file.

Senza il dedup: 4 allegati in chat (due png e due svg identici), tutti marcati
"contiene ancora i segnaposto" pur senza alcuna entità nel registro.
Dopo il fix: 2 allegati (una riga per file, id stabile tra gli step), stato
"raw" e nessun report, perché in una chat anonimizzata senza entità non è
mai esistito alcun TAG.

Tutto vero: OpenRouter (SPENDE crediti), sandbox Docker, route via TestClient
con DB e cartelle isolati.

Serve Docker attivo con l'immagine della sandbox.
Uso:
    python backend/tests/artifact_dedupe_e2e_test.py [modello]
"""

import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

DATA = HERE / "data" / "test_dedupe_e2e"
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_SANDBOX_POOL"] = "1"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True, exist_ok=True)
REAL_KEY = HERE / "data" / "openrouter.key"
if REAL_KEY.is_file():
    shutil.copyfile(REAL_KEY, DATA / "openrouter.key")
else:
    # la chiave vive solo sul record utente (provisioning):
    # si prende quella dell'admin dal DB di esercizio, SOLO per la copia di test
    import sqlite3
    con = sqlite3.connect(str(HERE / "data" / "blockingbear.db"))
    row = con.execute("select openrouter_key from users "
                      "where openrouter_key is not null limit 1").fetchone()
    con.close()
    if not row or not row[0]:
        sys.exit("Nessuna chiave OpenRouter: né backend/data/openrouter.key "
                 "né una chiave utente nel DB.")
    (DATA / "openrouter.key").write_text(row[0].strip(), encoding="utf-8")

import httpx                                                        # noqa: E402
from fastapi import FastAPI                                         # noqa: E402
from fastapi.testclient import TestClient                           # noqa: E402

from app import db                                                  # noqa: E402
from app.auth import seed_admin                                     # noqa: E402
from app.db import init_db                                          # noqa: E402
from app.openrouter import sandbox                                  # noqa: E402
from app.routes import auth_routes, chat_routes                     # noqa: E402

sandbox._NAME_PREFIX = "blockingbear-sbxd-"

MODEL = sys.argv[1] if len(sys.argv) > 1 else "anthropic/claude-sonnet-4.5"

# Il codice va eseguito COM'È: savefig di png+svg, POI il NameError — così
# la prima esecuzione fallisce con i file già scritti (new_files pieno) e la
# seconda li riscrive con gli stessi nomi.
PROMPT = """Sto testando la gestione degli errori della sandbox. \
Esegui ESATTAMENTE questo codice, senza correggerlo e senza aggiungere nulla:

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
fig, ax = plt.subplots()
ax.plot([1, 2, 3], [2, 4, 3])
fig.savefig("/workspace/outputs/linea.png")
fig.savefig("/workspace/outputs/linea.svg")
pltlt.close(fig)

Otterrai un NameError sull'ultima riga: è voluto. A quel punto riesegui \
l'INTERO codice una seconda volta correggendo solo l'ultima riga in \
plt.close(fig), risalvando i file con gli STESSI nomi. Alla fine rispondi \
solo "fatto"."""

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


def sse_events(response):
    events = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def main():
    print(f"Modello: {MODEL}")
    print("Avvio sandbox Docker...")
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(chat_routes.router)
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)
    sandbox.start()
    deadline = time.time() + 120
    while time.time() < deadline and not (sandbox.status()["available"]
                                          and sandbox.status()["pool_ready"]):
        time.sleep(0.5)
    if not sandbox.status()["available"]:
        sys.exit(f"Docker non disponibile: {sandbox.status()}")

    client = TestClient(app)
    client.timeout = httpx.Timeout(900.0)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = client.get("/api/openrouter/status", headers=auth).json()
    if not r.get("configured"):
        sys.exit(f"Chiave OpenRouter non valida: {r}")
    print(f"  crediti OpenRouter: {r.get('credits')}")

    # chat ANONIMIZZATA ma senza alcun dato personale nel prompt: il registro
    # deve restare vuoto per tutto il turno (è il caso del falso avviso)
    conv = client.post("/api/chats", json={"model": MODEL, "anonymized": True},
                       headers=auth).json()
    cid = conv["id"]

    print("Turno in corso (errore voluto -> retry del modello)...")
    t0 = time.time()
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": PROMPT}, headers=auth) as resp:
        events = sse_events(resp)
    elapsed = time.time() - t0
    errors = [e for e in events if e["type"] == "error"]
    done = next((e for e in reversed(events) if e["type"] == "done"), None)
    print(f"  turno concluso in {elapsed:.0f}s, eventi: {len(events)}")
    for e in errors:
        print(f"  ERRORE dal turno: {e.get('message')}")
    if done:
        usage = done.get("usage") or {}
        print(f"  iterazioni: {done.get('iterations')} costo: "
              f"${usage.get('cost', 0):.4f}")
    check("il turno si è chiuso senza errori fatali",
          done is not None and not [e for e in errors if e.get("fatal")],
          str([e.get("message") for e in errors])[:200])

    # lo scenario si è riprodotto davvero? (senza doppio salvataggio le
    # verifiche sul dedupe non proverebbero niente)
    produced = [e for e in events if e["type"] == "tool_result"
                and any(n.endswith("linea.png")
                        for n in (e.get("result", {}).get("new_files") or []))]
    check("il modello ha salvato linea.png in DUE esecuzioni (repro del bug)",
          len(produced) >= 2, f"esecuzioni col file: {len(produced)}")
    failed_first = any(e["result"].get("outcome") == "error" for e in produced)
    check("la prima esecuzione è fallita DOPO il savefig",
          failed_first,
          str([e["result"].get("outcome") for e in produced]))

    # gli step successivi devono RIUSARE gli stessi attachment id
    by_event = [{a["filename"]: a["id"] for a in (e.get("attachments") or [])}
                for e in produced]
    if len(by_event) >= 2:
        check("gli step riusano gli stessi attachment id",
              by_event[0] == by_event[1], f"{by_event[0]} vs {by_event[1]}")

    # in DB: una riga per file, non una per esecuzione
    with SessionLocal() as s:
        rows = (s.query(db.Attachment)
                .filter_by(conv_id=cid, direction="out").all())
        names = Counter(r.filename for r in rows)
        print(f"  allegati in DB: {dict(names)}")
        check("UNA riga per file (niente doppioni)",
              len(rows) == 2 and all(v == 1 for v in names.values()),
              dict(names))
        check("tutti agganciati al messaggio finale",
              all(r.message_id for r in rows),
              [r.message_id for r in rows])
        check("registro vuoto -> stato raw, nessuna etichetta in UI",
              all(r.anonymization_status == "raw" for r in rows),
              [r.anonymization_status for r in rows])
        check("registro vuoto -> nessun avviso sui segnaposto",
              all(not r.anonymization_report_json for r in rows),
              [r.anonymization_report_json for r in rows])
        check("i byte sono sul disco e la size corrisponde",
              all(Path(r.original_path).is_file()
                  and r.size == Path(r.original_path).stat().st_size
                  for r in rows))
        ents = s.query(db.ConversationEntity).filter_by(conv_id=cid).count()
        check("il registro è davvero rimasto vuoto", ents == 0, ents)

    # ciò che la UI ricarica a fine turno: 2 card, scaricabili
    got = client.get(f"/api/chats/{cid}", headers=auth).json()
    outs = [a for a in got.get("attachments", []) if a["direction"] == "out"]
    check("la chat ricaricata mostra 2 allegati", len(outs) == 2,
          [a["filename"] for a in outs])
    for a in outs:
        data = client.get(f"/api/chats/{cid}/attachments/{a['id']}",
                          headers=auth).content
        ok = (data.startswith(b"\x89PNG") if a["filename"].endswith(".png")
              else b"<svg" in data[:2048])
        check(f"{a['filename']} si scarica ed è integro", ok,
              f"{len(data)} byte")

    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        sandbox.shutdown()
    sys.exit(code)

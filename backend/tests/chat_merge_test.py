"""Fusione delle entità PRIMA che il turno parta.

Il registro di una chat libera nasce all'invio, quindi la domanda "queste due
entità sono la stessa?" si può porre solo dopo l'anonimizzazione. Il turno si
FERMA lì (evento SSE `merge_check`), niente parte verso il modello, e la
scelta entra nel system prompt di QUESTO stesso messaggio: prima di questa
versione la nota di equivalenza arrivava solo dal turno successivo.

Copre:
  - il turno si ferma su merge_check e non manda niente al modello;
  - «Unisci» + /messages/continue: il turno riprende e la nota di equivalenza
    è nel SUO system prompt;
  - i file NON vengono ri-redatti dalla fusione (contratto: cambia la mappa,
    il modello lo sa dal system prompt);
  - «Mantieni separate» non fa ricomparire la domanda ai turni successivi;
  - dall'anteprima pre-invio (from_staged) la domanda non si ripete, e il
    descrittore staged porta i suggerimenti ricalcolati;
  - un valore lasciato in chiaro (excluded) non viene più proposto;
  - /messages/continue senza turno in attesa -> 409.

Serve il finto OpenRouter (si avvia da solo) e il modello PII vero. Niente
Docker: si usa test/plain-model (senza tool), la sandbox non serve.

Uso:
    python backend/tests/chat_merge_test.py
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

os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_merge")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_OPENROUTER_BASE_URL"] = mock.BASE_URL

import httpx                                                     # noqa: E402
from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import chat_anonymization as ca                          # noqa: E402
from app import chat_staging, settings_store                      # noqa: E402
from app.auth import seed_admin                                  # noqa: E402
from app.db import ConversationEntity, ConversationEntityAlias, \
    init_db                                                       # noqa: E402
from app.openrouter import client as or_client                   # noqa: E402
from app.routes import auth_routes, chat_routes                  # noqa: E402

PASS = 0
FAIL = 0

TXT = ("Contratto di manutenzione.\n"
       "Referente tecnico: Giulia Ferrari (giulia.ferrari@example.com).\n"
       "Importo annuo concordato: 12.000 euro.\n")
PROMPT = "Riassumi il contratto allegato in tre righe."


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def sent():
    return httpx.get(mock.DEBUG_URL).json()


def system_text(request):
    return next(m["content"] for m in request["messages"]
                if m["role"] == "system")


def open_stream(client, cid, auth, body):
    """Consuma lo stream in un THREAD. Serve: il turno si ferma sul gate delle
    fusioni, e per rispondergli bisogna poter fare altre richieste mentre lo
    stream è ancora aperto (il thread principale sarebbe bloccato in lettura).
    """
    out = {"events": [], "done": False}

    def run():
        try:
            with client.stream("POST", f"/api/chats/{cid}/messages",
                               json=body, headers=auth) as resp:
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        out["events"].append(json.loads(line[6:]))
        finally:
            out["done"] = True

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return out


def wait_gate(cid, out, timeout=180):
    """Il turno è fermo in attesa delle fusioni? Si guarda il registro dei
    gate del router, non lo stream: TestClient consegna il corpo SSE solo a
    risposta CONCLUSA, mentre qui serve intervenire mentre il turno aspetta.
    L'ordine degli eventi si verifica dopo, sulla lista completa."""
    deadline = time.time() + timeout
    while time.time() < deadline and not out["done"]:
        if cid in chat_routes._merge_gates:
            return True
        time.sleep(0.05)
    return False


def wait_stream(out, timeout=180):
    """Aspetta che lo stream si chiuda (e con esso il turno)."""
    deadline = time.time() + timeout
    while time.time() < deadline and not out["done"]:
        time.sleep(0.05)
    return out["done"]


def kinds(out):
    return [e["type"] for e in out["events"]]


def before(out, first, second):
    ks = kinds(out)
    return (first in ks and second in ks
            and ks.index(first) < ks.index(second))


def seed_pair(SessionLocal, scope, short, full, n):
    """Due entità della stessa label che il resolver tiene separate ma che
    merge_suggestions propone (insieme di token della prima incluso nella
    seconda). Sono righe di registro come quelle che l'anonimizzazione scrive:
    quello che si vuole provare qui è il flusso, non il rilevatore."""
    with SessionLocal() as session:
        rows = [ConversationEntity(conv_id=scope, label="FULLNAME",
                                   placeholder=f"[FULLNAME_{n}]",
                                   canonical_value=short),
                ConversationEntity(conv_id=scope, label="FULLNAME",
                                   placeholder=f"[FULLNAME_{n + 1}]",
                                   canonical_value=full)]
        session.add_all(rows)
        session.commit()
        return {r.canonical_value: r.id for r in rows}


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
        files={"file": ("contratto.txt", TXT.encode(), "text/plain")},
        headers=auth).json()

    # --- il turno si ferma sulla domanda ------------------------------------
    ids = seed_pair(SessionLocal, cid, "Ferrari", "Giulia Ferrari", 90)
    n_reqs = len(sent())
    stream = open_stream(client, cid, auth, {"content": PROMPT})
    check("il turno si ferma in attesa delle fusioni", wait_gate(cid, stream))
    check("niente inviato al modello mentre il turno è fermo",
          len(sent()) == n_reqs)
    check("il file è già protetto (la domanda non lo blocca)",
          client.get(f"/api/chats/{cid}/attachments/{att['id']}/anonymized",
                     headers=auth).status_code == 200)
    protected_before = client.get(
        f"/api/chats/{cid}/attachments/{att['id']}/anonymized",
        headers=auth).content

    # --- «Unisci» mentre il turno aspetta -----------------------------------
    r = client.post(f"/api/chats/{cid}/entities/{ids['Ferrari']}/merge",
                    json={"into": ids["Giulia Ferrari"]}, headers=auth)
    check("fusione accettata a turno fermo", r.status_code == 200, r.text[:80])
    check("la coppia non è più proposta", r.json()["suggestions"] == [])
    check("la fusione non ri-redige il file protetto",
          client.get(f"/api/chats/{cid}/attachments/{att['id']}/anonymized",
                     headers=auth).content == protected_before)

    r = client.post(f"/api/chats/{cid}/messages/continue", headers=auth)
    check("continue accettato", r.status_code == 200, r.text[:80])
    check("il turno riprende e finisce", wait_stream(stream)
          and kinds(stream)[-1] == "done", json.dumps(kinds(stream)))
    check("ordine degli eventi: anonimizza, chiedi, poi parti",
          before(stream, "anon_done", "merge_check")
          and before(stream, "merge_check", "start"),
          json.dumps(kinds(stream)))
    gate = next(e for e in stream["events"] if e["type"] == "merge_check")
    check("la coppia proposta è quella attesa",
          [(s["source_value"], s["target_value"])
           for s in gate["suggestions"]] == [("Ferrari", "Giulia Ferrari")],
          json.dumps(gate["suggestions"], ensure_ascii=False))

    req = sent()[n_reqs]
    check("la nota di equivalenza è nel system prompt DI QUESTO turno",
          "[FULLNAME_90] = [FULLNAME_91]" in system_text(req),
          system_text(req)[-90:].replace("\n", " "))

    # --- niente domanda quando non c'è più niente da chiedere -------------
    stream = open_stream(client, cid, auth, {"content": "E il resto?"})
    check("secondo turno senza domanda", wait_stream(stream)
          and "merge_check" not in kinds(stream), json.dumps(kinds(stream)))

    r = client.post(f"/api/chats/{cid}/messages/continue", headers=auth)
    check("continue senza turno in attesa -> 409", r.status_code == 409,
          r.text[:80])

    # --- «Mantieni separate»: la domanda non torna --------------------------
    ids = seed_pair(SessionLocal, cid, "Bianchi", "Luca Bianchi", 92)
    stream = open_stream(client, cid, auth, {"content": "Terzo messaggio."})
    check("nuova coppia: il turno si ferma di nuovo", wait_gate(cid, stream))
    client.post(f"/api/chats/{cid}/entities/{ids['Bianchi']}/keep-separate",
                headers=auth)
    client.post(f"/api/chats/{cid}/messages/continue", headers=auth)
    check("il turno riprende dopo «mantieni separate»", wait_stream(stream)
          and kinds(stream)[-1] == "done", json.dumps(kinds(stream)))
    stream = open_stream(client, cid, auth, {"content": "Quarto messaggio."})
    check("coppia respinta: non si richiede più", wait_stream(stream)
          and "merge_check" not in kinds(stream), json.dumps(kinds(stream)))

    # --- anteprima pre-invio: la domanda è l'ultimo step del modal ---------
    ids = seed_pair(SessionLocal, cid, "Conti", "Elena Conti", 94)
    stream = open_stream(client, cid, auth, {"content": PROMPT,
                                             "preview": True})
    check("anteprima pronta", wait_stream(stream)
          and "staged" in kinds(stream), json.dumps(kinds(stream)))
    staged = next(e for e in stream["events"] if e["type"] == "staged")
    check("il descrittore staged porta i suggerimenti",
          [(s["source_value"], s["target_value"])
           for s in staged["suggestions"]] == [("Conti", "Elena Conti")],
          json.dumps(staged["suggestions"], ensure_ascii=False))

    r = client.post(f"/api/chats/{cid}/entities/{ids['Conti']}/merge",
                    json={"into": ids["Elena Conti"]}, headers=auth)
    check("fusione dall'anteprima: risponde con lo stato staged",
          r.status_code == 200 and "staged" in r.json()
          and r.json()["staged"]["suggestions"] == [], r.text[:80])

    # una coppia ancora aperta: from_staged NON deve richiedere niente (nel
    # modal la domanda è già stata posta, ignorarla è una risposta)
    ids = seed_pair(SessionLocal, cid, "Neri", "Sara Neri", 96)
    n_reqs = len(sent())
    stream = open_stream(client, cid, auth, {"content": PROMPT,
                                             "from_staged": True})
    check("from_staged non ripete la domanda", wait_stream(stream)
          and "merge_check" not in kinds(stream)
          and kinds(stream)[-1] == "done", json.dumps(kinds(stream)))
    check("la nota copre anche la fusione fatta dal modal",
          "[FULLNAME_94] = [FULLNAME_95]" in system_text(sent()[n_reqs]))

    # --- un valore lasciato in chiaro non si propone più -------------------
    with SessionLocal() as s:
        s.get(ConversationEntity, ids["Neri"]).excluded = 1
        s.commit()
    r = client.get(f"/api/chats/{cid}/entities", headers=auth)
    check("entità esclusa: fuori dai suggerimenti",
          r.json()["suggestions"] == [],
          json.dumps(r.json()["suggestions"], ensure_ascii=False))
    stream = open_stream(client, cid, auth, {"content": "Quinto messaggio."})
    check("e il turno non si ferma", wait_stream(stream)
          and "merge_check" not in kinds(stream), json.dumps(kinds(stream)))

    # --- ri-redazione dell'anteprima DOPO una fusione -----------------------
    # La fusione non ri-redige niente, ma una modifica manuale successiva
    # (anonimizza in più) sì: la ri-redazione ricuce il messaggio dagli span
    # dell'ANALISI, che conservano il segnaposto di prima della fusione. Va
    # tradotto, o il messaggio userebbe un tag che gli allegati ri-redatti non
    # usano più.
    conv2 = client.post("/api/chats", json={"anonymized": True},
                        headers=auth).json()
    cid2 = conv2["id"]
    client.patch(f"/api/chats/{cid2}", json={"model": "test/plain-model"},
                 headers=auth)
    ids = seed_pair(SessionLocal, cid2, "Ferrari", "Giulia Ferrari", 98)
    with SessionLocal() as s:
        s.add(ConversationEntityAlias(
            entity_id=ids["Ferrari"], original_surface="Ferrari",
            normalized_key=ca._surface_key("FULLNAME", "Ferrari"),
            source="user", confidence="exact", mapping_version=0))
        s.commit()
    stream = open_stream(client, cid2, auth, {
        "content": "Sentiamo Ferrari per il contratto.", "preview": True})
    check("anteprima del solo messaggio pronta", wait_stream(stream)
          and "staged" in kinds(stream), json.dumps(kinds(stream)))
    check("il messaggio usa il segnaposto della sorgente",
          "[FULLNAME_98]" in chat_staging.state(cid2)["model_content"],
          chat_staging.state(cid2)["model_content"])

    client.post(f"/api/chats/{cid2}/entities/{ids['Ferrari']}/merge",
                json={"into": ids["Giulia Ferrari"]}, headers=auth)
    check("la fusione da sola non ritocca il messaggio",
          "[FULLNAME_98]" in chat_staging.state(cid2)["model_content"])
    r = client.post(f"/api/chats/{cid2}/staged/anonymize-text",
                    json={"text": "contratto"}, headers=auth)
    check("anonimizza-in-più accettato", r.status_code == 200, r.text[:120])
    model_content = chat_staging.state(cid2)["model_content"]
    check("ri-redigendo, il messaggio passa al segnaposto di destinazione",
          "[FULLNAME_99]" in model_content
          and "[FULLNAME_98]" not in model_content, model_content)

    stop_mock()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

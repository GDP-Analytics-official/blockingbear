"""Le route della chat (app/routes/chat_routes.py) da un capo all'altro: app
FastAPI ridotta (auth + chat, SENZA jobs/torch) con TestClient, finto
OpenRouter e sandbox Docker vera.

Copre: chiave admin, catalogo, CRUD conversazioni, allegati con scheda del
contenuto, turno SSE completo con artifact, aggancio degli allegati al turno,
allegati multimodali (immagine mostrata al modello), opzioni di ragionamento e
parametri, costo della conversazione, persistenza + echo al turno successivo,
modello senza tool, cancellazione.

Il finto OpenRouter si avvia da solo. Serve Docker attivo e l'immagine:
    docker build -t blockingbear-sandbox:1 backend/sandbox
Uso:
    python backend/tests/chat_routes_test.py
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import chat_mock_openrouter as mock                              # noqa: E402

# PRIMA di ogni import di app.*: dati isolati (chiave e DB dei test non
# toccano quelli di esercizio) e OpenRouter dirottato sul finto server —
# le route non prendono base_url come parametro, la leggono da qui
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_routes")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_SANDBOX_POOL"] = "1"
os.environ["BLOCKINGBEAR_OPENROUTER_BASE_URL"] = mock.BASE_URL

import httpx                                                     # noqa: E402
from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import settings_store                                   # noqa: E402
from app.auth import hash_password, seed_admin                    # noqa: E402
from app.db import (ConversationEntity, ConversationEntityAlias, User,
                    init_db)                                     # noqa: E402
from app.openrouter import client as or_client                   # noqa: E402
from app.openrouter import sandbox                               # noqa: E402
from app.routes import auth_routes, chat_routes                  # noqa: E402

# prefisso dei container diverso da quello di esercizio: così lo sweep degli
# orfani allo start non azzera il pool di un backend di sviluppo attivo
sandbox._NAME_PREFIX = "blockingbear-sbxt-"

ASSETS = Path(__file__).resolve().parent / "assets"
# PNG 1x1 valido: basta a far scattare il percorso multimodale
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082")

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
    """Legge una risposta SSE del TestClient e ritorna gli eventi (dict)."""
    events = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def of_type(events, t):
    return [e for e in events if e["type"] == t]


def sent():
    return httpx.get(mock.DEBUG_URL).json()


def user_text(request):
    """Il testo dell'ultimo messaggio utente inviato al modello (che sia una
    stringa o una lista di parti multimodali)."""
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
    sandbox.start()
    deadline = time.time() + 90
    while time.time() < deadline and not (
            sandbox.status()["available"]
            and sandbox.status()["pool_ready"]):
        time.sleep(0.5)

    client = TestClient(app)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    # --- chiave ------------------------------------------------------------
    r = client.get("/api/openrouter/status", headers=auth).json()
    check("stato iniziale", r["configured"] is False
          and r["sandbox"]["available"] is True, json.dumps(r["sandbox"]))

    mock.grant_key(SessionLocal)
    r = client.get("/api/openrouter/status", headers=auth).json()
    check("stato con chiave personale", r["configured"] is True
          and r["personal_key"] is True
          and "sk-or-test-123456789" not in r["masked"]
          and "…" in r["masked"], r.get("masked", ""))

    # --- catalogo ------------------------------------------------------------
    r = client.get("/api/openrouter/models", headers=auth).json()
    happy = next(m for m in r["models"] if m["id"] == "test/happy-model")
    free = next(m for m in r["models"]
                if m["id"] == "test/happy-model:free")
    plain = next(m for m in r["models"] if m["id"] == "test/plain-model")
    nozdr = next(m for m in r["models"] if m["id"] == "test/nozdr-model")
    check("catalogo da /models/user", r["source"] == "user"
          and len(r["models"]) == 7)
    check("capability derivate", happy["tools"] is True
          and happy["reasoning"]["supported_efforts"] == ["high", "medium",
                                                          "low"]
          and happy["input_modalities"] == ["text", "image", "file"]
          and plain["tools"] is False and plain["moderated"] is True
          and free["variant_of"] == "test/happy-model")
    check("provider ZDR per modello",
          happy["zdr_providers"] == ["AltroFinto", "FintoProvider"]
          and nozdr["zdr_providers"] == []
          and free["zdr_providers"] == ["AltroFinto", "FintoProvider"],
          f"happy={happy['zdr_providers']} nozdr={nozdr['zdr_providers']} "
          f"free(variante)={free['zdr_providers']}")

    # --- conversazione + allegato con scheda ---------------------------------
    conv = client.post("/api/chats", json={}, headers=auth).json()
    cid = conv["id"]
    r = client.post(f"/api/chats/{cid}/messages",
                    json={"content": "ciao"}, headers=auth)
    check("messaggio senza modello rifiutato", r.status_code == 422)

    client.patch(f"/api/chats/{cid}", json={"model": "test/happy-model"},
                 headers=auth)
    xlsx = (ASSETS / "cartella_multifoglio.xlsx").read_bytes()
    att_in = client.post(
        f"/api/chats/{cid}/attachments",
        files={"file": ("cartella_multifoglio.xlsx", xlsx,
                        "application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet")},
        headers=auth).json()
    check("upload allegato", att_in["direction"] == "in"
          and att_in["filename"] == "cartella_multifoglio.xlsx"
          and att_in["size"] == len(xlsx) and att_in["message_id"] is None)
    check("scheda del contenuto all'upload",
          att_in["briefing"]["kind"] == "excel"
          # 49 = somma delle DIMENSIONI DICHIARATE dei cinque fogli
          # (briefing._scan_sheet legge <dimension ref>, non scorre le righe):
          # Clienti 11 + Fornitori 4 + Fatture 26 + Riepilogo 5 + Note 3.
          # Conta i buchi delle celle sparse (Riepilogo usa A1/A3/A5), ed è
          # voluto: su un foglio da 200.000 righe non si scorre nulla.
          and att_in["briefing"]["label"] == "5 fogli · 49 righe"
          and any("colonne: Nome | Email" in ln
                  for ln in att_in["briefing"]["lines"]),
          att_in["briefing"]["label"])

    # --- turno completo con sandbox ------------------------------------------
    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": "Quanto fa 6*7? Scrivilo in un file."},
                       headers=auth) as resp:
        check("risposta SSE", resp.status_code == 200
              and "text/event-stream" in resp.headers["content-type"])
        events = sse_events(resp)

    check("evento turn in testa (riaggancio dopo refresh)",
          events[0]["type"] == "turn"
          and events[0]["content"] == "Quanto fa 6*7? Scrivilo in un file."
          and events[0]["anonymized"] is False
          and events[0]["preview"] is False)
    check("evento start col messaggio utente",
          events[1]["type"] == "start"
          and events[1]["user_message"]["role"] == "user")
    text = "".join(e["delta"] for e in of_type(events, "text"))
    check("testo streamato", text == "Calcolo con la sandbox."
                                     "Il risultato è 42.", repr(text))
    results = of_type(events, "tool_result")
    check("artifact registrato nell'evento",
          len(results) == 2 and len(results[1]["attachments"]) == 1
          and results[1]["attachments"][0]["filename"] == "risposta.txt"
          and results[1]["attachments"][0]["direction"] == "out"
          and "files" not in results[1]["result"])
    done = events[-1]
    check("done con modello e costi", done["type"] == "done"
          and done["model"] == "test/happy-model"
          and abs(done["usage"]["cost"] - 0.004) < 1e-9)
    # occupazione del contesto = ULTIMA iterazione (100+40), non la somma
    # delle tre (420): ogni giro rispedisce tutta la storia
    check("context_tokens dall'ultima iterazione",
          done["usage"]["context_tokens"] == 140,
          json.dumps(done["usage"]))
    check("tempi del turno nel done",
          done["usage"]["elapsed_ms"] >= done["usage"]["response_ms"] >= 0
          and done["elapsed_ms"] == done["usage"]["elapsed_ms"],
          json.dumps(done["usage"]))

    # la scheda dell'allegato è arrivata al modello, e con la sola scheda:
    # niente byte dell'xlsx (nessun modello "vede" un foglio di calcolo)
    req = sent()[n_reqs]
    txt = user_text(req)
    check("scheda dell'allegato nel contesto",
          "<allegati>" in txt and "allegato_01.xlsx" in txt
          and "cartella_multifoglio.xlsx" not in txt
          and 'foglio "Clienti"' in txt and "/workspace/inputs" in txt
          and isinstance(req["messages"][-1]["content"], str),
          f"{len(txt)} caratteri di contesto")
    check("regole di sandbox nel system prompt",
          "execute_python" in req["messages"][0]["content"])

    # --- persistenza -----------------------------------------------------------
    full = client.get(f"/api/chats/{cid}", headers=auth).json()
    roles = [m["role"] for m in full["messages"]]
    check("thread persistito", roles == ["user", "assistant", "tool",
                                         "assistant", "tool", "assistant"],
          str(roles))
    last = full["messages"][-1]
    check("metadati sull'assistant finale", last["finish_reason"] == "stop"
          and last["model"] == "test/happy-model"
          and abs(last["usage"]["cost"] - 0.004) < 1e-9
          and last["content"] == "Il risultato è 42.")
    first_a = full["messages"][1]
    check("tool_calls e reasoning persistiti",
          first_a["tool_calls"][0]["id"] == "call_1"
          and first_a["reasoning"] == "Devo eseguire del codice.")
    check("allegati della conversazione", len(full["attachments"]) == 2
          and {a["direction"] for a in full["attachments"]} == {"in", "out"})
    check("titolo automatico", full["title"].startswith("Quanto fa 6*7?"))
    check("costo totale della conversazione",
          abs(full["usage_total"]["cost"] - 0.004) < 1e-9
          and full["usage_total"]["prompt_tokens"] == 300,
          json.dumps(full["usage_total"]))
    check("misure persistite nello usage",
          last["usage"]["context_tokens"] == 140
          and last["usage"]["elapsed_ms"] >= last["usage"]["response_ms"] >= 0,
          json.dumps(last["usage"]))

    out_att = next(a for a in full["attachments"]
                   if a["direction"] == "out")
    in_att = next(a for a in full["attachments"] if a["direction"] == "in")
    r = client.get(f"/api/chats/{cid}/attachments/{out_att['id']}",
                   headers=auth)
    check("download artifact", r.status_code == 200 and r.content == b"42")
    check("artifact agganciato al messaggio",
          out_att["message_id"] == full["messages"][-1]["id"],
          f"message_id={out_att['message_id']}")
    check("upload agganciato al messaggio utente",
          in_att["message_id"] == full["messages"][0]["id"],
          f"message_id={in_att['message_id']}")

    # --- turno 2: la storia ricostruita deve contenere l'echo ------------------
    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": "E adesso?"}, headers=auth) as resp:
        events = sse_events(resp)
    req = sent()[n_reqs]
    smsg = req["messages"]
    sent_assistant = next(m for m in smsg if m["role"] == "assistant"
                          and m.get("tool_calls"))
    check("echo dal DB al turno dopo",
          smsg[0]["role"] == "system"
          and sent_assistant["tool_calls"][0]["id"] == "call_1"
          and sent_assistant["reasoning_details"][0]["signature"] == "sig-abc"
          and sum(1 for m in smsg if m["role"] == "tool") == 2,
          f"{len(smsg)} messaggi inviati")
    check("la scheda resta sul messaggio che l'ha portata",
          "<allegati>" in smsg[1]["content"]
          and "<allegati>" not in smsg[-1]["content"])
    check("turno 2 concluso",
          events[-1]["type"] == "done"
          and events[-1]["finish_reason"] == "stop")

    # --- immagine mostrata al modello + opzioni --------------------------------
    conv3 = client.post("/api/chats", json={"model": "test/happy-model"},
                        headers=auth).json()
    png_att = client.post(f"/api/chats/{conv3['id']}/attachments",
                          files={"file": ("foto.png", PNG, "image/png")},
                          headers=auth).json()
    check("in chat normale l'immagine si allega senza cerimonie",
          png_att["anonymization_status"] == "raw")
    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{conv3['id']}/messages",
                       json={"content": "Cosa vedi?"}, headers=auth) as resp:
        sse_events(resp)
    parts = sent()[n_reqs]["messages"][-1]["content"]
    check("immagine allegata al messaggio",
          isinstance(parts, list) and parts[0]["type"] == "text"
          and parts[1]["type"] == "image_url"
          and parts[1]["image_url"]["url"].startswith(
              "data:image/png;base64,")
          and "guardarlo direttamente" in parts[0]["text"],
          str([p["type"] for p in parts]) if isinstance(parts, list)
          else "content è una stringa")

    client.patch(f"/api/chats/{conv3['id']}", headers=auth, json={"options": {
        "reasoning": {"enabled": True, "effort": "low"},
        "params": {"temperature": 0.2, "top_k": 9}}})
    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{conv3['id']}/messages",
                       json={"content": "E adesso?"}, headers=auth) as resp:
        sse_events(resp)
    req = sent()[n_reqs]
    check("opzioni del modello applicate e filtrate",
          req.get("reasoning") == {"enabled": True, "effort": "low"}
          and req.get("temperature") == 0.2 and "top_k" not in req,
          json.dumps({k: req.get(k) for k in ("reasoning", "temperature",
                                              "top_k")}))
    check("privacy imposta su ogni richiesta",
          req["provider"]["zdr"] is True
          and req["provider"]["data_collection"] == "deny")

    # --- modello senza tool ------------------------------------------------------
    conv2 = client.post("/api/chats", json={"model": "test/plain-model"},
                        headers=auth).json()
    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{conv2['id']}/messages",
                       json={"content": "ciao"}, headers=auth) as resp:
        events = sse_events(resp)
    req = sent()[n_reqs]
    text = "".join(e["delta"] for e in of_type(events, "text"))
    check("modello senza tool: richiesta pulita",
          "tools" not in req and text == "Risposta senza sandbox."
          and req["provider"] == {"zdr": True, "data_collection": "deny"})
    check("modello senza tool: niente promesse di sandbox",
          "execute_python" not in req["messages"][0]["content"])

    # A provider failure after the first token must survive a page reload.
    error_conv = client.post(
        "/api/chats", json={"model": "test/error-model"}, headers=auth).json()
    warning_lines = []
    original_warning = chat_routes.log.warning
    chat_routes.log.warning = lambda message, *args: warning_lines.append(
        message % args if args else message)
    try:
        with client.stream("POST", f"/api/chats/{error_conv['id']}/messages",
                           json={"content": "provoca errore"},
                           headers=auth) as resp:
            error_events = sse_events(resp)
    finally:
        chat_routes.log.warning = original_warning
    error_full = client.get(
        f"/api/chats/{error_conv['id']}", headers=auth).json()
    error_assistant = error_full["messages"][-1]
    check("errore provider mid-stream persistito col turno",
          of_type(error_events, "error")
          and error_assistant["role"] == "assistant"
          and error_assistant["finish_reason"] == "error"
          and "Provider disconnected" in error_assistant["error"]
          and error_assistant["content"] == "Inizio a rispond",
          json.dumps(error_assistant, ensure_ascii=False)[:180])
    check("errore OpenRouter scritto nei log server con id e modello",
          any(error_conv["id"] in line
              and "test/error-model" in line
              and "Provider disconnected" in line
              for line in warning_lines),
          repr(warning_lines)[:180])

    # --- regole privacy: modello senza ZDR e deroga ------------------------------
    conv4 = client.post("/api/chats", json={"model": "test/nozdr-model"},
                        headers=auth).json()
    with client.stream("POST", f"/api/chats/{conv4['id']}/messages",
                       json={"content": "ciao"}, headers=auth) as resp:
        events = sse_events(resp)
    errs = of_type(events, "error")
    check("modello senza ZDR: errore che spiega, non un 503 crudo",
          errs and "Zero Data Retention" in errs[0]["message"]
          and events[-1]["finish_reason"] == "error",
          errs[0]["message"][:80] if errs else "nessun evento error")
    failed = client.get(f"/api/chats/{conv4['id']}", headers=auth).json()
    failed_assistant = failed["messages"][-1]
    with SessionLocal() as s:
        failed_history = chat_routes._history(
            s, conv4["id"], {"text"}, 8 * 1024 * 1024)
    check("errore pre-stream persistito ma escluso dalla history OpenRouter",
          failed_assistant["role"] == "assistant"
          and failed_assistant["finish_reason"] == "error"
          and "Zero Data Retention" in failed_assistant["error"]
          and [m["role"] for m in failed_history] == ["user"],
          json.dumps(failed_assistant, ensure_ascii=False)[:180])

    # la deroga esiste solo se l'amministratore l'ha abilitata: da spenta non
    # deroga nessuno, nemmeno l'admin
    st = client.get("/api/openrouter/status", headers=auth).json()
    r = client.patch(f"/api/chats/{conv4['id']}", headers=auth,
                     json={"options": {"allow_non_zdr": True}})
    check("deroga disabilitata: nemmeno l'admin può derogare",
          st["allow_non_zdr"] is False and r.status_code == 403, r.text[:90])

    with SessionLocal() as s:
        settings_store.set_values(s, {"chat_allow_non_zdr": 1})
        if not s.query(User).filter_by(username="mario").first():
            s.add(User(username="mario", password_hash=hash_password("mario1"),
                       role="standard"))
            s.commit()
    # This fixture creates the user directly instead of going through the
    # provisioning route, so grant the per-user inference key explicitly.
    mock.grant_key(SessionLocal, "mario")
    st = client.get("/api/openrouter/status", headers=auth).json()
    check("stato: deroga abilitata sul server", st["allow_non_zdr"] is True)

    stoken = client.post("/api/auth/login", json={
        "username": "mario", "password": "mario1"}).json()["token"]
    sauth = {"Authorization": f"Bearer {stoken}"}
    conv5 = client.post("/api/chats", json={"model": "test/nozdr-model"},
                        headers=sauth).json()
    r = client.patch(f"/api/chats/{conv5['id']}", headers=sauth,
                     json={"options": {"allow_non_zdr": True}})
    check("abilitata, la deroga è dell'utente sulla sua conversazione",
          r.status_code == 200
          and r.json()["options"]["allow_non_zdr"] is True, r.text[:90])

    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{conv5['id']}/messages",
                       json={"content": "ciao"}, headers=sauth) as resp:
        events = sse_events(resp)
    req = sent()[n_reqs]
    text = "".join(e["delta"] for e in of_type(events, "text"))
    check("con la deroga la richiesta parte senza vincoli privacy",
          text == "Risposta senza sandbox."
          and not (req.get("provider") or {}).get("zdr")
          and "data_collection" not in (req.get("provider") or {}),
          json.dumps(req.get("provider")))

    # spegnendo l'interruttore le conversazioni già in deroga non si
    # rompono: si controlla il PASSAGGIO a acceso, non il valore
    with SessionLocal() as s:
        settings_store.set_values(s, {"chat_allow_non_zdr": 0})
    r = client.patch(f"/api/chats/{conv5['id']}", headers=sauth,
                     json={"options": {"allow_non_zdr": True,
                                       "params": {"max_tokens": 100}}})
    r2 = client.patch(f"/api/chats/{conv5['id']}", headers=sauth,
                      json={"options": {"params": {"max_tokens": 200}}})
    r3 = client.patch(f"/api/chats/{conv5['id']}", headers=sauth,
                      json={"options": {"allow_non_zdr": True}})
    check("deroga già concessa: resta modificabile, ma non si ri-concede",
          r.status_code == 200 and r2.status_code == 200
          and r3.status_code == 403,
          f"{r.status_code}/{r2.status_code}/{r3.status_code}")

    # --- parametri d'esercizio della chat -----------------------------------------
    # (/api/settings sta in settings_routes, fuori da questa app ridotta: qui si
    # verifica il registro, che è la sua unica fonte di verità)
    with SessionLocal() as s:
        vals = {k: settings_store.get_int(s, k) for k in
                ("chat_max_upload_mb", "chat_turn_cost_limit_cents",
                 "chat_model_attach_mb")}
    check("parametri di esercizio della chat leggibili",
          vals == {"chat_max_upload_mb": 20,
                   "chat_turn_cost_limit_cents": 100,
                   "chat_model_attach_mb": 8}, json.dumps(vals))

    # --- anonimizzazione: proprietà della CONVERSAZIONE ------------------------
    # Stub del solo turno di anonimizzazione: qui interessa il cablaggio
    # route/eventi/history/egress; resolver e redattori hanno il test dedicato
    # chat_anonymization_test.py.
    original_turn = chat_routes.chat_anon.anonymize_turn
    turn_calls = []

    # stessa firma del vero anonymize_turn: la route passa anche ocr/capture
    def fake_turn(turn_cid, text, att_ids, cancel=None, on_progress=None,
                  capture=None, ocr=False):
        turn_calls.append((turn_cid, text, list(att_ids)))
        if on_progress:
            on_progress({"index": 1, "total": 1, "filename": "Il tuo messaggio",
                         "stage": "detect", "phase": "Analisi",
                         "phase_index": 1, "phase_total": 1,
                         "units_done": 1, "units_total": 2})
        with SessionLocal() as s:
            conv_row = s.get(chat_routes.Conversation, turn_cid)
            conv_row.mapping_version = 1
            entity = (s.query(ConversationEntity)
                      .filter_by(conv_id=turn_cid,
                                 placeholder="[FULLNAME_1]").first())
            if entity is None:
                entity = ConversationEntity(
                    conv_id=turn_cid, placeholder="[FULLNAME_1]",
                    label="FULLNAME", canonical_value="Mario Rossi",
                    mapping_version=1)
                s.add(entity)
                s.flush()
                s.add(ConversationEntityAlias(
                    entity_id=entity.id, original_surface="Mario Rossi",
                    normalized_key="FULLNAME:mario rossi", source="test",
                    confidence="exact"))
            s.commit()
        return text.replace("Mario Rossi", "[FULLNAME_1]"), 1, []

    chat_routes.chat_anon.anonymize_turn = fake_turn
    protected_conv = client.post(
        "/api/chats", json={"model": "test/plain-model", "anonymized": True},
        headers=auth).json()
    check("la chat nasce col modo scelto", protected_conv["anonymized"] is True)
    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{protected_conv['id']}/messages",
                       json={"content": "Scrivi a Mario Rossi"},
                       headers=auth) as resp:
        protected_events = sse_events(resp)
    req = sent()[n_reqs]
    full = client.get(f"/api/chats/{protected_conv['id']}", headers=auth).json()
    umsg = next(m for m in full["messages"] if m["role"] == "user")
    amsg = next(m for m in reversed(full["messages"])
                if m["role"] == "assistant")
    streamed = "".join(e["delta"] for e in protected_events
                       if e["type"] == "text")
    with SessionLocal() as s:
        canonical_assistant = next(
            m for m in reversed(s.query(chat_routes.ChatMessage)
                                .filter_by(conv_id=protected_conv["id"])
                                .order_by(chat_routes.ChatMessage.seq).all())
            if m.role == "assistant").to_openrouter()["content"]
    kinds = [e["type"] for e in protected_events]
    check("l'anonimizzazione si racconta prima del modello",
          kinds[:5] == ["turn", "anon_start", "anon_progress", "anon_done",
                        "start"]
          and protected_events[2]["phase"] == "Analisi"
          and protected_events[1]["total"] == 1,
          str(kinds[:6]))
    check("prompt anonimizzato senza che il client debba chiederlo",
          user_text(req) == "Scrivi a [FULLNAME_1]"
          and umsg["content"] == "Scrivi a Mario Rossi"
          and umsg["anonymized"] is True
          and protected_events[-1]["type"] == "done")
    check("TAG spezzato: UI decodificata, history canonica",
          streamed == "Contatta Mario Rossi."
          and amsg["content"] == "Contatta Mario Rossi."
          and len(amsg.get("entities", [])) == 1
          and canonical_assistant == "Contatta [FULLNAME_1].")
    check("system prompt dell'anonimizzazione dal primo turno",
          "segnaposto" in req["messages"][0]["content"])
    check("modo bloccato a conversazione avviata",
          full["mode_locked"] is True
          and client.patch(f"/api/chats/{protected_conv['id']}",
                           json={"anonymized": False},
                           headers=auth).status_code == 409)

    # Le categorie da anonimizzare sono per CONVERSAZIONE: il server le
    # normalizza, dichiara se sono ancora quelle dell'admin e sa tornarci.
    tagged = client.patch(f"/api/chats/{protected_conv['id']}",
                          json={"anon_options": {"excluded_tags": ["city ", "AGE"]}},
                          headers=auth)
    reread = client.get(f"/api/chats/{protected_conv['id']}", headers=auth).json()
    back = client.patch(f"/api/chats/{protected_conv['id']}",
                        json={"anon_options": {"excluded_tags": None}},
                        headers=auth)
    bad = client.patch(f"/api/chats/{protected_conv['id']}",
                       json={"anon_options": {"excluded_tags": ["!!"]}},
                       headers=auth)
    check("categorie escluse per conversazione: normalizzate e dichiarate",
          full["anon_options"]["inherited"] is True
          and tagged.json()["anon_options"] == {"excluded_tags": ["AGE", "CITY"],
                                                "custom_terms": [],
                                                "inherited": False}
          and reread["anon_options"]["excluded_tags"] == ["AGE", "CITY"]
          and back.json()["anon_options"]["inherited"] is True
          and bad.status_code == 422,
          f"{tagged.status_code}/{back.status_code}/{bad.status_code}")

    # Immagini in chat anonimizzata: SENZA lo stack OCR si rifiutano subito
    # all'upload (col motivo); con l'OCR installato si accettano `pending`
    # (la lettura avverrà all'invio, se l'utente attiva l'OCR nel popup).
    from app.engine import image_ocr
    rejected = client.post(
        f"/api/chats/{protected_conv['id']}/attachments",
        files={"file": ("foto.png", PNG, "image/png")}, headers=auth)
    if image_ocr.available():
        check("immagine accettata pending (stack OCR installato)",
              rejected.status_code == 200
              and rejected.json()["anonymization_status"] == "pending",
              rejected.text[:90])
        client.delete(f"/api/chats/{protected_conv['id']}/attachments/"
                      f"{rejected.json()['id']}", headers=auth)
    else:
        check("immagine rifiutata all'upload in chat anonimizzata",
              rejected.status_code == 415 and "OCR" in rejected.text,
              rejected.text[:90])

    # Un guasto dell'anonimizzazione non manda NIENTE a OpenRouter e non
    # persiste nessun messaggio: il turno è fatale e lo dice.
    def broken_turn(*_a, **_kw):
        raise chat_routes.chat_anon.TurnAnonymizationError(
            "Verifica dei residui fallita.")

    chat_routes.chat_anon.anonymize_turn = broken_turn
    before_broken = len(sent())
    with client.stream("POST", f"/api/chats/{protected_conv['id']}/messages",
                       json={"content": "Scrivi a Mario Rossi"},
                       headers=auth) as resp:
        broken_events = sse_events(resp)
    after = client.get(f"/api/chats/{protected_conv['id']}", headers=auth).json()
    errs = of_type(broken_events, "error")
    check("anonimizzazione fallita: errore fatale, niente inviato, niente salvato",
          errs and errs[0]["fatal"] is True and len(sent()) == before_broken
          and len(after["messages"]) == len(full["messages"]),
          errs[0]["message"][:60] if errs else "nessun evento error")
    chat_routes.chat_anon.anonymize_turn = fake_turn

    # In una chat normale l'anonimizzazione non entra mai in gioco.
    plain_conv = client.post(
        "/api/chats", json={"model": "test/plain-model"}, headers=auth).json()
    before_plain = len(turn_calls)
    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{plain_conv['id']}/messages",
                       json={"content": "Scrivi a Mario Rossi"},
                       headers=auth) as resp:
        plain_events = sse_events(resp)
    req = sent()[n_reqs]
    check("chat normale: nessuna anonimizzazione, nessun evento anon",
          plain_conv["anonymized"] is False
          and len(turn_calls) == before_plain
          and not [e for e in plain_events if e["type"].startswith("anon")]
          and user_text(req) == "Scrivi a Mario Rossi"
          and "segnaposto" not in req["messages"][0]["content"])

    # La policy admin decide quali modi sono permessi, non il singolo invio.
    with SessionLocal() as s:
        settings_store.set_chat_anonymization_policy(s, "required")
    forced = client.post("/api/chats",
                         json={"model": "test/plain-model", "anonymized": False},
                         headers=auth).json()
    blocked = client.post(
        f"/api/chats/{plain_conv['id']}/messages",
        json={"content": "ora proteggi"}, headers=auth)
    check("policy required: solo chat anonimizzate, le vecchie in chiaro si fermano",
          forced["anonymized"] is True and blocked.status_code == 409
          and "obbligatoria" in blocked.text, blocked.text[:100])
    chat_routes.chat_anon.anonymize_turn = original_turn
    with SessionLocal() as s:
        settings_store.set_chat_anonymization_policy(s, "optional")

    # --- cancellazione -----------------------------------------------------------
    r = client.delete(f"/api/chats/{cid}", headers=auth)
    gone = client.get(f"/api/chats/{cid}", headers=auth)
    files_dir = Path(os.environ["BLOCKINGBEAR_DATA_DIR"]) / "chats" / cid
    check("conversazione eliminata", r.status_code == 200
          and gone.status_code == 404 and not files_dir.is_dir())

    sandbox.shutdown()
    stop_mock()
    print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

"""Ricerca web nelle chat, da un capo
all'altro: handler dei tool con browser FINTO, gambe privacy (deanonimizza la
query, ri-anonimizza il risultato, egress check), restrizione delle call ai
tool dichiarati, flag admin + flag per conversazione, route SSE complete col
finto OpenRouter (test/web-model).

NIENTE Docker e niente modello NER: la sandbox è spenta
(BLOCKINGBEAR_SANDBOX_ENGINE=off), il browser è monkeypatchato e il rilevatore
è il FakeEngine deterministico di chat_anonymization_test.

Uso:
    python backend/tests/web_search_test.py
"""
import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import chat_mock_openrouter as mock                              # noqa: E402

DATA = HERE / "data" / "test_websearch"
shutil.rmtree(DATA, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_SANDBOX_ENGINE"] = "off"
# un'istanza "esterna" mai contattata: basta a dichiarare il browser
# disponibile senza Docker (search/read sono monkeypatchate qui sotto)
os.environ["BLOCKINGBEAR_CAMOFOX_URL"] = "http://127.0.0.1:59999"
os.environ["BLOCKINGBEAR_OPENROUTER_BASE_URL"] = mock.BASE_URL

import httpx                                                     # noqa: E402
from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import chat_anonymization as chat_anon                  # noqa: E402
from app import db, settings_store                               # noqa: E402
from app.auth import seed_admin                                  # noqa: E402
from app.db import ChatMessage, Conversation, init_db            # noqa: E402
from app.openrouter import browser                               # noqa: E402
from app.openrouter import chat as or_chat                       # noqa: E402
from app.openrouter import client as or_client                   # noqa: E402
from app.openrouter import tools as or_tools                     # noqa: E402
from app.routes import auth_routes, chat_routes                  # noqa: E402

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


def of_type(events, t):
    return [e for e in events if e["type"] == t]


def sent():
    return httpx.get(mock.DEBUG_URL).json()


# --- rilevatore deterministico (stesso schema di chat_anonymization_test) ---

class FakeEngine:
    def __init__(self, entries):
        self.entries = entries

    def analyze(self, text, excluded=None, **_kwargs):
        skip = set(excluded or ())
        entities = []
        for label, value in self.entries:
            if label in skip:
                continue
            for match in re.finditer(re.escape(value), text, re.IGNORECASE):
                entities.append({
                    "label": label, "start": match.start(), "end": match.end(),
                    "value": text[match.start():match.end()], "source": "test",
                    "validated": True, "ph": "[LOCAL_1]",
                })
        entities.sort(key=lambda e: e["start"])
        return {"entities": entities, "anonymized_text": text, "mapping": {},
                "n_entities": len(entities), "n_unique": len(entities),
                "by_label": {}}


FAKE_ENTRIES = [("PERSON", "Mario Rossi"), ("PERSON", "Luca Bianchi")]
chat_anon._base_engine = lambda: FakeEngine(FAKE_ENTRIES)

# --- browser finto ------------------------------------------------------------

CALLS = {"search": [], "read": []}
RESULTS = [
    {"title": "Mario Rossi - il profilo", "url": "https://esempio.it/MarioRossi",
     "snippet": "Tutte le novità su Mario Rossi e Luca Bianchi."},
    {"title": "Altro risultato", "url": "https://altro.example.org/pagina",
     "snippet": "Testo generico senza nomi."},
]
PAGE = {"url": "https://esempio.it/MarioRossi", "title": "Mario Rossi",
        "text": "Biografia di Mario Rossi. Collabora con Luca Bianchi da anni.",
        "total": 61}


def fake_search(query, max_results=5):
    CALLS["search"].append((query, max_results))
    return [dict(r) for r in RESULTS[:max_results]]


def fake_read(url):
    CALLS["read"].append(url)
    if "errore" in url:
        raise browser.BrowserError("pagina rotta: CAPTCHA")
    return dict(PAGE, url=url)


# la read VERA, per i test d'instradamento (A0) che girano coi fake qui sotto
REAL_READ = browser.read

browser.search = fake_search
browser.read = fake_read

SessionLocal = init_db()
with SessionLocal() as s:
    seed_admin(s)


def new_conv(anonymized=True):
    with db.SessionLocal() as s:
        conv = Conversation(owner_id=1, anonymized=1 if anonymized else 0,
                            title="t", model="test/web-model")
        s.add(conv)
        s.commit()
        return conv.id


def seed_registry(cid):
    """[FULLNAME_1] = "Mario Rossi" nel registro della conversazione."""
    with chat_anon.conversation_lock(cid), db.SessionLocal() as s:
        conv = s.get(Conversation, cid)
        chat_anon.ConversationEngine(
            FakeEngine(FAKE_ENTRIES[:1]), s, conv).analyze(
            "Dossier su Mario Rossi")
        s.commit()


def mapping_of(cid):
    with db.SessionLocal() as s:
        return chat_anon.conversation_mapping(s, cid)


def run(coro):
    return asyncio.run(coro)


def read_routing_tests():
    """A0. read(): instradamento navigate -> statico -> tab fresca
    (_read_spa, il rimedio alle SPA a guscio vuoto)."""
    page_json = json.dumps({"url": "https://sito.it/p", "title": "T",
                            "contentType": "text/html", "total": 9,
                            "text": "contenuto"})

    class FakeTab:
        nav = {"ok": True}
        plan = []           # esiti di evaluate in ordine; l'ultimo si ripete

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def navigate(self, url):
            return FakeTab.nav

        def evaluate(self, expression, timeout_ms=10000, http_timeout=60):
            r = FakeTab.plan[0]
            if len(FakeTab.plan) > 1:
                FakeTab.plan.pop(0)
            if isinstance(r, Exception):
                raise r
            return r, False

    hits = {"static": 0}

    def static_vuoto(url):
        hits["static"] += 1
        raise browser.BrowserError("La pagina non contiene testo leggibile.")

    real_spa = browser._read_spa
    saved = (browser._Tab, browser._read_static, browser._read_spa,
             browser._ensure, browser._SPA_SETTLE)
    browser._Tab = FakeTab
    browser._ensure = lambda: None
    browser._read_static = static_vuoto
    try:
        # 1) navigate ok -> estrazione nel browser, nessun ripiego
        FakeTab.nav, FakeTab.plan = {"ok": True}, [page_json]
        page = REAL_READ("https://sito.it/p")
        check("navigate ok -> estrazione diretta, niente ripieghi",
              page["text"] == "contenuto" and hits["static"] == 0)

        # 2) navigate scaduto + statico ok -> statico, tab fresca mai aperta
        spa_calls = []
        browser._read_spa = lambda url, cap: spa_calls.append(url)
        browser._read_static = lambda url: {"url": url, "title": "s",
                                            "text": "dallo statico",
                                            "total": 13}
        FakeTab.nav = None
        page = REAL_READ("https://sito.it/p")
        check("navigate scaduto -> statico, niente tab fresca",
              page["text"] == "dallo statico" and spa_calls == [])

        # 3) navigate scaduto + statico vuoto -> _read_spa
        browser._read_static = static_vuoto
        browser._read_spa = lambda url, cap: {"url": url, "title": "spa",
                                              "text": "dalla tab fresca",
                                              "total": 16}
        page = REAL_READ("https://sito.it/p")
        check("statico senza testo -> tab fresca via location.href",
              page["text"] == "dalla tab fresca" and hits["static"] >= 1)

        # 4) anche la tab fresca fallisce -> il suo errore è quello primario
        def spa_boom(url, cap):
            raise browser.BrowserError("Navigazione fallita, pagina non "
                                       "raggiungibile: x")
        browser._read_spa = spa_boom
        try:
            REAL_READ("https://sito.it/p")
            check("tab fresca fallita -> errore primario", False)
        except browser.BrowserError as e:
            check("tab fresca fallita -> errore primario",
                  "Navigazione fallita" in str(e), str(e))

        # 5) _read_spa vera: kickoff, poi estrazione unica (l'attesa vive
        #    dentro _EXTRACT_JS, qui il fake risponde subito)
        browser._SPA_SETTLE = 6
        FakeTab.plan = ["ok", page_json]
        page = real_spa("https://sito.it/p", 120000)
        check("_read_spa: kickoff e estrazione unica",
              page["text"] == "contenuto", json.dumps(page)[:80])

        # 6) _read_spa: contesto distrutto dal commit -> ritento, poi ok
        browser._SPA_SETTLE = 6
        FakeTab.plan = ["ok",
                        browser.BrowserError("Estrazione nella pagina "
                                             "fallita (js_error): Execution "
                                             "context was destroyed"),
                        page_json]
        page = real_spa("https://sito.it/p", 120000)
        check("_read_spa: contesto distrutto -> ritento sul documento nuovo",
              page["text"] == "contenuto", json.dumps(page)[:80])

        # 7) _read_spa: la pagina resta su about:blank -> navigazione fallita
        browser._SPA_SETTLE = 4
        about = json.dumps({"url": "about:blank", "title": "",
                            "contentType": "", "total": 0, "text": ""})
        FakeTab.plan = ["ok", about]
        try:
            real_spa("https://sito.it/x", 120000)
            check("_read_spa: about:blank persistente -> errore", False)
        except browser.BrowserError as e:
            check("_read_spa: about:blank persistente -> errore",
                  "non raggiungibile" in str(e), str(e))

        # 8) _read_spa: testo vuoto -> pagina vuota resa al chiamante
        #    (è read() a decidere l'errore)
        browser._SPA_SETTLE = 4
        empty = json.dumps({"url": "https://sito.it/v", "title": "",
                            "contentType": "", "total": 0, "text": ""})
        FakeTab.plan = ["ok", empty]
        page = real_spa("https://sito.it/v", 120000)
        check("_read_spa: pagina vuota resa al chiamante",
              page["text"] == "", json.dumps(page)[:80])

        # 9) navigate vera: timeout httpx lato client -> None (tab avvelenata
        #    come col 500 del server: read() scende su statico e tab fresca;
        #    senza questo, un errore salterebbe i ripieghi)
        real_http = browser._http

        def http_boom(*a, **k):
            raise browser.httpx.TimeoutException("timed out")
        browser._http = http_boom
        try:
            t = object.__new__(saved[0])
            t.tab_id = "t1"
            check("navigate: timeout client -> None, ripieghi di read()",
                  t.navigate("https://sito.it/lenta") is None)
        finally:
            browser._http = real_http
    finally:
        (browser._Tab, browser._read_static, browser._read_spa,
         browser._ensure, browser._SPA_SETTLE) = saved


def main():
    stop_mock = mock.serve()

    read_routing_tests()

    # =========================================================================
    # A. handler dei tool, diretti (browser finto, registro vero)
    # =========================================================================
    ws = or_tools.get("web_search")
    rp = or_tools.get("read_page")
    check("registry con i tool web",
          or_tools.names() == ["execute_python", "web_search", "read_page",
                               "read_document_images"]
          and ws.ui_kind == "search" and rp.ui_kind == "page")
    check("available_specs(web=False) esclude i tool web",
          [s.name for s in or_tools.available_specs(web=False)]
          == [s.name for s in or_tools.available_specs()
              if s.name not in or_tools.WEB_TOOL_NAMES])

    cid = new_conv()
    seed_registry(cid)
    m = mapping_of(cid)
    check("registro pronto", m.get("[FULLNAME_1]") == "Mario Rossi", str(m))

    # --- gamba di uscita: query deanonimizzata ------------------------------
    ctx = {"anonymized": True, "scope": cid}
    res = run(ws.handler(cid, {"query": "novità su [FULLNAME_1]",
                               "max_results": 2}, ctx))
    check("query deanonimizzata verso il motore",
          CALLS["search"][-1] == ("novità su Mario Rossi", 2),
          str(CALLS["search"][-1]))
    dumped = json.dumps(res, ensure_ascii=False)
    check("risultati senza valori reali",
          res["outcome"] == "ok" and "Mario Rossi" not in dumped
          and "Luca Bianchi" not in dumped, dumped[:200])
    check("valore noto -> stesso TAG (anche nell'URL, forma attaccata)",
          "[FULLNAME_1]" in res["results"][0]["title"]
          and res["results"][0]["url"] == "https://esempio.it/[FULLNAME_1]",
          json.dumps(res["results"][0], ensure_ascii=False))
    m = mapping_of(cid)
    new_tag = next((ph for ph, v in m.items() if v == "Luca Bianchi"), None)
    check("entità nuova dal web -> TAG nuovo additivo",
          new_tag is not None and new_tag != "[FULLNAME_1]"
          and new_tag in dumped, str(new_tag))

    # --- tag allucinato: errore, la ricerca NON parte ------------------------
    n_search = len(CALLS["search"])
    res = run(ws.handler(cid, {"query": "info su [FULLNAME_99]"}, ctx))
    check("tag allucinato -> tool result d'errore, niente uscita",
          res["outcome"] == "error" and "[FULLNAME_99]" in res["stderr"]
          and len(CALLS["search"]) == n_search, res["stderr"][:100])

    # --- read_page: URL deanonimizzato, contenuto ri-anonimizzato ------------
    res = run(rp.handler(cid, {"url": "https://esempio.it/[FULLNAME_1]"}, ctx))
    check("URL deanonimizzato verso il browser",
          CALLS["read"][-1] == "https://esempio.it/MarioRossi",
          CALLS["read"][-1])
    dumped = json.dumps(res, ensure_ascii=False)
    check("pagina ri-anonimizzata (pipeline di ingresso)",
          res["outcome"] == "ok" and "Mario Rossi" not in dumped
          and "Luca Bianchi" not in dumped
          and "[FULLNAME_1]" in res["content"] and new_tag in res["content"],
          dumped[:200])
    with db.SessionLocal() as s:
        leaks = chat_anon.known_surface_leaks(s, cid, res["content"])
    check("controllo di uscita pulito sul contenuto", leaks == [], str(leaks))

    # --- variante dedotta + URL col nome attaccato (regressione) -------------
    # Registro: [ORG_1] = "Acme Analytics Srl" (variante dedotta "acme
    # analytics") e [URL_1] = "https://www.acmeanalytics.com". La pagina nomina
    # "Acme Analytics" (che il NER finto NON riconosce) e contiene l'URL.
    # `for_text` sul testo ORIGINALE lasciava la variante in chiaro (nel testo
    # c'è "acmeanalytics" attaccato), ma il controllo di uscita legge il testo
    # COPERTO, dove l'URL è già [URL_1]: trovava "Acme Analytics" e scartava
    # una pagina che avrebbe dovuto solo coprire. Ora `for_text` giudica il
    # rischio con le superfici viste già coperte: la variante si copre anche
    # lei e il risultato passa.
    acme_cid = new_conv()
    with chat_anon.conversation_lock(acme_cid), db.SessionLocal() as s:
        conv = s.get(Conversation, acme_cid)
        chat_anon.ConversationEngine(
            FakeEngine([("ORG", "Acme Analytics Srl"),
                        ("URL", "https://www.acmeanalytics.com")]), s,
            conv).analyze("Acme Analytics Srl - https://www.acmeanalytics.com")
        s.commit()
    acme_ctx = {"anonymized": True, "scope": acme_cid}
    real_page = dict(PAGE)
    PAGE.update({"title": "Acme Analytics", "url": "https://www.acmeanalytics.com/",
                 "text": "Acme Analytics, leader del settore. Sito: "
                         "https://www.acmeanalytics.com - ragione sociale "
                         "Acme Analytics Srl."})
    try:
        res = run(rp.handler(acme_cid, {"url": "https://www.acmeanalytics.com/"},
                             acme_ctx))
    finally:
        PAGE.clear(); PAGE.update(real_page)
    dumped = json.dumps(res, ensure_ascii=False)
    check("variante dedotta coperta dopo che l'URL è diventato [URL_1]",
          res["outcome"] == "ok" and "Acme Analytics" not in dumped
          and "acmeanalytics" not in dumped.lower()
          and "[ORG_1]" in res["title"] and "[ORG_1]" in res["content"]
          and "[URL_1]" in res["content"], dumped[:300])
    # forma attaccata in un punto SCONOSCIUTO al registro (handle social): la
    # regola prudenziale resta — la variante non si copre — ma il controllo
    # di uscita deve restare coerente e NON scartare la pagina
    PAGE.update({"title": "Acme Analytics", "url": "https://social.example/x",
                 "text": "Seguici: @acmeanalytics. Acme Analytics dal 1999."})
    try:
        res = run(rp.handler(acme_cid, {"url": "https://social.example/x"},
                             acme_ctx))
    finally:
        PAGE.clear(); PAGE.update(real_page)
    check("forma attaccata sconosciuta: variante in chiaro ma pagina NON scartata",
          res["outcome"] == "ok" and "@acmeanalytics" in res["content"],
          json.dumps(res, ensure_ascii=False)[:300])

    # --- egress check che scatta (cablaggio della difesa) --------------------
    real_leaks = chat_anon.known_surface_leaks
    chat_anon.known_surface_leaks = lambda *a, **k: [("[FULLNAME_1]",
                                                      "Mario Rossi")]
    try:
        res = run(rp.handler(cid, {"url": "https://esempio.it/x"}, ctx))
    finally:
        chat_anon.known_surface_leaks = real_leaks
    check("egress sporco -> risultato scartato con errore",
          res["outcome"] == "error" and "scartato" in res["stderr"],
          res["stderr"][:100])

    # --- errori del browser: tool result parlante -----------------------------
    res = run(rp.handler(cid, {"url": "https://x.it/errore"}, ctx))
    check("guasto del browser -> errore parlante",
          res["outcome"] == "error" and "CAPTCHA" in res["stderr"],
          res["stderr"])

    # --- chat NON anonimizzata: le gambe sono no-op ---------------------------
    plain_ctx = {"anonymized": False, "scope": cid}
    res = run(ws.handler(cid, {"query": "chi è Mario Rossi",
                               "max_results": 99}, plain_ctx))
    check("chat normale: query com'è, tetto sui risultati",
          CALLS["search"][-1] == ("chi è Mario Rossi", 10)
          and "Mario Rossi" in json.dumps(res, ensure_ascii=False))

    # --- troncamento della pagina --------------------------------------------
    real_cap = or_tools.WEB_PAGE_MAX_CHARS
    or_tools.WEB_PAGE_MAX_CHARS = 20
    try:
        res = run(rp.handler(cid, {"url": "https://esempio.it/lunga"},
                             plain_ctx))
    finally:
        or_tools.WEB_PAGE_MAX_CHARS = real_cap
    check("pagina troncata con nota (stile kernel)",
          res["truncated"] is True
          and "…[pagina troncata: 61 caratteri totali]…" in res["content"]
          and res["content"].startswith(PAGE["text"][:20]),
          res["content"][:80])

    # --- solo i tool dichiarati nel turno sono eseguibili ---------------------
    call = {"id": "c1", "function": {"name": "web_search",
                                     "arguments": '{"query": "x"}'}}
    spec, args, err = or_chat._parse_call(call, {"execute_python"})
    check("call verso tool non dichiarato -> errore",
          spec is None and err["outcome"] == "error"
          and err["stderr"] == "Tool sconosciuto: web_search. "
                               "È disponibile solo execute_python.",
          repr(err["stderr"]))
    spec, args, err = or_chat._parse_call(call, set())
    check("nessun tool dichiarato -> errore dedicato",
          "Nessun tool è disponibile" in err["stderr"], repr(err["stderr"]))
    spec, args, err = or_chat._parse_call(call, {"web_search", "read_page"})
    check("call dichiarata -> spec risolta",
          err is None and spec is ws and args == {"query": "x"})

    # --- system prompt coi blocchi web ----------------------------------------
    prompt = or_chat.system_prompt(
        tool_names=["execute_python", "web_search", "read_page"],
        anonymized=True)
    check("prompt: blocco web presente, una volta sola, niente vuoti",
          prompt.count("web_search e read_page") == 1
          and "segnaposto [ETICHETTA_n] direttamente nelle query" in prompt
          and "\n\n\n" not in prompt)
    prompt = or_chat.system_prompt(tool_names=["web_search", "read_page"],
                                   anonymized=False)
    check("prompt senza sandbox: solo regole web",
          "execute_python, un ambiente Python" not in prompt
          and "web_search e read_page" in prompt
          and "direttamente nelle query" not in prompt)

    # =========================================================================
    # B. route complete (finto OpenRouter, test/web-model)
    # =========================================================================
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(chat_routes.router)
    client = TestClient(app)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    mock.grant_key(SessionLocal)

    r = client.get("/api/openrouter/status", headers=auth).json()
    check("flag admin acceso di default: web_search True nello status",
          r["web_search"] is True and r["browser"]["available"] is True
          and r["browser"]["external"] is True, json.dumps(r["browser"]))

    with db.SessionLocal() as s:
        settings_store.set_values(s, {"chat_web_search": 0})
    r = client.get("/api/openrouter/status", headers=auth).json()
    check("flag admin spento: web_search False", r["web_search"] is False)

    with db.SessionLocal() as s:
        settings_store.set_values(s, {"chat_web_search": 1})

    # --- turno anonimizzato completo: IL test che conta ---------------------
    conv = client.post("/api/chats", json={
        "model": "test/web-model", "anonymized": True},
        headers=auth).json()
    wid = conv["id"]
    n_reqs = len(sent())
    CALLS["search"].clear()
    CALLS["read"].clear()
    with client.stream("POST", f"/api/chats/{wid}/messages",
                       json={"content": "Cerca le novità su Mario Rossi"},
                       headers=auth) as resp:
        events = sse_events(resp)

    calls = of_type(events, "tool_call")
    results = of_type(events, "tool_result")
    check("due tool call (search poi page), col kind",
          [c.get("kind") for c in calls] == ["search", "page"]
          and [r.get("kind") for r in results] == ["search", "page"],
          str([c.get("kind") for c in calls]))
    check("la query REALE partita è visibile all'utente",
          calls[0]["display_args"]["query"] == "ultime notizie su Mario Rossi"
          and calls[0]["args"]["query"] == "ultime notizie su [FULLNAME_1]",
          json.dumps(calls[0].get("display_args")))
    check("il motore ha ricevuto la query in chiaro",
          CALLS["search"] and CALLS["search"][-1][0]
          == "ultime notizie su Mario Rossi", str(CALLS["search"]))
    check("tool result decodificato per il display",
          "Mario Rossi" in results[0]["result"]["results"][0]["title"]
          and results[0]["result"]["results"][0]["url"]
          == "https://esempio.it/MarioRossi",
          json.dumps(results[0]["result"]["results"][0], ensure_ascii=False))
    check("read_page sull'URL vero (round-trip del TAG)",
          CALLS["read"] and CALLS["read"][-1]
          == "https://esempio.it/MarioRossi", str(CALLS["read"]))
    check("contenuto pagina decodificato per il display",
          "Mario Rossi" in results[1]["result"]["content"]
          and "Luca Bianchi" in results[1]["result"]["content"],
          results[1]["result"]["content"][:80])
    text = "".join(e["delta"] for e in of_type(events, "text"))
    check("risposta finale decodificata (TAG nato prima del turno)",
          text == "Dal web: novità su Mario Rossi.", repr(text))

    # ciò che è PARTITO verso OpenRouter: solo TAG, mai valori reali
    reqs = sent()[n_reqs:]
    payload = json.dumps([r["messages"] for r in reqs], ensure_ascii=False)
    check("nessun valore reale verso il provider (messaggi di 3 richieste)",
          "Mario Rossi" not in payload and "Luca Bianchi" not in payload
          and "MarioRossi" not in payload, "")
    tool_msgs = [m for r in reqs for m in r["messages"]
                 if m.get("role") == "tool"]
    check("tool result al modello coi TAG",
          tool_msgs and "[FULLNAME_1]" in tool_msgs[0]["content"]
          and "https://esempio.it/[FULLNAME_1]" in tool_msgs[0]["content"],
          tool_msgs[0]["content"][:120] if tool_msgs else "")
    check("tools dichiarati in ogni richiesta del turno",
          all({t["function"]["name"] for t in r.get("tools", [])}
              == {"web_search", "read_page", "read_document_images"}
              for r in reqs),
          str([len(r.get("tools", [])) for r in reqs]))

    # persistenza: forma canonica coi tag + display decodificato al reload
    full = client.get(f"/api/chats/{wid}", headers=auth).json()
    tool_rows = [m for m in full["messages"] if m["role"] == "tool"]
    check("messaggi tool persistiti canonici (TAG, niente valori)",
          tool_rows and "[FULLNAME_1]" in tool_rows[0]["content"]
          and "Mario Rossi" not in tool_rows[0]["content"], "")
    assist = [m for m in full["messages"]
              if m["role"] == "assistant" and m.get("tool_calls")]
    check("display_args decodificati anche al ricaricamento",
          assist and assist[0]["tool_calls"][0]["display_args"]["query"]
          == "ultime notizie su Mario Rossi",
          json.dumps(assist[0]["tool_calls"][0].get("display_args"))
          if assist else "")
    final = [m for m in full["messages"] if m["role"] == "assistant"][-1]
    check("testo finale decodificato al reload (versione di fine turno)",
          final["content"] == "Dal web: novità su Mario Rossi.",
          repr(final["content"]))

    # --- interruttore della conversazione: web spento -> tool non dichiarati -
    r = client.patch(f"/api/chats/{wid}",
                     json={"options": {"web_search": False}}, headers=auth)
    check("opzione per conversazione salvata",
          r.status_code == 200, r.text[:80])
    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{wid}/messages",
                       json={"content": "Ciao, come va?"},
                       headers=auth) as resp:
        events = sse_events(resp)
    reqs = sent()[n_reqs:]
    # (read_document_images non è un tool web e resta dichiarato: il flag
    # web toglie SOLO web_search e read_page)
    check("web spento nella chat: niente tool web dichiarati",
          reqs and all({t["function"]["name"] for t in r.get("tools", [])}
                       == {"read_document_images"} for r in reqs)
          and not of_type(events, "tool_call"),
          str([[t["function"]["name"] for t in r.get("tools", [])]
               for r in reqs]))

    # --- call verso un tool NON dichiarato: rifiutata, mai eseguita ----------
    n_search = len(CALLS["search"])
    with client.stream("POST", f"/api/chats/{wid}/messages",
                       json={"content": "FORZATOOL cerca lo stesso"},
                       headers=auth) as resp:
        events = sse_events(resp)
    results = of_type(events, "tool_result")
    check("tool non dichiarato -> tool result d'errore, handler mai eseguito",
          results and results[0]["result"]["outcome"] == "error"
          and "Tool sconosciuto: web_search" in results[0]["result"]["stderr"]
          and len(CALLS["search"]) == n_search,
          results[0]["result"]["stderr"][:100] if results else "")

    # --- flag admin spento: vince su tutto ------------------------------------
    with db.SessionLocal() as s:
        settings_store.set_values(s, {"chat_web_search": 0})
    conv2 = client.post("/api/chats", json={
        "model": "test/web-model", "anonymized": False},
        headers=auth).json()
    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{conv2['id']}/messages",
                       json={"content": "Cerca qualcosa"},
                       headers=auth) as resp:
        sse_events(resp)
    reqs = sent()[n_reqs:]
    check("flag admin spento: niente tool web qualunque sia la chat",
          reqs and all({t["function"]["name"] for t in r.get("tools", [])}
                       == {"read_document_images"} for r in reqs), "")

    # --- chat normale col web accesso: passthrough senza gambe privacy --------
    with db.SessionLocal() as s:
        settings_store.set_values(s, {"chat_web_search": 1})
    conv3 = client.post("/api/chats", json={
        "model": "test/web-model", "anonymized": False},
        headers=auth).json()
    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{conv3['id']}/messages",
                       json={"content": "Fai una ricerca qualsiasi"},
                       headers=auth) as resp:
        events = sse_events(resp)
    reqs = sent()[n_reqs:]
    tool_msgs = [m for r in reqs for m in r["messages"]
                 if m.get("role") == "tool"]
    check("chat normale: risultati al modello coi valori reali",
          tool_msgs and "Mario Rossi" in tool_msgs[0]["content"],
          tool_msgs[0]["content"][:100] if tool_msgs else "")
    calls = of_type(events, "tool_call")
    check("chat normale: niente display_args (non serve decodifica)",
          calls and "display_args" not in calls[0], str(calls[0].keys()))

    stop_mock()
    print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

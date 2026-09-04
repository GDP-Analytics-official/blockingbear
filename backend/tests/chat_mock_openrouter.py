"""Finto OpenRouter per collaudare la chat SENZA chiave e senza costi.

Riproduce le particolarità vere del protocollo (verificate sulle docs):
commenti SSE keep-alive, tool call spezzate in frammenti da accumulare per
index, reasoning_details a frammenti (stesso id) con signature sull'ultimo,
usage solo nel chunk finale, errori a metà stream con HTTP 200 + campo
"error", errori pre-stream con status JSON.

Modelli finti:
  test/happy-model   2 tool call (6*7 e scrittura file) poi risposta finale;
                     dichiara tools, reasoning, immagini e file in ingresso
  test/plain-model   senza tool: la chat lo usa in modalità pura
  test/error-model   un po' di testo poi errore mid-stream
  test/loop-model    chiama execute_python all'infinito (test del tetto di
                     iterazioni); rispetta tool_choice=none
  test/parallel-model  DUE tool call nello stesso messaggio (frammenti
                     interlacciati per index) poi risposta finale; varianti
                     con parole chiave nel messaggio utente (SINGOLO,
                     ARGROTTI, TOOLIGNOTO, MISTO) per il batch parallelo
  test/unauthorized  401 pre-stream
  test/nofile-model  dichiara "file" ma con provider.zdr rifiuta le parti
                     `file` con 404 "No endpoints found that support file
                     input" (come x-ai/grok-4.6 dal vero); senza parti file
                     risponde come testo puro

GET /debug/requests restituisce i body ricevuti: è così che i test
verificano cosa è stato spedito davvero (echo del reasoning, tools in ogni
richiesta, allegati multimodali, parametri...).

Uso: `serve()` lo avvia in un thread (i test lo fanno da soli), oppure
     python -m uvicorn chat_mock_openrouter:app --port 8765
"""
import json
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
REQUESTS = []
META_ONLY_KEY = "sk-or-test-metadata-only"

PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}/api/v1"
DEBUG_URL = f"http://127.0.0.1:{PORT}/debug/requests"


def chunk(model, delta=None, finish=None, usage=None, error=None):
    obj = {"id": "gen-test", "object": "chat.completion.chunk", "created": 1,
           "model": model,
           "choices": [{"index": 0, "delta": delta or {},
                        "finish_reason": finish}]}
    if usage:
        obj["usage"] = usage
    if error:
        obj["error"] = error
    return "data: " + json.dumps(obj) + "\n\n"


def usage(cost, prompt=100, completion=40):
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion, "cost": cost,
            "prompt_tokens_details": {"cached_tokens": 80}}


@app.get("/debug/requests")
def debug_requests():
    return REQUESTS


_MODELS = [
    {"id": "test/happy-model", "name": "Happy Model",
     "description": "Modello finto con tool calling.",
     "context_length": 128000,
     # "file" = PDF nativi, "image" = visione: la chat allega davvero i byte
     "architecture": {"input_modalities": ["text", "image", "file"],
                      "output_modalities": ["text"]},
     "pricing": {"prompt": "0.000001", "completion": "0.000002"},
     "top_provider": {"max_completion_tokens": 16000, "is_moderated": False},
     "supported_parameters": ["tools", "tool_choice", "reasoning",
                              "max_tokens", "temperature", "seed"],
     "reasoning": {"mandatory": False, "default_enabled": False,
                   "supported_efforts": ["high", "medium", "low"],
                   "default_effort": "medium", "supports_max_tokens": True},
     # punteggi Artificial Analysis: solo nel catalogo PUBBLICO, come dal vero
     "benchmarks": {"design_arena": [],
                    "artificial_analysis": {"intelligence_index": 60.0,
                                            "coding_index": 70.0,
                                            "agentic_index": 50.0}}},
    {"id": "test/plain-model", "name": "Plain Model",
     "description": "Senza tool: la chat lo usa in modalità pura.",
     "context_length": 8000,
     "architecture": {"input_modalities": ["text"],
                      "output_modalities": ["text"]},
     "pricing": {"prompt": "0", "completion": "0"},
     "top_provider": {"max_completion_tokens": 4000, "is_moderated": True},
     "supported_parameters": ["max_tokens"]},
    {"id": "test/happy-model:free", "name": "Happy Model (free)",
     "description": "Variante free.",
     "context_length": 16000,
     "architecture": {"input_modalities": ["text"],
                      "output_modalities": ["text"]},
     "pricing": {"prompt": "0", "completion": "0"},
     "top_provider": {}, "supported_parameters": ["tools"]},
    {"id": "test/web-model", "name": "Web Model",
     "description": "Modello finto che usa web_search e read_page.",
     "context_length": 32000,
     "architecture": {"input_modalities": ["text"],
                      "output_modalities": ["text"]},
     "pricing": {"prompt": "0.000001", "completion": "0.000002"},
     "top_provider": {"max_completion_tokens": 8000, "is_moderated": False},
     "supported_parameters": ["tools", "tool_choice", "max_tokens"],
     # più bravo di happy-model a programmare, ma senza visione
     "benchmarks": {"design_arena": [],
                    "artificial_analysis": {"intelligence_index": 40.0,
                                            "coding_index": 80.0,
                                            "agentic_index": 20.0}}},
    {"id": "test/nozdr-model", "name": "No ZDR Model",
     "description": "Nessun provider con Zero Data Retention: con le regole "
                    "privacy attive risponde 503.",
     "context_length": 8000,
     "architecture": {"input_modalities": ["text"],
                      "output_modalities": ["text"]},
     "pricing": {"prompt": "0", "completion": "0"},
     "top_provider": {}, "supported_parameters": ["max_tokens"]},
    {"id": "test/nofile-model", "name": "No File Input Model",
     "description": "Il catalogo dichiara i PDF nativi, l'endpoint ZDR li "
                    "rifiuta.",
     "context_length": 8000,
     "architecture": {"input_modalities": ["text", "image", "file"],
                      "output_modalities": ["text"]},
     "pricing": {"prompt": "0", "completion": "0"},
     "top_provider": {}, "supported_parameters": ["max_tokens", "tools"]},
    {"id": "test/error-model", "name": "Streaming Error Model",
     "description": "Emits a partial answer followed by a provider error.",
     "context_length": 8000,
     "architecture": {"input_modalities": ["text"],
                      "output_modalities": ["text"]},
     "pricing": {"prompt": "0", "completion": "0"},
     "top_provider": {}, "supported_parameters": ["max_tokens"]},
]

# GET /endpoints/zdr: gli endpoint con Zero Data Retention. test/nozdr-model
# NON c'è, ed è il modello su cui si collauda l'avviso privacy e la deroga.
_ZDR_ENDPOINTS = [
    {"name": "Finto | test/happy-model", "model_id": "test/happy-model",
     "model_name": "Happy Model", "provider_name": "FintoProvider",
     "supports_implicit_caching": True},
    {"name": "Altro | test/happy-model", "model_id": "test/happy-model",
     "model_name": "Happy Model", "provider_name": "AltroFinto",
     "supports_implicit_caching": False},
    {"name": "Finto | test/plain-model", "model_id": "test/plain-model",
     "model_name": "Plain Model", "provider_name": "FintoProvider",
     "supports_implicit_caching": False},
    {"name": "Finto | test/error-model", "model_id": "test/error-model",
     "model_name": "Streaming Error Model", "provider_name": "FintoProvider",
     "supports_implicit_caching": False},
]


@app.get("/api/v1/endpoints/zdr")
def zdr_endpoints():
    return {"data": _ZDR_ENDPOINTS}


@app.get("/api/v1/models")
def models():
    return {"data": _MODELS}


@app.get("/api/v1/models/user")
def models_user(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer sk-or-"):
        return JSONResponse(status_code=401, content={
            "error": {"code": 401, "message": "Chiave API non valida"}})
    # come OpenRouter: la lista dell'account non porta i benchmark
    return {"data": [{k: v for k, v in m.items() if k != "benchmarks"}
                     for m in _MODELS]}


@app.get("/api/v1/key")
def key_info(request: Request):
    auth = request.headers.get("authorization", "")
    if auth == f"Bearer {MGMT_KEY}":
        return {"data": {"label": "management-test",
                         "is_management_key": True,
                         "is_provisioning_key": True}}
    if not auth.startswith("Bearer sk-or-"):
        return JSONResponse(status_code=401, content={
            "error": {"code": 401, "message": "Chiave API non valida"}})
    return {"data": {"label": "test", "limit": 100.0,
                     "limit_remaining": 92.5, "usage": 7.5,
                     "usage_monthly": 7.5, "is_free_tier": False,
                     "is_management_key": False,
                     "is_provisioning_key": False}}


# --- Management API (chiavi per-utente, crediti, analytics) ------------------
# Bearer dedicato: la management key NON è una chiave di inferenza (e il
# backend non deve mai confonderle). Stato in RAM, azzerabile dai test.
MGMT_KEY = "mgmt-test-provisioning"
KEYS = {}                     # hash -> record chiave
_KEY_SEQ = [0]


def _mgmt_guard(request):
    if request.headers.get("authorization") != f"Bearer {MGMT_KEY}":
        return JSONResponse(status_code=401, content={
            "error": {"code": 401, "message": "Management key non valida"}})
    return None


@app.get("/api/v1/keys")
def keys_list(request: Request, offset: int = 0,
              include_disabled: bool = False):
    if (err := _mgmt_guard(request)):
        return err
    rows = [k for k in KEYS.values()
            if include_disabled or not k["disabled"]]
    return {"data": rows[offset:offset + 100]}


@app.post("/api/v1/keys")
async def keys_create(request: Request):
    if (err := _mgmt_guard(request)):
        return err
    body = await request.json()
    _KEY_SEQ[0] += 1
    n = _KEY_SEQ[0]
    key_hash = f"hash-{n:04d}"
    record = {"hash": key_hash, "name": body["name"], "disabled": False,
              "limit": body.get("limit"),
              "limit_reset": body.get("limit_reset"),
              "limit_remaining": body.get("limit"),
              "usage": 0.0, "usage_daily": 0.0, "usage_weekly": 0.0,
              "usage_monthly": 0.0,
              "created_at": "2026-08-11T00:00:00Z"}
    KEYS[key_hash] = record
    return JSONResponse(status_code=201, content={
        "key": f"sk-or-test-{n:04d}", "data": record})


@app.get("/api/v1/keys/{key_hash}")
def keys_get(key_hash: str, request: Request):
    if (err := _mgmt_guard(request)):
        return err
    if key_hash not in KEYS:
        return JSONResponse(status_code=404, content={
            "error": {"code": 404, "message": "Chiave inesistente"}})
    return {"data": KEYS[key_hash]}


@app.patch("/api/v1/keys/{key_hash}")
async def keys_patch(key_hash: str, request: Request):
    if (err := _mgmt_guard(request)):
        return err
    if key_hash not in KEYS:
        return JSONResponse(status_code=404, content={
            "error": {"code": 404, "message": "Chiave inesistente"}})
    body = await request.json()
    KEYS[key_hash].update({k: v for k, v in body.items() if k in (
        "name", "disabled", "limit", "limit_reset", "include_byok_in_limit")})
    return {"data": KEYS[key_hash]}


@app.delete("/api/v1/keys/{key_hash}")
def keys_delete(key_hash: str, request: Request):
    if (err := _mgmt_guard(request)):
        return err
    if key_hash not in KEYS:
        return JSONResponse(status_code=404, content={
            "error": {"code": 404, "message": "Chiave inesistente"}})
    del KEYS[key_hash]
    return {"data": {"deleted": True}}


@app.get("/api/v1/credits")
def credits(request: Request):
    if (err := _mgmt_guard(request)):
        return err
    return {"data": {"total_credits": 120.0, "total_usage": 34.5}}


@app.post("/api/v1/analytics/query")
async def analytics_query(request: Request):
    if (err := _mgmt_guard(request)):
        return err
    body = await request.json()
    REQUESTS.append({"analytics": body})
    dims = body.get("dimensions") or []
    rows = []
    if body.get("granularity") == "day" and "api_key_id" in dims:
        for name in [k["name"] for k in KEYS.values()][:2] or ["blockingbear-admin"]:
            rows += [{"date__day": "2026-08-10", "api_key_id": name,
                      "total_usage": 1.25, "request_count": "10",
                      "tokens_total": "5000"},
                     {"date__day": "2026-08-11", "api_key_id": name,
                      "total_usage": 0.75, "request_count": "4",
                      "tokens_total": "2100"}]
    elif "model" in dims:
        rows = [{"model": "test/happy-model", "total_usage": 3.5,
                 "request_count": "20"},
                {"model": "test/plain-model", "total_usage": 0.5,
                 "request_count": "8"}]
    else:
        rows = [{"total_usage": 4.0, "request_count": "28",
                 "tokens_total": "14200"}]
    return {"data": {"data": rows,
                     "metadata": {"row_count": len(rows), "truncated": False}}}


@app.get("/api/v1/analytics/meta")
def analytics_meta(request: Request):
    if (err := _mgmt_guard(request)):
        return err
    return {"data": {"metrics": ["total_usage", "request_count"],
                     "dimensions": ["model", "api_key_id"],
                     "granularities": ["day", "week", "month"]}}


@app.post("/api/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    REQUESTS.append(body)
    model = body["model"]
    n_tool = sum(1 for m in body["messages"] if m.get("role") == "tool")

    auth = request.headers.get("authorization", "")
    if auth == f"Bearer {META_ONLY_KEY}" or not auth.startswith(
            "Bearer sk-or-test-"):
        return JSONResponse(status_code=401, content={
            "error": {"code": 401,
                      "message": "Key is valid for metadata but not inference"}})

    if model == "test/unauthorized":
        return JSONResponse(status_code=401, content={
            "error": {"code": 401, "message": "Chiave API non valida"}})

    prefs = body.get("provider") or {}
    if model == "test/nozdr-model" and (
            prefs.get("zdr") or prefs.get("data_collection") == "deny"):
        # come OpenRouter quando nessun endpoint soddisfa le regole privacy
        return JSONResponse(status_code=503, content={
            "error": {"code": 503,
                      "message": "No endpoints found matching your data policy"}})

    if model == "test/nofile-model" and prefs.get("zdr") and any(
            isinstance(m.get("content"), list)
            and any((p or {}).get("type") == "file" for p in m["content"])
            for m in body["messages"]):
        # come OpenRouter quando l'endpoint ZDR non accetta i file nativi
        return JSONResponse(status_code=404, content={
            "error": {"code": 404,
                      "message": "No endpoints found that support file input"}})

    if body.get("stream") is False:
        return {"id": "completion-key-check", "model": model,
                "choices": [{"index": 0, "finish_reason": "length",
                             "message": {"role": "assistant",
                                         "content": "OK"}}],
                "usage": usage(0.0, prompt=4, completion=1)}

    has_tools = bool(body.get("tools"))
    # il web-model segue il suo script SOLO se web_search è dichiarato: un
    # modello vero non chiama un tool che non vede (con altri tool dichiarati
    # ma niente web, es. read_document_images, risponde come testo puro)
    if model == "test/web-model":
        has_tools = any((t.get("function") or {}).get("name") == "web_search"
                        for t in body.get("tools") or [])
    # "FORZATOOL" nel messaggio: il web-model chiama web_search ANCHE senza
    # tool dichiarati (collauda il rifiuto delle call verso tool non
    # dichiarati nel turno)
    _last_user = next((m for m in reversed(body["messages"])
                       if m.get("role") == "user"), {})
    _last_text = _last_user.get("content") or ""
    if isinstance(_last_text, list):
        _last_text = "".join(p.get("text", "") for p in _last_text
                             if p.get("type") == "text")
    force_tool = model == "test/web-model" and "FORZATOOL" in _last_text

    def gen():
        yield ": OPENROUTER PROCESSING\n\n"

        if not has_tools and not force_tool and model not in (
                "test/error-model", "test/unauthorized"):
            # richiesta senza tool dichiarati: risposta puramente testuale
            last_user = next((m for m in reversed(body["messages"])
                              if m.get("role") == "user"), {})
            user_content = last_user.get("content") or ""
            if isinstance(user_content, list):
                user_content = "".join(p.get("text", "") for p in user_content
                                       if p.get("type") == "text")
            if "[FULLNAME_1]" in user_content:
                # Placeholder intenzionalmente spezzato tra chunk SSE.
                yield chunk(model, {"content": "Contatta [FULL"})
                yield chunk(model, {"content": "NAME_1]."})
            elif "LENTISSIMO" in user_content:
                # ~8 secondi di streaming: la finestra dei test UI, dove in
                # mezzo c'è un refresh vero del browser
                for i in range(10):
                    yield chunk(model, {"content": f"pezzo{i} "})
                    time.sleep(0.8)
                yield chunk(model, {"content": "fine."})
            elif "LENTO" in user_content:
                # risposta a rate umano: serve ai test del riaggancio (il
                # client si stacca a metà e il turno deve continuare)
                for i in range(6):
                    yield chunk(model, {"content": f"pezzo{i} "})
                    time.sleep(0.4)
                yield chunk(model, {"content": "fine."})
            else:
                yield chunk(model, {"content": "Risposta senza sandbox."})
            yield chunk(model, finish="stop", usage=usage(0.001))
        elif model == "test/error-model":
            yield chunk(model, {"content": "Inizio a rispond"})
            yield chunk(model, {"content": ""}, finish="error",
                        error={"code": "server_error",
                               "message": "Provider disconnected unexpectedly"})

        elif model == "test/web-model":
            # Script deterministico: ricerca -> lettura del primo risultato ->
            # risposta finale. La query usa il TAG che il modello VEDE nel
            # messaggio utente (è così che si collauda la gamba di uscita);
            # l'URL di read_page è quello del tool result (com'è arrivato:
            # nelle chat anonimizzate è già ri-anonimizzato).
            users = [m for m in body["messages"] if m.get("role") == "user"]
            utext = users[-1].get("content") or "" if users else ""
            if isinstance(utext, list):
                utext = "".join(p.get("text", "") for p in utext
                                if p.get("type") == "text")
            # i tool message del SOLO turno in corso (dopo l'ultimo user):
            # n_tool globale conterebbe anche la storia dei turni precedenti
            last_user_at = max((i for i, m in enumerate(body["messages"])
                                if m.get("role") == "user"), default=-1)
            n_tool_turn = sum(1 for m in body["messages"][last_user_at + 1:]
                              if m.get("role") == "tool")
            if n_tool_turn == 0:
                if "TAGFALSO" in utext:
                    query = "informazioni su [FULLNAME_99]"
                elif "[FULLNAME_1]" in utext:
                    query = "ultime notizie su [FULLNAME_1]"
                else:
                    query = "chi è Mario Rossi"
                args = json.dumps({"query": query, "max_results": 3})
                # spezzata in 2 frammenti, come dal vero
                yield chunk(model, {"tool_calls": [
                    {"index": 0, "id": "call_ws", "type": "function",
                     "function": {"name": "web_search",
                                  "arguments": args[:18]}}]})
                yield chunk(model, {"tool_calls": [
                    {"index": 0, "function": {"arguments": args[18:]}}]})
                yield chunk(model, finish="tool_calls", usage=usage(0.001))
            elif n_tool_turn == 1:
                last_tool = [m for m in body["messages"]
                             if m.get("role") == "tool"][-1]
                try:
                    result = json.loads(last_tool.get("content") or "{}")
                except ValueError:
                    result = {}
                rows = result.get("results") or []
                if result.get("outcome") != "ok" or not rows:
                    yield chunk(model, {"content": "Niente dal web: "
                                        + str(result.get("stderr") or "")})
                    yield chunk(model, finish="stop", usage=usage(0.001))
                else:
                    yield chunk(model, {"tool_calls": [
                        {"index": 0, "id": "call_rp", "type": "function",
                         "function": {"name": "read_page",
                                      "arguments": json.dumps(
                                          {"url": rows[0]["url"]})}}]})
                    yield chunk(model, finish="tool_calls", usage=usage(0.001))
            else:
                tag = "[FULLNAME_1]" if "[FULLNAME_1]" in utext else "il tema"
                yield chunk(model, {"content": f"Dal web: novità su {tag}."})
                yield chunk(model, finish="stop", usage=usage(0.002))

        elif model == "test/parallel-model":
            # Un batch di tool call nello STESSO messaggio assistant (è il
            # caso che il loop può eseguire in parallelo), poi la risposta
            # finale. Le due call arrivano a frammenti INTERLACCIATI per
            # index, come dai provider veri.
            users = [m for m in body["messages"] if m.get("role") == "user"]
            utext = users[-1].get("content") or "" if users else ""
            if isinstance(utext, list):
                utext = "".join(p.get("text", "") for p in utext
                                if p.get("type") == "text")
            last_user_at = max((i for i, m in enumerate(body["messages"])
                                if m.get("role") == "user"), default=-1)
            n_tool_turn = sum(1 for m in body["messages"][last_user_at + 1:]
                              if m.get("role") == "tool")
            if n_tool_turn == 0:
                a1 = json.dumps({"query": "meteo milano", "max_results": 3})
                if "SINGOLO" in utext:
                    calls = [("call_a", "web_search", a1)]
                elif "ARGROTTI" in utext:
                    calls = [("call_a", "web_search", a1),
                             ("call_b", "read_page", '{"url": ')]
                elif "TOOLIGNOTO" in utext:
                    calls = [("call_a", "web_search", a1),
                             ("call_b", "execute_python", '{"code": "1+1"}')]
                elif "MISTO" in utext:
                    calls = [("call_a", "execute_python", '{"code": "1+1"}'),
                             ("call_b", "web_search", a1)]
                else:
                    calls = [("call_a", "web_search", a1),
                             ("call_b", "read_page", json.dumps(
                                 {"url": "https://esempio.it/pagina"}))]
                for i, (cid, name, args) in enumerate(calls):
                    yield chunk(model, {"tool_calls": [
                        {"index": i, "id": cid, "type": "function",
                         "function": {"name": name,
                                      "arguments": args[:8]}}]})
                yield chunk(model, {"tool_calls": [
                    {"index": i, "function": {"arguments": args[8:]}}
                    for i, (_cid, _name, args) in enumerate(calls)]})
                yield chunk(model, finish="tool_calls", usage=usage(0.001))
            else:
                yield chunk(model, {"content": "Fatto."})
                yield chunk(model, finish="stop", usage=usage(0.001))

        elif model == "test/loop-model":
            if body.get("tool_choice") == "none":
                yield chunk(model, {"content": "Risposta forzata."})
                yield chunk(model, finish="stop", usage=usage(0.002))
            else:
                yield chunk(model, {"tool_calls": [
                    {"index": 0, "id": f"call_loop_{n_tool}",
                     "type": "function",
                     "function": {"name": "execute_python",
                                  "arguments": '{"code": "1 + 1"}'}}]})
                yield chunk(model, finish="tool_calls", usage=usage(0.0005))

        elif n_tool == 0:
            # reasoning in 2 frammenti (stesso blocco, signature sul secondo)
            yield chunk(model, {
                "reasoning": "Devo eseguire",
                "reasoning_details": [{"type": "reasoning.text",
                                       "text": "Devo eseguire", "id": "r1",
                                       "format": "anthropic-claude-v1",
                                       "index": 0}]})
            yield chunk(model, {
                "reasoning": " del codice.",
                "reasoning_details": [{"type": "reasoning.text",
                                       "text": " del codice.", "id": "r1",
                                       "format": "anthropic-claude-v1",
                                       "index": 0, "signature": "sig-abc"}]})
            yield chunk(model, {"content": "Calcolo con la sandbox."})
            # tool call spezzata in 3 frammenti
            yield chunk(model, {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "execute_python",
                              "arguments": '{"code": "x = 6'}}]})
            yield chunk(model, {"tool_calls": [
                {"index": 0, "function": {"arguments": ' * 7\\n'}}]})
            yield chunk(model, {"tool_calls": [
                {"index": 0, "function": {"arguments": 'x"}'}}]})
            yield chunk(model, finish="tool_calls", usage=usage(0.001))

        elif n_tool == 1:
            code = ("open('/workspace/outputs/risposta.txt', 'w')"
                    ".write(str(x))\nprint('scritto')")
            args = json.dumps({"code": code})
            yield chunk(model, {"tool_calls": [
                {"index": 0, "id": "call_2", "type": "function",
                 "function": {"name": "execute_python",
                              "arguments": args[:25]}}]})
            yield chunk(model, {"tool_calls": [
                {"index": 0, "function": {"arguments": args[25:]}}]})
            yield chunk(model, finish="tool_calls", usage=usage(0.001))

        else:
            yield chunk(model, {"content": "Il risultato"})
            yield chunk(model, {"content": " è 42."})
            yield chunk(model, finish="stop", usage=usage(0.002))

        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def grant_key(session_factory, username="admin", key="sk-or-test-123456789"):
    """Assegna all'utente una chiave di inferenza finta, come farebbe il
    wizard di installazione tramite la management key: le suite non passano
    dal wizard e OpenRouter qui è questo finto server."""
    from app.db import User
    with session_factory() as s:
        u = s.query(User).filter_by(username=username).one()
        u.openrouter_key = key
        s.commit()
    return key


def serve(port=PORT, timeout=20):
    """Avvia il finto OpenRouter in un thread e ritorna la funzione per
    fermarlo. Un thread (e non un processo) perché così un test è UN
    comando: niente server da avviare a mano prima."""
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + timeout
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError(f"il finto OpenRouter non si è avviato sulla {port}")

    def stop():
        server.should_exit = True
    return stop

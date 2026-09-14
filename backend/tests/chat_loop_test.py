"""Loop di tool calling (app/openrouter/chat.py) contro il finto OpenRouter e
la sandbox VERA: accumulo dei frammenti di tool call, echo di
reasoning_details, esecuzioni reali con artifact, errori pre e mid-stream,
tetto di iterazioni e tetto di costo, passthrough dei parametri, rifiuti di
routing (scala di rilassamento e spiegazioni).

Il finto OpenRouter (chat_mock_openrouter.py) si avvia da solo: nessun server
da lanciare a mano, nessun token speso.

Serve Docker attivo e l'immagine costruita:
    docker build -t blockingbear-sandbox:1 backend/sandbox
Uso:
    python backend/tests/chat_loop_test.py
"""
import asyncio
import inspect
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/

os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_loop")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_SANDBOX_POOL"] = "1"
sys.path.insert(0, str(HERE))

import httpx                                                     # noqa: E402
import chat_mock_openrouter as mock                              # noqa: E402
from app import settings_store                                    # noqa: E402
from app.openrouter import chat, sandbox                          # noqa: E402

# prefisso dei container diverso da quello di esercizio: così lo sweep degli
# orfani allo start non azzera il pool di un backend di sviluppo attivo
sandbox._NAME_PREFIX = "blockingbear-sbxt-"

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


def of_type(events, t):
    return [e for e in events if e["type"] == t]


def sent():
    return httpx.get(mock.DEBUG_URL).json()


async def collect(gen):
    return [e async for e in gen]


async def async_main():
    # --- percorso felice: 2 tool call reali poi risposta finale -------------
    reqs_before = len(sent())
    messages = [
        {"role": "system", "content": chat.system_prompt()},
        {"role": "user",
         "content": "Quanto fa 6*7? Scrivi il risultato in un file."},
    ]
    events = await collect(chat.run_turn(
        "loop-conv-1", messages, "test/happy-model", "sk-or-test-loop",
        base_url=mock.BASE_URL))

    text = "".join(e["delta"] for e in of_type(events, "text"))
    check("testo streamato",
          text == "Calcolo con la sandbox.Il risultato è 42.", repr(text))
    reasoning = "".join(e["delta"] for e in of_type(events, "reasoning"))
    check("reasoning streamato", reasoning == "Devo eseguire del codice.",
          repr(reasoning))

    calls = of_type(events, "tool_call")
    check("tool call accumulata dai frammenti",
          len(calls) == 2 and calls[0]["code"] == "x = 6 * 7\nx",
          repr(calls[0]["code"] if calls else None))

    results = of_type(events, "tool_result")
    check("prima esecuzione reale",
          results and results[0]["result"]["outcome"] == "ok"
          and results[0]["result"]["stdout"].strip() == "42")
    host = (results[1]["result"]["files"].get("outputs/risposta.txt")
            if len(results) > 1 else None)
    check("artifact dal secondo step",
          host is not None and open(host, encoding="utf-8").read() == "42",
          str(host))

    done = events[-1]
    check("done con conteggi", done["type"] == "done"
          and done["finish_reason"] == "stop" and done["iterations"] == 2
          and abs(done["usage"]["cost"] - 0.004) < 1e-9,
          json.dumps({k: done[k] for k in ("finish_reason", "iterations",
                                           "usage")}))

    roles = [m["role"] for m in messages]
    check("storia della conversazione", roles == [
        "system", "user", "assistant", "tool", "assistant", "tool",
        "assistant"], str(roles))

    reqs = sent()[reqs_before:]
    check("tre richieste, tools ovunque",
          len(reqs) == 3 and all("tools" in r for r in reqs))
    r2_assist = reqs[1]["messages"][2]
    check("echo tool_calls invariato",
          r2_assist.get("tool_calls", [{}])[0].get("id") == "call_1"
          and json.loads(r2_assist["tool_calls"][0]["function"]["arguments"])
          == {"code": "x = 6 * 7\nx"})
    rd = r2_assist.get("reasoning_details")
    check("echo reasoning_details mergiato",
          rd == [{"type": "reasoning.text", "text": "Devo eseguire del codice.",
                  "id": "r1", "format": "anthropic-claude-v1", "index": 0,
                  "signature": "sig-abc"}], json.dumps(rd, ensure_ascii=False))
    tool_msg = json.loads(reqs[1]["messages"][3]["content"])
    check("tool result senza percorsi host",
          tool_msg["stdout"].strip() == "42" and "files" not in tool_msg)
    check("session_id e caching",
          reqs[0].get("session_id") == "blockingbear-loop-conv-1"
          and reqs[0].get("cache_control") == {"type": "ephemeral"}
          and reqs[0].get("provider", {}).get("require_parameters") is True)

    # --- errore a metà stream ----------------------------------------------
    messages2 = [{"role": "user", "content": "Ciao"}]
    events = await collect(chat.run_turn(
        "loop-conv-2", messages2, "test/error-model", "sk-or-test-loop",
        base_url=mock.BASE_URL))
    text = "".join(e["delta"] for e in of_type(events, "text"))
    check("errore mid-stream",
          of_type(events, "error")
          and "disconnected" in of_type(events, "error")[0]["message"]
          and events[-1]["finish_reason"] == "error"
          and text == "Inizio a rispond")
    check("parziale conservato in storia",
          messages2[-1]["role"] == "assistant"
          and messages2[-1]["content"] == "Inizio a rispond")

    # --- errore pre-stream (chiave non valida) ------------------------------
    messages3 = [{"role": "user", "content": "Ciao"}]
    events = await collect(chat.run_turn(
        "loop-conv-2", messages3, "test/unauthorized", "sk-or-test-bad",
        base_url=mock.BASE_URL))
    check("errore pre-stream",
          of_type(events, "error")
          and "non valida" in of_type(events, "error")[0]["message"]
          and events[-1]["finish_reason"] == "error"
          and len(messages3) == 1)

    # --- PDF nativo rifiutato dal routing ZDR: si riparte senza -------------
    reqs_before = len(sent())
    pdf_part = {"type": "file", "file": {"filename": "verbale.pdf",
                                          "file_data": "data:application/pdf;"
                                                       "base64,JVBERi0x"}}
    messages_pdf = [
        {"role": "user", "content": [
            {"type": "text", "text": "Cos'è questo file?\n\n<allegati>"
                                     "verbale.pdf</allegati>"},
            pdf_part]}]
    strict = {"zdr": True, "data_collection": "deny"}
    events = await collect(chat.run_turn(
        "loop-conv-pdf", messages_pdf, "test/nofile-model", "sk-or-test-loop",
        tools=[], provider=strict, base_url=mock.BASE_URL))
    reqs = sent()[reqs_before:]
    text = "".join(e["delta"] for e in of_type(events, "text"))
    check("file rifiutato: nessun errore, risposta arrivata",
          not of_type(events, "error") and text == "Risposta senza sandbox."
          and events[-1]["finish_reason"] == "stop", repr(text))
    check("file rifiutato: due richieste, la seconda senza parti file",
          len(reqs) == 2
          and any(p.get("type") == "file"
                  for p in reqs[0]["messages"][0]["content"])
          and isinstance(reqs[1]["messages"][0]["content"], str)
          and "/workspace/inputs/verbale.pdf" in reqs[1]["messages"][0]["content"]
          and "<allegati>" in reqs[1]["messages"][0]["content"],
          f"richieste={len(reqs)}")
    check("file rifiutato: il messaggio utente in storia è senza file",
          isinstance(messages_pdf[0]["content"], str)
          and messages_pdf[-1]["role"] == "assistant")
    check("file rifiutato: il modello resta segnato per la route",
          chat.file_input_refused("test/nofile-model", strict)
          and not chat.file_input_refused("test/nofile-model", None)
          and not chat.file_input_refused("test/happy-model", strict))

    # --- modello che non si ferma: budget di iterazioni ----------------------
    reqs_before = len(sent())
    messages4 = [{"role": "user", "content": "Vai"}]
    events = await collect(chat.run_turn(
        "loop-conv-3", messages4, "test/loop-model", "sk-or-test-loop",
        base_url=mock.BASE_URL, max_iter=3))
    executed = [e for e in of_type(events, "tool_result")
                if e["result"]["outcome"] == "ok"]
    refused = [e for e in of_type(events, "tool_result")
               if e["result"]["outcome"] == "budget_exceeded"]
    done = events[-1]
    check("budget di iterazioni",
          len(executed) == 3 and len(refused) == 1
          and done["iterations"] == 3 and done["finish_reason"] == "stop",
          f"eseguite={len(executed)} rifiutate={len(refused)}")
    text = "".join(e["delta"] for e in of_type(events, "text"))
    check("risposta finale forzata", text == "Risposta forzata.")
    reqs = sent()[reqs_before:]
    check("tool_choice=none sull'ultimo giro",
          reqs[-1].get("tool_choice") == "none"
          and all("tool_choice" not in r for r in reqs[:-1]))

    # --- i tetti sono parametri di esercizio; nessun tetto di tempo ---------
    sig = inspect.signature(chat.run_turn).parameters
    check("run_turn: tetti come parametri, niente wall clock",
          "wall_clock" not in sig
          and all(k in sig for k in ("max_iter", "exec_timeout",
                                     "page_max_chars", "cost_limit")))
    # senza max_iter il loop ricade sul default del REGISTRY (qui il DB non
    # è inizializzato: settings_store.current risponde col default)
    default_rounds = settings_store.REGISTRY["chat_max_tool_rounds"]["default"]
    messages5 = [{"role": "user", "content": "Vai"}]
    events = await collect(chat.run_turn(
        "loop-conv-4", messages5, "test/loop-model", "sk-or-test-loop",
        base_url=mock.BASE_URL))
    done = events[-1]
    check("default dei giri dal registro delle Impostazioni",
          done["iterations"] == default_rounds == 50
          and done["finish_reason"] == "stop",
          f"iterations={done['iterations']} default={default_rounds}")

    # --- tetto di COSTO + parametri di generazione ---------------------------
    reqs_before = len(sent())
    messages5 = [{"role": "user", "content": "Analizza"}]
    events = await collect(chat.run_turn(
        "loop-conv-4", messages5, "test/happy-model", "sk-or-test-loop",
        base_url=mock.BASE_URL, cost_limit=0.0015,
        params={"temperature": 0.3}))
    executed = [e for e in of_type(events, "tool_result")
                if e["result"]["outcome"] == "ok"]
    refused = [e for e in of_type(events, "tool_result")
               if e["result"]["outcome"] == "budget_exceeded"]
    check("tetto di costo: si smette di eseguire",
          len(executed) == 1 and len(refused) == 1
          and "costo" in refused[0]["result"]["stderr"],
          refused[0]["result"]["stderr"][:70] if refused else "")
    check("la risposta arriva comunque",
          "".join(e["delta"] for e in of_type(events, "text")).endswith("42."))
    check("parametri di generazione inoltrati",
          all(r.get("temperature") == 0.3 for r in sent()[reqs_before:]))

    # --- rifiuti di routing: la scala di rilassamento -----------------------
    # test/strict-model è anthropic/claude-fable-5.1 dal vero (2026-09-11):
    # dichiara tools ma nessun endpoint dichiara tool_choice, e con
    # provider.require_parameters la risposta finale forzata moriva con 404
    # "No endpoints found that can handle the requested parameters" dopo 10
    # iterazioni di ricerche. Il modello, senza tool_choice, si ferma solo
    # leggendo i tool result «budget esaurito».
    def text_of(events):
        return "".join(e["delta"] for e in of_type(events, "text"))

    def history_valid(messages):
        """Ogni tool call ha il suo tool result: la storia si può rimandare."""
        calls = {c["id"] for m in messages if m["role"] == "assistant"
                 for c in m.get("tool_calls") or ()}
        results = {m["tool_call_id"] for m in messages if m["role"] == "tool"}
        return calls == results

    chat._ROUTING_RELAXED.clear()
    reqs_before = len(sent())
    messages6 = [{"role": "user", "content": "Vai"}]
    events = await collect(chat.run_turn(
        "loop-conv-5", messages6, "test/strict-model", "sk-or-test-loop",
        base_url=mock.BASE_URL, max_iter=2))
    reqs = sent()[reqs_before:]
    check("tool_choice rifiutato dal routing: la risposta arriva comunque",
          not of_type(events, "error") and text_of(events) == "Risposta forzata."
          and events[-1]["finish_reason"] == "stop"
          and history_valid(messages6), repr(text_of(events)))
    # 2 esecuzioni, 1 call rifiutata per budget, poi [con tool_choice → 404]
    # e la stessa richiesta senza tool_choice (require_parameters resta)
    check("gradino 1: si ritenta senza tool_choice, con require_parameters",
          len(reqs) == 5 and reqs[3].get("tool_choice") == "none"
          and "tool_choice" not in reqs[4]
          and reqs[4]["provider"].get("require_parameters") is True
          and reqs[4]["messages"] == reqs[3]["messages"],
          f"richieste={len(reqs)}")
    check("il gradino si ricorda per il modello",
          chat.routing_level("test/strict-model") == 1
          and chat.routing_level("test/strict-model",
                                 {"zdr": True}) == 0)

    reqs_before = len(sent())
    events = await collect(chat.run_turn(
        "loop-conv-6", [{"role": "user", "content": "Vai"}],
        "test/strict-model", "sk-or-test-loop",
        base_url=mock.BASE_URL, max_iter=2))
    reqs = sent()[reqs_before:]
    check("turno successivo: niente tool_choice, nessuna richiesta a vuoto",
          len(reqs) == 4 and not any("tool_choice" in r for r in reqs)
          and text_of(events) == "Risposta forzata.",
          f"richieste={len(reqs)}")

    # il catalogo lo sa prima: supported_parameters senza tool_choice → non
    # si spedisce nemmeno la prima volta, e il gradino resta 0
    chat._ROUTING_RELAXED.clear()
    reqs_before = len(sent())
    events = await collect(chat.run_turn(
        "loop-conv-7", [{"role": "user", "content": "Vai"}],
        "test/strict-model", "sk-or-test-loop",
        base_url=mock.BASE_URL, max_iter=2,
        supported_params=["tools", "max_tokens", "temperature"]))
    reqs = sent()[reqs_before:]
    check("supported_parameters senza tool_choice: mai spedito",
          len(reqs) == 4 and not any("tool_choice" in r for r in reqs)
          and chat.routing_level("test/strict-model") == 0
          and text_of(events) == "Risposta forzata.",
          f"richieste={len(reqs)} "
          f"livello={chat.routing_level('test/strict-model')}")

    # modello TESTARDO: ignora il primo avviso di budget (senza tool_choice
    # può farlo) e chiama ancora un tool: un'altra possibilità, poi basta
    reqs_before = len(sent())
    messages7 = [{"role": "user", "content": "Vai TESTARDO"}]
    events = await collect(chat.run_turn(
        "loop-conv-8", messages7, "test/strict-model", "sk-or-test-loop",
        base_url=mock.BASE_URL, max_iter=1,
        supported_params=["tools", "max_tokens"]))
    reqs = sent()[reqs_before:]
    refused = [e for e in of_type(events, "tool_result")
               if e["result"]["outcome"] == "budget_exceeded"]
    check("avviso di budget ignorato: un secondo tentativo, poi la risposta",
          text_of(events) == "Risposta forzata." and len(refused) == 2
          and len(reqs) == 4 and events[-1]["finish_reason"] == "stop"
          and history_valid(messages7),
          f"richieste={len(reqs)} rifiutate={len(refused)}")

    # parametro utente che restringe l'imbuto sotto ZDR (temperature su
    # claude-opus-5, max_tokens su gpt-5.5 dal vero): il 404 parla di
    # "data policy", ma l'imbuto mostra il filtro parametri → gradino 2,
    # regole privacy intatte
    chat._ROUTING_RELAXED.clear()
    strict = {"zdr": True, "data_collection": "deny"}
    reqs_before = len(sent())
    events = await collect(chat.run_turn(
        "loop-conv-9", [{"role": "user", "content": "Vai"}],
        "test/strict-model", "sk-or-test-loop",
        base_url=mock.BASE_URL, max_iter=1, provider=strict,
        params={"temperature": 0.3},
        supported_params=["tools", "max_tokens", "temperature"]))
    reqs = sent()[reqs_before:]
    check("parametro che svuota l'imbuto sotto ZDR: preferenze morbide",
          not of_type(events, "error") and len(reqs) == 4
          and reqs[0]["provider"].get("require_parameters") is True
          and all("require_parameters" not in r["provider"]
                  for r in reqs[1:])
          and all(r["provider"].get("zdr") is True
                  and r["provider"].get("data_collection") == "deny"
                  and r.get("temperature") == 0.3 for r in reqs)
          and text_of(events) == "Risposta forzata.",
          f"richieste={len(reqs)} errori={of_type(events, 'error')}")
    check("gradino 2 ricordato solo per le regole strette",
          chat.routing_level("test/strict-model", strict) == 2
          and chat.routing_level("test/strict-model") == 0)

    # solo privacy: nessun endpoint ZDR, imbuto pieno fino al filtro privacy
    # → niente da rilassare, una sola richiesta e la spiegazione (oggi
    # OpenRouter risponde 404, non più 503)
    reqs_before = len(sent())
    events = await collect(chat.run_turn(
        "loop-conv-10", [{"role": "user", "content": "Vai"}],
        "test/nozdr404-model", "sk-or-test-loop",
        base_url=mock.BASE_URL, provider=strict))
    reqs = sent()[reqs_before:]
    errs = of_type(events, "error")
    check("solo privacy (404 con imbuto pieno): nessun ritentativo",
          len(reqs) == 1 and errs and errs[0]["status"] == 404
          and "Zero Data Retention" in errs[0]["message"]
          and events[-1]["finish_reason"] == "error"
          and chat.routing_level("test/nozdr404-model", strict) == 0,
          f"richieste={len(reqs)} "
          f"{errs[0]['message'][:60] if errs else 'nessun errore'}")

    # le spiegazioni, a freddo
    err = chat.or_client.OpenRouterError(
        "No endpoints found matching your data policy", status=503)
    check("503 privacy (snapshot 2026-08) spiegato",
          "Zero Data Retention" in chat._explain(err, {"provider": strict}))
    err = chat.or_client.OpenRouterError(
        "No endpoints found that can handle the requested parameters.",
        status=404, metadata={"failed_routing_step": "Filter by Parameters",
                              "routing_funnel": [{"step": "Initial Endpoints",
                                                  "endpoint_count": 4}]})
    check("404 parametri spiegato",
          "parametri" in chat._explain(err, {"provider": {}})
          and chat._params_narrowed(err) is True)
    err = chat.or_client.OpenRouterError(
        "No endpoints found matching your data policy", status=404,
        metadata={"failed_routing_step": "Filter by Data Policy",
                  "routing_funnel": [{"step": "Initial Endpoints",
                                      "endpoint_count": 4}]})
    check("imbuto pieno: i parametri non c'entrano",
          chat._params_narrowed(err) is False
          and "Zero Data Retention" in chat._explain(err, {"provider": strict}))
    err = chat.or_client.OpenRouterError("Provider returned error", status=502)
    check("un 502 del provider non è un rifiuto di routing",
          not chat._is_routing_refusal(err)
          and chat._explain(err, {"provider": strict}) == str(err))


def main():
    stop_mock = mock.serve()
    sandbox.start()
    deadline = time.time() + 90
    while time.time() < deadline:
        if sandbox.status()["available"] and sandbox.status()["pool_ready"]:
            break
        time.sleep(0.5)
    check("sandbox pronta", sandbox.status()["available"],
          str(sandbox.status()))
    try:
        asyncio.run(async_main())
    finally:
        sandbox.shutdown()
        stop_mock()
    print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

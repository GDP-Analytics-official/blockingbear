"""Loop di tool calling (app/openrouter/chat.py) contro il finto OpenRouter e
la sandbox VERA: accumulo dei frammenti di tool call, echo di
reasoning_details, esecuzioni reali con artifact, errori pre e mid-stream,
tetto di iterazioni e tetto di costo, passthrough dei parametri.

Il finto OpenRouter (chat_mock_openrouter.py) si avvia da solo: nessun server
da lanciare a mano, nessun token speso.

Serve Docker attivo e l'immagine costruita:
    docker build -t blockingbear-sandbox:1 backend/sandbox
Uso:
    python backend/tests/chat_loop_test.py
"""
import asyncio
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

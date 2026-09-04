"""Batch di tool call nello stesso messaggio assistant (chat.run_turn): un
batch di SOLI tool web parte in parallelo (asyncio.gather), qualunque batch
che tocca execute_python resta seriale (le esecuzioni condividono lo stato
del kernel e l'ordine conta). Si collaudano: sovrapposizione reale dei tempi,
ordine di eventi e storia, filtri result_keys, argomenti rotti e tool non
dichiarati dentro il batch, annullamento a metà batch.

NIENTE Docker, niente camofox, niente modello NER: browser.search/read e
sandbox.execute sono finti temporizzati, il modello è test/parallel-model
del finto OpenRouter (due call nello stesso messaggio, frammenti
interlacciati per index).

Uso:
    python backend/tests/parallel_tools_test.py
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_parallel")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_SANDBOX_ENGINE"] = "off"

import chat_mock_openrouter as mock                              # noqa: E402
from app.openrouter import browser                               # noqa: E402
from app.openrouter import chat                                  # noqa: E402
from app.openrouter import tools as or_tools                     # noqa: E402

PASS = 0
FAIL = 0

# il tempo di ogni handler finto: abbastanza lungo da rendere la
# sovrapposizione misurabile senza ambiguità, abbastanza corto da non
# rallentare la suite
_SLEEP = 0.6

CALLS = []      # (nome, inizio, fine) su time.monotonic, in ordine di FINE


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


def tool_events(events):
    return [e["type"] for e in events
            if e["type"] in ("tool_call", "tool_result")]


async def collect(gen):
    return [e async for e in gen]


def fake_search(query, max_results=5):
    t0 = time.monotonic()
    time.sleep(_SLEEP)
    CALLS.append(("search", t0, time.monotonic()))
    return [{"title": "Esempio", "url": "https://esempio.it/pagina",
             "snippet": "Uno snippet."}]


def fake_read(url, cap=120000):
    t0 = time.monotonic()
    time.sleep(_SLEEP)
    CALLS.append(("read", t0, time.monotonic()))
    return {"url": url, "title": "Pagina", "text": "Contenuto.", "total": 10}


def fake_execute(conv_id, code, timeout=None, files=None):
    t0 = time.monotonic()
    time.sleep(_SLEEP)
    CALLS.append(("python", t0, time.monotonic()))
    return {"stdout": "2", "stderr": "", "outcome": "ok",
            "new_files": [], "files": {}, "elapsed_ms": int(_SLEEP * 1000)}


browser.search = fake_search
browser.read = fake_read
or_tools.sandbox.execute = fake_execute

WEB_TOOLS = [or_tools.web_search_tool(), or_tools.read_page_tool()]
ALL_TOOLS = [or_tools.python_tool()] + WEB_TOOLS


def overlapped():
    """I due handler registrati in CALLS hanno girato sovrapposti?"""
    if len(CALLS) != 2:
        return None
    (a, b) = sorted(CALLS, key=lambda c: c[1])   # per istante di inizio
    return b[1] < a[2]                            # b inizia prima che a finisca


async def run(conv_id, content, tools, model="test/parallel-model",
              cancel=None):
    messages = [{"role": "user", "content": content}]
    events = await collect(chat.run_turn(
        conv_id, messages, model, "sk-or-test",
        base_url=mock.BASE_URL, tools=tools, cancel=cancel))
    return messages, events


async def async_main():
    # --- batch di due tool web: in parallelo ---------------------------------
    CALLS.clear()
    messages, events = await run("par-1", "meteo", WEB_TOOLS)

    calls = of_type(events, "tool_call")
    results = of_type(events, "tool_result")
    check("batch web: due call e due result",
          len(calls) == 2 and len(results) == 2
          and [c["id"] for c in calls] == ["call_a", "call_b"]
          and [r["id"] for r in results] == ["call_a", "call_b"],
          str([e.get("id") for e in calls + results]))
    check("batch web: tutte le call PRIMA dei result",
          tool_events(events) == ["tool_call", "tool_call",
                                  "tool_result", "tool_result"],
          str(tool_events(events)))
    check("batch web: kind sugli eventi",
          [c["kind"] for c in calls] == ["search", "page"]
          and [r["kind"] for r in results] == ["search", "page"])
    check("batch web: esiti ok",
          results[0]["result"]["outcome"] == "ok"
          and results[0]["result"]["results"][0]["url"]
          == "https://esempio.it/pagina"
          and results[1]["result"]["outcome"] == "ok"
          and results[1]["result"]["content"] == "Contenuto.")
    check("batch web: handler sovrapposti", overlapped() is True,
          str(CALLS))
    # la finestra del BATCH (dal primo start all'ultima fine), non il turno
    # intero: i roundtrip HTTP verso il finto OpenRouter non c'entrano
    window = max(c[2] for c in CALLS) - min(c[1] for c in CALLS)
    check("batch web: durata da parallelo (non somma)",
          window < _SLEEP * 2 * 0.9, f"{window:.2f}s")

    text = "".join(e["delta"] for e in of_type(events, "text"))
    done = events[-1]
    check("batch web: risposta finale e done",
          text == "Fatto." and done["type"] == "done"
          and done["finish_reason"] == "stop" and done["iterations"] == 1,
          repr(text))

    roles = [m["role"] for m in messages]
    tool_ids = [m.get("tool_call_id") for m in messages
                if m["role"] == "tool"]
    check("batch web: storia valida e in ordine",
          roles == ["user", "assistant", "tool", "tool", "assistant"]
          and tool_ids == ["call_a", "call_b"],
          f"{roles} {tool_ids}")
    tool_payloads = [json.loads(m["content"]) for m in messages
                     if m["role"] == "tool"]
    check("batch web: result_keys filtrate anche in parallelo",
          set(tool_payloads[0]) <= {"results", "outcome", "stderr",
                                    "elapsed_ms", "notice"}
          and set(tool_payloads[1]) <= {"url", "title", "content",
                                        "truncated", "outcome", "stderr",
                                        "elapsed_ms", "notice"}
          and "files" not in tool_payloads[0],
          str([sorted(p) for p in tool_payloads]))

    # --- una sola call web: percorso seriale, nessun batch -----------------
    CALLS.clear()
    messages, events = await run("par-2", "meteo SINGOLO", WEB_TOOLS)
    check("call singola: eventi interlacciati",
          tool_events(events) == ["tool_call", "tool_result"]
          and of_type(events, "tool_result")[0]["result"]["outcome"] == "ok",
          str(tool_events(events)))
    check("call singola: risposta finale",
          "".join(e["delta"] for e in of_type(events, "text")) == "Fatto.")

    # --- batch misto (execute_python + web_search): seriale ------------------
    CALLS.clear()
    messages, events = await run("par-3", "calcola MISTO", ALL_TOOLS)
    results = of_type(events, "tool_result")
    check("batch misto: eventi interlacciati (seriale)",
          tool_events(events) == ["tool_call", "tool_result",
                                  "tool_call", "tool_result"],
          str(tool_events(events)))
    check("batch misto: esiti ok in ordine",
          len(results) == 2 and results[0]["result"]["outcome"] == "ok"
          and results[0]["result"]["stdout"] == "2"
          and results[1]["result"]["outcome"] == "ok")
    check("batch misto: NESSUNA sovrapposizione", overlapped() is False,
          str(CALLS))
    check("batch misto: python prima della ricerca",
          [c[0] for c in CALLS] == ["python", "search"], str(CALLS))

    # --- argomenti rotti dentro il batch parallelo ---------------------------
    CALLS.clear()
    messages, events = await run("par-4", "meteo ARGROTTI", WEB_TOOLS)
    results = of_type(events, "tool_result")
    check("argomenti rotti: batch resta parallelo nel flusso eventi",
          tool_events(events) == ["tool_call", "tool_call",
                                  "tool_result", "tool_result"],
          str(tool_events(events)))
    check("argomenti rotti: la call sana gira, la rotta è un errore",
          results[0]["result"]["outcome"] == "ok"
          and results[1]["result"]["outcome"] == "error"
          and "Argomenti della tool call non validi"
          in results[1]["result"]["stderr"]
          and [c[0] for c in CALLS] == ["search"],
          results[1]["result"]["stderr"][:60])
    check("argomenti rotti: storia comunque valida",
          [m.get("tool_call_id") for m in messages if m["role"] == "tool"]
          == ["call_a", "call_b"])

    # --- tool non dichiarato dentro il batch: si resta seriali ---------------
    CALLS.clear()
    messages, events = await run("par-5", "meteo TOOLIGNOTO", WEB_TOOLS)
    results = of_type(events, "tool_result")
    check("tool non dichiarato: percorso seriale",
          tool_events(events) == ["tool_call", "tool_result",
                                  "tool_call", "tool_result"],
          str(tool_events(events)))
    check("tool non dichiarato: errore parlante, l'altra call gira",
          results[0]["result"]["outcome"] == "ok"
          and results[1]["result"]["outcome"] == "error"
          and "Tool sconosciuto" in results[1]["result"]["stderr"]
          and [c[0] for c in CALLS] == ["search"],
          results[1]["result"]["stderr"][:60])

    # --- annullamento a metà batch parallelo --------------------------------
    CALLS.clear()
    cancel = asyncio.Event()

    async def trip():
        await asyncio.sleep(_SLEEP / 4)
        cancel.set()

    t0 = time.monotonic()
    tripper = asyncio.create_task(trip())
    messages, events = await run("par-6", "meteo", WEB_TOOLS, cancel=cancel)
    await tripper
    elapsed = time.monotonic() - t0
    results = of_type(events, "tool_result")
    check("annullamento: entrambi i result canceled",
          len(results) == 2
          and all(r["result"]["outcome"] == "canceled" for r in results),
          str([r["result"]["outcome"] for r in results]))
    check("annullamento: il turno molla subito (lavoro orfano)",
          elapsed < _SLEEP * 0.9 and events[-1]["finish_reason"] == "canceled",
          f"{elapsed:.2f}s {events[-1]['finish_reason']}")
    check("annullamento: storia valida (ogni call ha il suo result)",
          [m.get("tool_call_id") for m in messages if m["role"] == "tool"]
          == ["call_a", "call_b"])
    # i thread orfani finiscono per conto loro: si aspettano per non
    # inquinare i CALLS di eventuali test futuri
    await asyncio.sleep(_SLEEP + 0.2)


def main():
    stop_mock = mock.serve()
    try:
        asyncio.run(async_main())
    finally:
        stop_mock()
    print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()



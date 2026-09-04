"""Ciclo di vita del SandboxManager (app/openrouter/sandbox.py) con un runtime
Docker vero: warm pool, assegnazione per conversazione, staging dei file,
artifact, crash del kernel, eviction LRU, reaper degli inattivi, shutdown.

Configurazione ridotta per fare in fretta (pool=1, max 2 assegnati, reaper
ogni 2 s): sono gli stessi percorsi di codice, con numeri più piccoli.

Serve Docker attivo e l'immagine costruita:
    docker build -t blockingbear-sandbox:1 backend/sandbox
Uso:
    python backend/tests/sandbox_manager_test.py
"""
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/

# PRIMA dell'import di app.config: dati isolati + parametri da test
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_manager")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_SANDBOX_POOL"] = "1"
os.environ["BLOCKINGBEAR_SANDBOX_MAX"] = "2"
sys.path.insert(0, str(HERE))

from app.openrouter import sandbox  # noqa: E402

# Prefisso DIVERSO da quello di esercizio: allo start il manager spazza via
# tutti i container col proprio prefisso (orfani di un crash precedente), e un
# backend di sviluppo attivo si ritroverebbe il pool azzerato a metà lavoro.
sandbox._NAME_PREFIX = "blockingbear-sbxt-"
ORPHAN = sandbox._NAME_PREFIX + "orphan"

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


def wait_for(pred, seconds, step=0.5):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def docker_names():
    r = subprocess.run(["docker", "ps", "-a", "--filter",
                        f"name={sandbox._NAME_PREFIX}",
                        "--format", "{{.Names}}"],
                       capture_output=True, text=True, timeout=30)
    return [n for n in r.stdout.split() if n]


def main():
    # container orfano finto (come dopo un crash del backend)
    subprocess.run(["docker", "rm", "-f", ORPHAN],
                   capture_output=True, timeout=30)
    subprocess.run(["docker", "run", "-d", "-i", "--name", ORPHAN,
                    os.environ.get("BLOCKINGBEAR_SANDBOX_IMAGE",
                                   "blockingbear-sandbox:1")],
                   capture_output=True, timeout=60, check=True)

    sandbox._REAP_INTERVAL = 2
    sandbox.start()

    ok = wait_for(lambda: sandbox.status()["available"], 30)
    check("rilevazione runtime", ok, str(sandbox.status()))
    ok = wait_for(lambda: sandbox.status()["pool_ready"] >= 1, 90)
    check("warm pool pieno", ok, str(sandbox.status()))
    check("smoke test reale pubblicato nello stato",
          sandbox.status().get("smoke_tested") is True,
          str(sandbox.status()))
    check("sweep orfani", ORPHAN not in docker_names())

    t0 = time.perf_counter()
    r = sandbox.execute("conv-a", "x = 41")
    ms = (time.perf_counter() - t0) * 1000
    check("assegnazione dal pool", r["outcome"] == "ok" and ms < 2000,
          f"{ms:.0f} ms")

    r = sandbox.execute("conv-a", "x + 1")
    check("stato persistente", r["stdout"].strip() == "42")

    r = sandbox.execute("conv-a",
                        "print(open('/workspace/inputs/note.txt').read())",
                        files={"note.txt": b"ciao mondo"})
    check("staging file input", r["stdout"].strip() == "ciao mondo")

    r = sandbox.execute(
        "conv-a", "open('/workspace/inputs/note.txt', 'w').write('mutato')")
    check("input sandbox in sola lettura",
          r["outcome"] == "error" and "PermissionError" in r["stderr"])
    r = sandbox.execute(
        "conv-a", "print(open('/workspace/inputs/note.txt').read())")
    check("input resta integro", r["stdout"].strip() == "ciao mondo")

    r = sandbox.execute("conv-a",
                        "open('/workspace/outputs/out.txt', 'w').write('fatto')")
    host = r["files"].get("outputs/out.txt")
    check("artifact con percorso host",
          host is not None and open(host, encoding="utf-8").read() == "fatto",
          str(host))

    r = sandbox.execute("conv-b", "x")
    check("isolamento tra conversazioni",
          r["outcome"] == "error" and "NameError" in r["stderr"])

    ok = wait_for(lambda: sandbox.status()["pool_ready"] >= 1, 60)
    check("rimpiazzo asincrono del pool", ok, str(sandbox.status()))

    # crash: il container di conv-a muore sotto al manager
    name = sandbox._assigned["conv-a"].name
    subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                   timeout=30, check=True)
    r = sandbox.execute("conv-a", "1 + 1")
    check("kernel morto -> kernel_restarted",
          r["outcome"] == "kernel_restarted", r["stderr"][:80])
    ok = wait_for(lambda: "conv-a" in sandbox._assigned, 10)
    r = sandbox.execute("conv-a", "40 + 2")
    check("ambiente ricreato funzionante",
          ok and r["outcome"] == "ok" and r["stdout"].strip() == "42")

    # eviction LRU: max=2, conv-a e conv-b assegnate, arriva conv-c
    r = sandbox.execute("conv-c", "0")
    st = sandbox.status()
    check("eviction LRU al tetto", r["outcome"] == "ok"
          and st["assigned"] == 2 and "conv-b" not in sandbox._assigned,
          str(st))
    r = sandbox.execute("conv-b", "1 + 1")
    check("avviso dopo eviction", r.get("notice") is not None
          and r["outcome"] == "ok", str(r.get("notice"))[:60])

    # reaper: abbasso l'idle a 3 s e aspetto che spenga gli assegnati
    sandbox._IDLE_SECONDS = 3
    ok = wait_for(lambda: sandbox.status()["assigned"] == 0, 20)
    check("reaper idle", ok, str(sandbox.status()))
    sandbox._IDLE_SECONDS = 3600
    r = sandbox.execute("conv-c", "2 + 2")
    check("avviso dopo reap", r.get("notice") is not None
          and r["stdout"].strip() == "4")

    sandbox.shutdown()
    ok = wait_for(lambda: docker_names() == [], 20)
    check("shutdown pulito", ok, str(docker_names()))
    staging = Path(os.environ["BLOCKINGBEAR_DATA_DIR"]) / "sandbox"

    def staging_empty():
        return not staging.is_dir() or not any(staging.iterdir())
    check("staging ripulito", wait_for(staging_empty, 10),
          str(list(staging.iterdir()) if staging.is_dir() else []))

    print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

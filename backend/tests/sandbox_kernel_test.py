"""Collaudo del MATTONE di base: l'immagine sandbox e il suo kernel, senza
l'app intorno (sandbox/Dockerfile + sandbox/kernel.py).

Avvia il container con gli stessi flag della sandbox, parla col kernel via
JSON-lines su stdin/stdout e verifica: statefulness, echo dell'ultima
espressione, file di input/output, LibreOffice caldo, timeout che non uccide lo
stato, errori, isolamento di rete, inputs in sola lettura, troncamento.

Serve Docker attivo e l'immagine costruita:
    docker build -t blockingbear-sandbox:1 backend/sandbox
Uso:
    python backend/tests/sandbox_kernel_test.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

IMAGE = os.environ.get("BLOCKINGBEAR_SANDBOX_IMAGE", "blockingbear-sandbox:1")
STAGING = Path(__file__).parent / "data" / "test_kernel"
INPUTS = STAGING / "inputs"
OUTPUTS = STAGING / "outputs"

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


def main():
    INPUTS.mkdir(parents=True, exist_ok=True)
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    for f in OUTPUTS.iterdir():
        f.unlink()
    (INPUTS / "dati.csv").write_text(
        "citta,fatturato\nTorino,120\nMilano,340\nRoma,210\n", encoding="utf-8")

    cmd = [
        "docker", "run", "--rm", "-i",
        # nome fuori dal prefisso di esercizio (blockingbear-sbx-): un riavvio del
        # backend farebbe lo sweep degli "orfani" e ucciderebbe questo container
        "--name", "blockingbear-sbxt-kernel",
        "--network=none",
        "--read-only", "--tmpfs", "/tmp:size=256m",
        "--memory=2g", "--cpus=1", "--pids-limit=256",
        "--cap-drop=ALL", "--security-opt", "no-new-privileges",
        "--user", "1000:1000",
        "-v", f"{INPUTS}:/workspace/inputs:ro",
        "-v", f"{OUTPUTS}:/workspace/outputs",
        IMAGE,
    ]
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
    )

    def rpc(code, timeout=120):
        proc.stdin.write(json.dumps({"code": code, "timeout": timeout}) + "\n")
        proc.stdin.flush()
        t = time.perf_counter()
        line = proc.stdout.readline()
        ms = (time.perf_counter() - t) * 1000
        if not line:
            raise RuntimeError("il kernel ha chiuso stdout (container morto?)")
        return json.loads(line), ms

    try:
        ready = json.loads(proc.stdout.readline())
        boot_ms = (time.perf_counter() - t0) * 1000
        check("handshake ready", ready.get("ready") is True,
              f"python {ready.get('python')} in {boot_ms:.0f} ms")

        r, ms = rpc("2 + 2")
        check("echo ultima espressione",
              r["stdout"].strip() == "4" and r["outcome"] == "ok",
              f"stdout={r['stdout'].strip()!r} {ms:.0f} ms")

        rpc("x = 41")
        r, ms = rpc("x + 1")
        check("stato persistente", r["stdout"].strip() == "42", f"{ms:.0f} ms")

        r, ms1 = rpc("import pandas as pd; print(pd.__version__)")
        check("import pandas", r["outcome"] == "ok",
              f"v={r['stdout'].strip()} primo import {ms1:.0f} ms")
        r, ms2 = rpc("pd.DataFrame({'a': [1]}).shape")
        check("pandas caldo", r["outcome"] == "ok", f"secondo uso {ms2:.0f} ms")

        r, ms = rpc("df = pd.read_csv('/workspace/inputs/dati.csv'); df.shape")
        check("lettura input", r["stdout"].strip() == "(3, 2)", f"{ms:.0f} ms")

        r, ms = rpc("df.to_excel('/workspace/outputs/report.xlsx', index=False)")
        check("output xlsx + new_files",
              "outputs/report.xlsx" in r["new_files"]
              and (OUTPUTS / "report.xlsx").is_file(),
              f"new_files={r['new_files']} {ms:.0f} ms")

        code = (
            "import subprocess\n"
            "from docx import Document\n"
            "d = Document()\n"
            "d.add_heading('Prova sandbox', 0)\n"
            "d.add_paragraph('Generato da python-docx e convertito da LibreOffice.')\n"
            "d.save('/tmp/prova.docx')\n"
            "p = subprocess.run(['soffice', '--headless', '--convert-to', 'pdf',\n"
            "                    '--outdir', '/workspace/outputs', '/tmp/prova.docx'],\n"
            "                   capture_output=True, text=True, timeout=90)\n"
            "print('rc =', p.returncode)\n"
        )
        r, ms = rpc(code)
        check("docx -> pdf via soffice",
              "outputs/prova.pdf" in r["new_files"]
              and (OUTPUTS / "prova.pdf").is_file(),
              f"{ms:.0f} ms (atteso <8000 col profilo pre-cotto) "
              f"stderr={r['stderr'][:120]!r}")

        r, ms = rpc(
            "import matplotlib.pyplot as plt\n"
            "plt.plot([1, 2, 3], [2, 4, 9])\n"
            "plt.savefig('/workspace/outputs/grafico.png')\n"
        )
        check("grafico matplotlib", "outputs/grafico.png" in r["new_files"],
              f"{ms:.0f} ms")

        r, ms = rpc("while True: pass", timeout=3)
        check("timeout", r["outcome"] == "timeout", f"{ms:.0f} ms")
        r, _ = rpc("x + 1")
        check("stato sopravvive al timeout", r["stdout"].strip() == "42")

        r, _ = rpc("1 / 0")
        check("errore con traceback", r["outcome"] == "error"
              and "ZeroDivisionError" in r["stderr"]
              and "kernel.py" not in r["stderr"], f"stderr={r['stderr'][-160:]!r}")

        r, _ = rpc(
            "import socket\n"
            "s = socket.socket(); s.settimeout(3)\n"
            "try:\n"
            "    s.connect(('1.1.1.1', 80)); print('RETE APERTA')\n"
            "except OSError as e:\n"
            "    print('rete bloccata:', type(e).__name__)\n"
        )
        check("isolamento rete", "rete bloccata" in r["stdout"],
              r["stdout"].strip())

        r, _ = rpc("open('/workspace/inputs/hack.txt', 'w')")
        check("inputs read-only", r["outcome"] == "error"
              and ("Read-only" in r["stderr"] or "Permission" in r["stderr"]))

        r, _ = rpc("input()")
        check("input() non blocca",
              r["outcome"] == "error" and "EOFError" in r["stderr"])

        r, _ = rpc("print('x' * 20000)")
        check("troncamento stdout",
              "troncato" in r["stdout"] and len(r["stdout"]) < 9000,
              f"len={len(r['stdout'])}")

        r, _ = rpc("import os; print(os.getuid(), os.environ.get('HOME'))")
        check("utente non root", r["stdout"].split()[0] == "1000",
              r["stdout"].strip())

    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.wait(timeout=20)

    print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

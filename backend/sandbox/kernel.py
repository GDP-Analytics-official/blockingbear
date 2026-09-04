"""Kernel stateful della sandbox (gira DENTRO il container, non sul server).

Protocollo JSON-lines su stdin/stdout del processo principale del container:
il backend scrive una riga
    {"code": "...", "timeout": 120}
e legge una riga di risposta
    {"stdout": "...", "stderr": "...", "outcome": "ok|error|timeout",
     "new_files": ["outputs/report.xlsx"], "elapsed_ms": 1840}

All'avvio il kernel emette una riga di handshake {"ready": true, ...} che il
SandboxManager usa per sapere che il container è pronto.

Lo stato (variabili, DataFrame) vive nel namespace di questo processo e
persiste tra le esecuzioni. Il timeout usa SIGALRM: interrompe il codice
Python puro e le attese di subprocess; NON interrompe loop chiusi dentro
estensioni C (caso raro) — per quello il SandboxManager ha il fallback duro:
nessuna risposta entro timeout+grazia -> kill del container.

I fd 0/1 del container sono il canale del protocollo: vengono duplicati e poi
rimpiazzati, così il codice utente (e i subprocess come soffice) scrivono su
file di cattura e leggono EOF da /dev/null. Il protocollo non è corrompibile
da un print().
"""

import ast
import json
import os
import shutil
import signal
import sys
import time
import traceback

WORKSPACE = "/workspace"
OUTPUTS_DIR = os.path.join(WORKSPACE, "outputs")
STDOUT_LIMIT = 8000       # char nel tool result; oltre = troncato con marcatore
STDERR_LIMIT = 4000       # per gli errori conta la coda (ultimi frame)
CAP_OUT = "/tmp/.kernel_cap_out"
CAP_ERR = "/tmp/.kernel_cap_err"

# Risorse pre-generate nella build dell'immagine, copiate su tmpfs all'avvio
# perché il rootfs è --read-only (il wrapper /usr/local/bin/soffice punta
# già a /tmp/lo-profile, MPLCONFIGDIR punta già a /tmp/mpl).
BAKED = [("/opt/lo-profile", "/tmp/lo-profile"), ("/opt/mpl-cache", "/tmp/mpl")]


class _Timeout(Exception):
    pass


def _on_alarm(signum, frame):
    raise _Timeout()


def _setup_channels():
    """Sequestra stdin/stdout per il protocollo e reindirizza i fd 0/1/2."""
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8", errors="replace")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8")

    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)

    cap_out = os.open(CAP_OUT, os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o600)
    cap_err = os.open(CAP_ERR, os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o600)
    os.dup2(cap_out, 1)
    os.dup2(cap_err, 2)
    os.close(cap_out)
    os.close(cap_err)

    # riallinea gli oggetti Python ai nuovi fd (line buffered)
    sys.stdout = os.fdopen(1, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False)
    sys.stderr = os.fdopen(2, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False)
    sys.stdin = open(os.devnull, "r")
    return proto_in, proto_out


def _reset_capture():
    for fd in (1, 2):
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)


def _read_capture():
    sys.stdout.flush()
    sys.stderr.flush()
    out = open(CAP_OUT, "r", encoding="utf-8", errors="replace").read()
    err = open(CAP_ERR, "r", encoding="utf-8", errors="replace").read()
    return out, err


def _truncate_head(s, limit):
    if len(s) <= limit:
        return s
    return s[:limit] + "\n…[stdout troncato: {} caratteri totali]…".format(len(s))


def _truncate_tail(s, limit):
    if len(s) <= limit:
        return s
    return "…[stderr troncato: {} caratteri totali]…\n".format(len(s)) + s[-limit:]


def _snapshot_outputs():
    snap = {}
    for root, _dirs, files in os.walk(OUTPUTS_DIR):
        for name in files:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            rel = os.path.relpath(path, WORKSPACE)
            snap[rel] = (st.st_size, st.st_mtime_ns)
    return snap


def _execute(code, namespace):
    """exec con echo dell'ultima espressione (stile Jupyter)."""
    tree = ast.parse(code, mode="exec")
    last_expr = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = ast.Expression(tree.body.pop().value)
    if tree.body:
        exec(compile(tree, "<cell>", "exec"), namespace)
    if last_expr is not None:
        value = eval(compile(last_expr, "<cell>", "eval"), namespace)
        if value is not None:
            print(repr(value))


def _format_user_traceback(exc):
    # salta i frame del kernel (questo file) per mostrare solo il codice utente
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename == __file__:
        tb = tb.tb_next
    return "".join(traceback.format_exception(type(exc), exc, tb))


def main():
    proto_in, proto_out = _setup_channels()

    os.makedirs("/tmp/home", exist_ok=True)
    # /workspace e' un tmpfs privato per-container. Creare qui le due superfici
    # esplicite mantiene rootfs read-only e impedisce bind path dipendenti
    # dall'host; il backend scambia i file con stream `docker exec`.
    os.makedirs(os.path.join(WORKSPACE, "inputs"), exist_ok=True)
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    for src, dst in BAKED:
        if os.path.isdir(src) and not os.path.isdir(dst):
            try:
                shutil.copytree(src, dst)
            except OSError:
                pass  # senza profilo caldo funziona comunque, solo più lento

    signal.signal(signal.SIGALRM, _on_alarm)
    namespace = {"__name__": "__main__", "__builtins__": __builtins__}

    proto_out.write(json.dumps({
        "ready": True,
        "python": sys.version.split()[0],
        "pid": os.getpid(),
    }) + "\n")
    proto_out.flush()

    for line in proto_in:
        line = line.strip()
        if not line:
            continue
        started = time.perf_counter()
        try:
            request = json.loads(line)
            code = request["code"]
            timeout = max(1, int(request.get("timeout", 120)))
        except (ValueError, KeyError, TypeError) as exc:
            proto_out.write(json.dumps({
                "stdout": "", "stderr": "richiesta non valida: {}".format(exc),
                "outcome": "error", "new_files": [], "elapsed_ms": 0,
            }) + "\n")
            proto_out.flush()
            continue

        before = _snapshot_outputs()
        _reset_capture()
        outcome = "ok"
        try:
            signal.alarm(timeout)
            _execute(code, namespace)
        except _Timeout:
            outcome = "timeout"
            print("\nEsecuzione interrotta: timeout di {} s superato.".format(timeout),
                  file=sys.stderr)
        except BaseException as exc:  # anche SystemExit/KeyboardInterrupt: il kernel resta vivo
            outcome = "error"
            sys.stderr.write(_format_user_traceback(exc))
        finally:
            signal.alarm(0)

        out, err = _read_capture()
        after = _snapshot_outputs()
        new_files = sorted(rel for rel, sig in after.items() if before.get(rel) != sig)

        proto_out.write(json.dumps({
            "stdout": _truncate_head(out, STDOUT_LIMIT),
            "stderr": _truncate_tail(err, STDERR_LIMIT),
            "outcome": outcome,
            "new_files": new_files,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }, ensure_ascii=False) + "\n")
        proto_out.flush()


if __name__ == "__main__":
    main()

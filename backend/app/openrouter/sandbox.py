"""SandboxManager: ciclo di vita dei container del code interpreter.

Ogni conversazione della chat LLM può avere UN container (immagine
backend/sandbox/, kernel stateful JSON-lines su stdin/stdout) dove il modello
esegue codice Python sui file dell'utente.

Regole del ciclo di vita:
  - warm pool di container VERGINI pre-avviati (SANDBOX_POOL): l'assegnazione
    alla prima esecuzione è istantanea e il rimpiazzo parte subito in
    background; pool momentaneamente vuoto = partenza a freddo (~1-3 s);
  - un container per CONVERSAZIONE, mai riusato tra conversazioni (lo stato
    del kernel è parte della conversazione: riciclarlo farebbe trovare a una
    chat i dati di un'altra) — usato -> distrutto, il pool si rifornisce solo
    di container nuovi;
  - idle timeout SANDBOX_IDLE_MIN dall'ULTIMA esecuzione, solo per i container
    assegnati (il pool non scade, solo riciclo periodico); tetto SANDBOX_MAX
    di assegnati con eviction LRU;
  - kernel morto o esecuzione che non risponde entro timeout+grazia (loop
    chiusi in estensioni C che SIGALRM non interrompe) -> kill del container,
    ricreazione immediata dal pool e outcome "kernel_restarted" al modello.

Il runtime (docker/podman) si rileva all'avvio: se manca, execute() solleva
SandboxUnavailable e la chat degrada con eleganza (la sandbox è una feature
opzionale, mai un requisito). Chi chiama (il loop di tool calling in chat.py)
è codice async: usare `await asyncio.to_thread(sandbox.execute, ...)` — qui
tutto è sincrono e thread-safe, stesso stile di jobs.py.

Ogni container ha un workspace tmpfs privato. Il backend trasferisce solo i
file della conversazione attraverso stream ``docker exec`` e recupera allo
stesso modo gli artifact prodotti: nessun percorso host viene reinterpretato dal demone e una
sandbox non può vedere lo staging delle altre. Questo vale uguale con Docker
Engine Linux e col demone nella VM di Docker Desktop. Le copie host temporanee
restano in {DATA_DIR}/sandbox/{sid} e vengono cancellate col container.
"""

import atexit
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from ..config import (DATA_DIR, SANDBOX_ENGINE, SANDBOX_EXEC_TIMEOUT,
                      SANDBOX_IDLE_MIN, SANDBOX_IMAGE, SANDBOX_MAX,
                      SANDBOX_MEM, SANDBOX_POOL)
from ..logging_setup import get_logger

log = get_logger("blockingbear.sandbox")

_STAGING_ROOT = DATA_DIR / "sandbox"
_NAME_PREFIX = "blockingbear-sbx-"

_HANDSHAKE_TIMEOUT = 60     # s per il ready del kernel a container appena nato
_EXEC_GRACE = 10            # s oltre il timeout del kernel prima del kill duro
_REAP_INTERVAL = 60         # s tra due giri del reaper
_IDLE_SECONDS = SANDBOX_IDLE_MIN * 60
_POOL_MAX_AGE = 24 * 3600   # riciclo periodico dei container in pool

_RESTART_NOTICE = ("Ambiente di esecuzione ricreato: lo stato precedente "
                   "(variabili, DataFrame) è andato perso. I file di input "
                   "sono di nuovo disponibili in /workspace/inputs: ricarica "
                   "ciò che ti serve.")


class SandboxError(RuntimeError):
    """Guasto del runtime container (creazione fallita, immagine assente...)."""


class SandboxUnavailable(SandboxError):
    """Nessun runtime container sul server: la chat degrada senza sandbox."""


class _Container:
    """Un container vivo: attach al kernel, staging, stato d'uso."""

    def __init__(self, sid, proc, root):
        self.sid = sid
        self.name = _NAME_PREFIX + sid
        self.proc = proc                    # il client `docker run -i` attaccato
        self.root = root                    # {DATA_DIR}/sandbox/{sid}
        self.inputs = root / "inputs"
        self.outputs = root / "outputs"
        self.lines = queue.Queue()          # righe di risposta del kernel
        self.err_tail = deque(maxlen=30)    # coda di stderr del client (diagnosi)
        self.created_at = time.monotonic()
        self.last_used = time.monotonic()
        self.lock = threading.Lock()        # una esecuzione alla volta per kernel
        self.staged = {}                    # nome file -> firma di ciò che è già dentro


# --- Stato del modulo (stesso stile di jobs.py) ------------------------------

_lock = threading.Lock()
_engine = None                  # "docker" | "podman" | None (assente o off)
_started = False
_detecting = False              # True mentre il bootstrap rileva il runtime
_closing = False                # shutdown in corso: niente nuovi container
_smoke_tested = False           # almeno un kernel ha eseguito codice + bind output
_pool = []                      # container vergini pronti
_assigned = {}                  # conv_id -> _Container
_state_lost = set()             # conversazioni il cui container è stato spento:
                                # la prossima esecuzione avvisa il modello
_pool_event = threading.Event() # sveglia il filler (rimpiazzo asincrono)


# --- Runtime ------------------------------------------------------------------

def _detect_engine():
    """Rileva il runtime container disponibile. `info` interroga il demone:
    un CLI installato ma col demone spento (Docker Desktop chiuso) fallisce,
    che è esattamente ciò che vogliamo sapere."""
    if SANDBOX_ENGINE == "off":
        return None
    candidates = ([SANDBOX_ENGINE] if SANDBOX_ENGINE in ("docker", "podman")
                  else ["docker", "podman"])
    for exe in candidates:
        try:
            r = subprocess.run([exe, "info"], capture_output=True, timeout=20)
            if r.returncode == 0:
                return exe
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def _run_cmd(name):
    """Il docker run della sandbox: niente rete, filesystem in sola
    lettura, workspace tmpfs privato, limiti di risorse, niente privilegi,
    utente non root. Niente bind di percorsi host: i file passano con docker
    exec, che funziona anche quando il demone vive nella VM di Docker Desktop."""
    return [
        _engine, "run", "--rm", "-i", "--name", name,
        "--network=none",
        "--read-only", "--tmpfs", "/tmp:size=256m",
        "--tmpfs", "/workspace:rw,size=512m,uid=1000,gid=1000,mode=0700",
        # Il processo sandbox vede gli input read-only. Solo il breve exec
        # controllato di _copy_into_container riceve privilegi per scriverli.
        "--tmpfs", "/workspace/inputs:rw,size=256m,uid=0,gid=0,mode=0555",
        f"--memory={SANDBOX_MEM}", "--cpus=1", "--pids-limit=256",
        # DAC_OVERRIDE resta nel bounding set solo per il brevissimo exec root
        # che alimenta il tmpfs input 0555. Il kernel gira uid 1000 con CapEff=0.
        "--cap-drop=ALL", "--cap-add=DAC_OVERRIDE",
        "--security-opt", "no-new-privileges",
        "--user", "1000:1000",
        SANDBOX_IMAGE,
    ]


def _copy_into_container(name, source, destination):
    """Trasmette un file al tmpfs privato del container attraverso stdin."""
    try:
        with open(source, "rb") as stream:
            r = subprocess.run(
                [_engine, "exec", "--user", "0", "-i", name, "/bin/sh", "-c",
                 'cat > "$1"', "blockingbear-copy", str(destination)],
                stdin=stream, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SandboxError(f"trasferimento input non eseguibile: {e}") from e
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or b"errore senza dettaglio").decode(
            "utf-8", errors="replace").strip()
        raise SandboxError(f"trasferimento input fallito: {detail}")


def _copy_from_container(name, source, destination):
    """Recupera un file dal tmpfs privato senza farne risolvere il path al daemon.

    ``docker cp`` non include i tmpfs montati; ``docker exec cat`` legge invece
    il namespace reale del container e conserva byte arbitrari nello stream.
    """
    try:
        with open(destination, "wb") as stream:
            r = subprocess.run([_engine, "exec", name, "cat", str(source)],
                               stdout=stream, stderr=subprocess.PIPE,
                               timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        Path(destination).unlink(missing_ok=True)
        raise SandboxError(f"trasferimento output non eseguibile: {e}") from e
    if r.returncode != 0:
        Path(destination).unlink(missing_ok=True)
        detail = (r.stderr or b"errore senza dettaglio").decode(
            "utf-8", errors="replace").strip()
        raise SandboxError(f"trasferimento output fallito: {detail}")


def _pump(stream, sink):
    """Travasa le righe di uno stream del client in una coda/deque; il
    sentinel None segnala l'EOF (container uscito)."""
    for line in stream:
        sink(line)
    sink(None)


def _spawn():
    """Crea un container vergine e ne prova davvero kernel e trasferimento file.

    L'handshake da solo dimostra soltanto che ``kernel.py`` e' partito. Prima
    di pubblicare il container nel pool eseguiamo una cella sintetica e
    copiamo dal container il file scritto in ``outputs``: cosi' un Docker API
    non funzionante o un kernel che non esegue codice non possono produrre
    ``available: true``. Lento (~1-3 s), mai sotto _lock.
    """
    global _smoke_tested
    sid = uuid.uuid4().hex[:12]
    root = _STAGING_ROOT / sid
    (root / "inputs").mkdir(parents=True, exist_ok=True)
    (root / "outputs").mkdir(parents=True, exist_ok=True)
    try:
        # Le copie locali appartengono allo stesso uid del backend. Il workspace
        # del kernel e' invece un tmpfs privato creato da _run_cmd.
        os.chmod(root / "outputs", 0o777)
    except OSError:
        pass

    proc = subprocess.Popen(
        _run_cmd(_NAME_PREFIX + sid),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    c = _Container(sid, proc, root)
    threading.Thread(target=_pump, args=(proc.stdout, c.lines.put),
                     daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr, c.err_tail.append),
                     daemon=True).start()

    deadline = time.monotonic() + _HANDSHAKE_TIMEOUT
    while True:
        try:
            line = c.lines.get(timeout=max(0.1, deadline - time.monotonic()))
        except queue.Empty:
            line = None
        if line is None:
            _destroy(c)
            tail = " | ".join(l.strip() for l in c.err_tail if l)
            raise SandboxError(
                f"Avvio del container sandbox fallito ({SANDBOX_IMAGE}): "
                f"{tail or 'nessun handshake dal kernel'}")
        try:
            if json.loads(line).get("ready"):
                break
        except ValueError:
            continue    # riga spuria del client, si ignora

    marker = c.outputs / ".blockingbear-sandbox-smoke"
    try:
        c.proc.stdin.write(json.dumps({
            # Solo builtin e nessuna assegnazione: il kernel e' persistente e
            # deve arrivare alla conversazione con un namespace ancora vergine.
            "code": ("open('/workspace/outputs/.blockingbear-sandbox-smoke', "
                     "'w').write('42'); print(6 * 7)"),
            "timeout": 10,
        }) + "\n")
        c.proc.stdin.flush()
        line = c.lines.get(timeout=10 + _EXEC_GRACE)
        result = json.loads(line) if line is not None else {}
        _copy_from_container(
            c.name, "/workspace/outputs/.blockingbear-sandbox-smoke", marker)
        mount_value = marker.read_text(encoding="utf-8")
        marker.unlink(missing_ok=True)
        if (result.get("outcome") != "ok" or
                result.get("stdout", "").strip() != "42" or
                mount_value != "42"):
            raise ValueError(f"risposta inattesa: {result!r}")
    except (OSError, ValueError, json.JSONDecodeError, queue.Empty) as e:
        _destroy(c)
        raise SandboxError(
            "Smoke test del container sandbox fallito: esecuzione Python o "
            f"trasferimento output non funzionante ({e})") from e
    with _lock:
        _smoke_tested = True
    return c


def _destroy(c):
    """Spegne il container e ne cancella lo staging. Idempotente, non solleva:
    si chiama anche nei percorsi d'errore. Lenta (subprocess): mai sotto _lock."""
    try:
        subprocess.run([_engine, "rm", "-f", c.name],
                       capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        c.proc.stdin.close()
        c.proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
    shutil.rmtree(c.root, ignore_errors=True)


def _sweep_orphans():
    """Al boot: container blockingbear-sbx-* superstiti di un processo precedente
    (crash del backend) e staging orfani si eliminano."""
    try:
        r = subprocess.run([_engine, "ps", "-aq", "--filter",
                            f"name={_NAME_PREFIX}"],
                           capture_output=True, text=True, timeout=30)
        ids = r.stdout.split()
        if ids:
            subprocess.run([_engine, "rm", "-f", *ids],
                           capture_output=True, timeout=60)
            log.info(f"rimossi {len(ids)} container orfani")
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning(f"sweep orfani fallito: {e!r}")
    if _STAGING_ROOT.is_dir():
        for child in _STAGING_ROOT.iterdir():
            shutil.rmtree(child, ignore_errors=True)


# --- Warm pool e reaper -------------------------------------------------------

def _filler():
    """Tiene il pool a SANDBOX_POOL container vergini. Si sveglia a ogni
    assegnazione (rimpiazzo asincrono) e comunque una volta al minuto."""
    while True:
        _pool_event.wait(timeout=60)
        _pool_event.clear()
        while True:
            with _lock:
                if _closing:
                    return
                if len(_pool) >= SANDBOX_POOL:
                    break
            try:
                c = _spawn()
            except SandboxError as e:
                log.warning(f"rifornimento pool fallito: {e}")
                break               # si riprova alla prossima sveglia
            with _lock:
                if not _closing and len(_pool) < SANDBOX_POOL:
                    _pool.append(c)
                    c = None
            if c is not None:       # overshoot o chiusura in corso: via
                _destroy(c)


def _reaper():
    """Spegne i container assegnati fermi da più di _IDLE_SECONDS (il timer
    riparte a ogni esecuzione) e ricicla i container in pool troppo vecchi."""
    while True:
        time.sleep(_REAP_INTERVAL)
        now = time.monotonic()
        doomed = []
        with _lock:
            for conv_id, c in list(_assigned.items()):
                # un kernel in piena esecuzione non è idle per definizione
                if now - c.last_used > _IDLE_SECONDS and not c.lock.locked():
                    _assigned.pop(conv_id)
                    _state_lost.add(conv_id)
                    doomed.append(c)
            for c in list(_pool):
                if now - c.created_at > _POOL_MAX_AGE:
                    _pool.remove(c)
                    doomed.append(c)
        for c in doomed:
            _destroy(c)
        if doomed:
            log.info(f"reaper: spenti {len(doomed)} container")
            _pool_event.set()


def _acquire(conv_id):
    """Assegna un container alla conversazione: dal pool se possibile
    (istantaneo), altrimenti a freddo. Applica il tetto SANDBOX_MAX con
    eviction del meno usato di recente."""
    with _lock:
        c = _pool.pop(0) if _pool else None
    _pool_event.set()               # rimpiazzo asincrono, parte subito
    if c is None:
        c = _spawn()                # pool vuoto (raffica): 1-3 s, accettabile

    evicted = None
    with _lock:
        existing = _assigned.get(conv_id)
        if existing is not None:
            # due richieste della stessa conversazione in corsa: vince la
            # prima; quello preparato qui è ancora vergine, torna in pool
            if len(_pool) < SANDBOX_POOL:
                _pool.append(c)
            else:
                evicted = c
            c = existing
        else:
            if len(_assigned) >= SANDBOX_MAX:
                # LRU tra i non impegnati; se lavorano tutti, LRU assoluto
                # (il tetto protegge la RAM del server, ha la precedenza)
                lru = sorted(_assigned, key=lambda k: _assigned[k].last_used)
                victim = next((k for k in lru
                               if not _assigned[k].lock.locked()), lru[0])
                evicted = _assigned.pop(victim)
                _state_lost.add(victim)
            _assigned[conv_id] = c
            c.last_used = time.monotonic()
    if evicted is not None:
        _destroy(evicted)
    return c


# --- Staging ------------------------------------------------------------------

def _stage(c, files):
    """Copia in inputs i file mancanti o cambiati. `files` è l'elenco
    COMPLETO degli allegati della conversazione {nome: bytes|percorso}: si
    passa a ogni esecuzione, la firma evita ricopiature e su un container
    nuovo (dopo un restart) rimette tutto a posto da sola."""
    for name, src in files.items():
        safe = os.path.basename(str(name).replace("\\", "/"))
        if not safe:
            continue
        dst = c.inputs / safe
        if isinstance(src, (bytes, bytearray)):
            sig = ("bytes", len(src))
            if c.staged.get(safe) == sig and dst.exists():
                continue
            dst.write_bytes(src)
        else:
            path = Path(src)
            st = path.stat()
            sig = ("file", st.st_size, st.st_mtime_ns)
            if c.staged.get(safe) == sig and dst.exists():
                continue
            shutil.copyfile(path, dst)
        _copy_into_container(c.name, dst, f"/workspace/inputs/{safe}")
        c.staged[safe] = sig


def _collect_files(c, new_files):
    """Traduce i percorsi relativi del kernel (es. "outputs/report.xlsx") nei
    percorsi HOST dentro lo staging, da cui il chiamante raccoglie gli
    artifact prima che il container venga distrutto."""
    out = {}
    for rel in new_files:
        parts = rel.split("/", 1)
        if len(parts) == 2 and parts[0] == "outputs":
            host = (c.outputs / parts[1]).resolve()
            if not host.is_relative_to(c.outputs.resolve()):
                continue
            host.parent.mkdir(parents=True, exist_ok=True)
            try:
                _copy_from_container(c.name, f"/workspace/{rel}", host)
            except SandboxError as e:
                log.warning(f"artifact sandbox non recuperabile ({rel}): {e}")
                continue
            if host.is_file():
                out[rel] = str(host)
    return out


# --- API per il loop di chat ---------------------------------------------------

def execute(conv_id, code, timeout=None, files=None):
    """Esegue `code` nel container della conversazione (assegnandone uno se
    serve) e ritorna il risultato del kernel arricchito:

        {"stdout", "stderr", "outcome": "ok|error|timeout|kernel_restarted",
         "new_files": [rel...], "files": {rel: percorso host}, "elapsed_ms",
         "notice": presente solo se l'ambiente è stato ricreato}

    Bloccante (dal codice async: asyncio.to_thread). Solleva
    SandboxUnavailable se il runtime manca, SandboxError se la creazione del
    container fallisce."""
    if _engine is None or _closing:
        raise SandboxUnavailable(
            "Nessun runtime container disponibile: sandbox disattivata.")
    timeout = int(timeout or SANDBOX_EXEC_TIMEOUT)

    notice = None
    with _lock:
        c = _assigned.get(conv_id)
    if c is None:
        c = _acquire(conv_id)
        with _lock:
            if conv_id in _state_lost:
                _state_lost.discard(conv_id)
                notice = _RESTART_NOTICE

    with c.lock:
        if files:
            _stage(c, files)
        try:
            c.proc.stdin.write(json.dumps({"code": code, "timeout": timeout})
                               + "\n")
            c.proc.stdin.flush()
        except OSError:
            return _kernel_died(conv_id, c, notice)
        try:
            line = c.lines.get(timeout=timeout + _EXEC_GRACE)
        except queue.Empty:
            line = None                 # bloccato oltre timeout+grazia (loop C)
        if line is None:
            return _kernel_died(conv_id, c, notice)
        result = json.loads(line)
        c.last_used = time.monotonic()

    result["files"] = _collect_files(c, result.get("new_files", ()))
    if notice:
        result["notice"] = notice
    return result


def _kernel_died(conv_id, c, notice):
    """Kernel morto o irraggiungibile: container via, se ne prepara subito uno
    nuovo e si riferisce l'accaduto al modello, che ricaricherà
    i dati (i file verranno ri-stagiati alla prossima esecuzione)."""
    with _lock:
        if _assigned.get(conv_id) is c:
            _assigned.pop(conv_id)
    _destroy(c)
    try:
        _acquire(conv_id)
        with _lock:
            _state_lost.discard(conv_id)    # l'avviso lo diamo già qui sotto
    except SandboxError as e:
        log.warning(f"ricreazione dopo crash fallita: {e}")
    return {
        "stdout": "",
        "stderr": ("L'esecuzione ha bloccato o terminato il kernel (memoria "
                   "esaurita o loop non interrompibile). " + _RESTART_NOTICE),
        "outcome": "kernel_restarted",
        "new_files": [], "files": {}, "elapsed_ms": 0,
        **({"notice": notice} if notice else {}),
    }


def release(conv_id):
    """Spegne il container della conversazione (es. conversazione eliminata).
    Nessun avviso al modello: la conversazione non esiste più."""
    with _lock:
        c = _assigned.pop(conv_id, None)
        _state_lost.discard(conv_id)
    if c is not None:
        _destroy(c)


def status():
    """Per /api/health e il pannello admin."""
    with _lock:
        return {
            "engine": _engine,
            "available": _engine is not None,
            "detecting": _detecting,
            "smoke_tested": _smoke_tested,
            "image": SANDBOX_IMAGE,
            "pool_ready": len(_pool),
            "assigned": len(_assigned),
        }


def shutdown():
    """Spegne tutto (chiusura del processo). I container eventualmente
    superstiti li raccoglie lo sweep del prossimo avvio."""
    global _closing
    with _lock:
        _closing = True
        doomed = list(_pool) + list(_assigned.values())
        _pool.clear()
        _assigned.clear()
        _state_lost.clear()
    _pool_event.set()               # il filler si accorge della chiusura ed esce
    for c in doomed:
        _destroy(c)


def start():
    """Da chiamare nello startup FastAPI. Non blocca: la rilevazione del
    runtime e il riempimento del pool avvengono in background."""
    global _started, _detecting
    with _lock:
        if _started:
            return
        _started = True
        _detecting = True
    threading.Thread(target=_bootstrap, daemon=True).start()


def _bootstrap():
    global _engine, _detecting
    engine = _detect_engine()
    with _lock:
        _engine = engine
        _detecting = False
    if engine is None:
        log.warning("nessun runtime container: sandbox disattivata "
                    f"(BLOCKINGBEAR_SANDBOX_ENGINE={SANDBOX_ENGINE})")
        return
    log.info(f"runtime: {engine}, immagine: {SANDBOX_IMAGE}, "
             f"pool: {SANDBOX_POOL}, max assegnati: {SANDBOX_MAX}")
    _STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    _sweep_orphans()
    atexit.register(shutdown)
    # Con pool=0 il primo container nascerebbe soltanto alla prima tool call.
    # Il deployment verifier deve poter provare la sandbox anche in questa
    # configurazione senza inviare una chat reale.
    if SANDBOX_POOL == 0:
        try:
            probe = _spawn()
            _destroy(probe)
        except SandboxError as e:
            log.warning(f"smoke test sandbox fallito: {e}")
    threading.Thread(target=_filler, daemon=True).start()
    threading.Thread(target=_reaper, daemon=True).start()
    _pool_event.set()

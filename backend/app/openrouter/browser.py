"""Browser web per i tool di ricerca.

Il backend NON dipende da camofox in giro per il codice: questo modulo è
l'unico che ne conosce l'API. L'interfaccia interna è `search(query)` /
`read(url)`: se camofox cambia o muore, si sostituisce il backend di queste
due funzioni (per esempio col pacchetto Python camoufox + Playwright), non
l'app.

Ciclo di vita OPPOSTO alla sandbox: UN container singleton
(`blockingbear-camofox`, immagine ufficiale ghcr.io/redf0x1/camofox-browser),
avvio lazy al primo uso, bind su 127.0.0.1, API key generata al volo e
passata via env, health check e restart se muore. Niente pool, niente
container per conversazione: l'isolamento tra conversazioni è una TAB per
chiamata (apri -> usa -> chiudi), stateless.

In alternativa BLOCKINGBEAR_CAMOFOX_URL punta a un'istanza esterna già avviata
(il backend non ne gestisce il ciclo di vita). Il runtime container è lo
stesso rilevato da sandbox.py (_detect_engine): niente doppia rilevazione.

Tutto il modulo è SINCRONO e thread-safe (stile sandbox.py): chi chiama dal
codice async usa `await asyncio.to_thread(...)`. Ogni guasto esce come
BrowserError con un messaggio parlante: i handler dei tool lo trasformano in
tool result d'errore, mai in un'eccezione che uccide il turno.
"""

import atexit
import ipaddress
import json
import re
import socket
import subprocess
import threading
import time
import uuid
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

from ..config import (CAMOFOX_API_KEY, CAMOFOX_IMAGE, CAMOFOX_MEM,
                      CAMOFOX_PORT, CAMOFOX_URL, WEB_SEARCH_URL)
from ..logging_setup import get_logger
from . import sandbox

log = get_logger("blockingbear.browser")

_NAME = "blockingbear-camofox"
_USER = "blockingbear"
_HEALTH_TIMEOUT = 90        # s per il ready del server a container appena nato
_REQUEST_TIMEOUT = 45       # s per una singola chiamata REST (navigate ~30s)
_PDF_MAX_BYTES = 30 * 1024 * 1024

_lock = threading.Lock()    # avvio/restart del container: uno alla volta
_started = False            # container avviato da questo processo
_api_key = CAMOFOX_API_KEY
_base_url = CAMOFOX_URL     # istanza esterna, se configurata
_last_error = None          # ultima diagnosi di avvio fallito (per status())
_keeper = None              # tab di ancoraggio mai chiusa (vedi _ensure_keeper)


class BrowserError(RuntimeError):
    """Guasto del browser (container assente, navigazione fallita, timeout).
    Il messaggio è pensato per il modello: dice cosa è andato storto e cosa
    ha senso fare (riprovare, cambiare URL, rispondere senza web)."""


def _engine():
    """Il runtime container, riusando la rilevazione della sandbox: se il
    bootstrap della sandbox è già passato vale il suo esito, altrimenti si
    rileva qui (una volta) con la stessa funzione."""
    if sandbox._engine is not None:
        return sandbox._engine
    if sandbox._started and not sandbox._detecting:
        return None                     # rilevazione fatta: non c'è niente
    return sandbox._detect_engine()


def available():
    """Il tool web si può dichiarare? Ottimista come la sandbox: basta che
    un'istanza esterna sia configurata o che un runtime container esista —
    l'avvio vero è lazy e un eventuale guasto arriva al modello come tool
    result d'errore."""
    if _base_url:
        return True
    return _engine() is not None


def status():
    """Per /api/openrouter/status e il pannello admin."""
    return {
        "available": available(),
        "running": _started or (_base_url is not None),
        "external": _base_url is not None,
        "image": CAMOFOX_IMAGE,
        "error": _last_error,
    }


# --- Ciclo di vita del container -------------------------------------------------

def _http(method, path, payload=None, timeout=_REQUEST_TIMEOUT):
    headers = {}
    if _api_key:
        headers["Authorization"] = f"Bearer {_api_key}"
    url = f"{_base_url}{path}"
    r = httpx.request(method, url, json=payload, headers=headers,
                      timeout=timeout)
    if r.status_code >= 400:
        try:
            detail = r.json().get("error") or r.text
        except ValueError:
            detail = r.text
        raise BrowserError(f"camofox {method} {path}: HTTP {r.status_code} "
                           f"({str(detail)[:200]})")
    return r.json()


def _healthy():
    try:
        r = httpx.get(f"{_base_url}/health", timeout=5)
        return r.status_code < 500
    except httpx.HTTPError:
        return False


def _spawn():
    """Avvia il container singleton (rimuovendo un eventuale superstite di un
    processo precedente: la API key cambia a ogni avvio) e attende l'health
    check. Lento (~secondi): mai sotto il lock di chi fa richieste brevi."""
    global _api_key, _base_url, _started, _last_error, _keeper
    _keeper = None
    engine = _engine()
    if engine is None:
        raise BrowserError("Nessun runtime container disponibile: "
                           "ricerca web disattivata.")
    _api_key = uuid.uuid4().hex
    subprocess.run([engine, "rm", "-f", _NAME], capture_output=True, timeout=30)
    cmd = [
        engine, "run", "-d", "--rm", "--name", _NAME,
        "-p", f"127.0.0.1:{CAMOFOX_PORT}:9377",
        "-e", "CAMOFOX_HOST=0.0.0.0",
        "-e", "CAMOFOX_AUTH_MODE=required",
        "-e", f"CAMOFOX_API_KEY={_api_key}",
        f"--memory={CAMOFOX_MEM}",
        "--security-opt", "no-new-privileges",
        CAMOFOX_IMAGE,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BrowserError(f"Avvio del browser web fallito: {e}")
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip()[-300:]
        _last_error = tail
        raise BrowserError(f"Avvio del browser web fallito ({CAMOFOX_IMAGE}): "
                           f"{tail}")
    _base_url = f"http://127.0.0.1:{CAMOFOX_PORT}"
    deadline = time.monotonic() + _HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        if _healthy():
            _started = True
            _last_error = None
            atexit.register(shutdown)
            log.info(f"camofox avviato ({CAMOFOX_IMAGE}) su {_base_url}")
            return
        time.sleep(0.5)
    subprocess.run([engine, "rm", "-f", _NAME], capture_output=True, timeout=30)
    _last_error = "health check scaduto"
    raise BrowserError("Il browser web non ha risposto all'health check "
                       f"entro {_HEALTH_TIMEOUT} s.")


def _ensure():
    """Il server camofox è su e risponde; se il container gestito è morto lo
    si riavvia (una volta). Da chiamare prima di ogni operazione."""
    global _started
    with _lock:
        if _base_url and _healthy():
            _ensure_keeper()
            return
        if CAMOFOX_URL:
            # istanza esterna: il ciclo di vita non è nostro, si riferisce
            raise BrowserError(
                f"Il browser web esterno ({CAMOFOX_URL}) non risponde.")
        _started = False
        _spawn()
        _ensure_keeper()


def _ensure_keeper():
    """Tiene aperta una tab di ancoraggio che non si chiude mai: la chiusura
    dell'ULTIMA tab smonta il contesto in modo asincrono e uccide le tab nate
    in quella finestra: dopo una chiusura le chiamate concorrenti muoiono
    tutte con "Tab not found" o con un result non valido.
    Finché l'ancora vive, nessuna chiusura di tab di lavoro è "l'ultima".
    Best-effort: se fallisce ci pensa il retry di _retry_transient."""
    global _keeper
    if _keeper is not None:
        return
    try:
        out = _http("POST", "/tabs", {"userId": _USER,
                                      "sessionKey": "keeper"})
        _keeper = out.get("tabId")
    except (BrowserError, httpx.HTTPError):
        _keeper = None


def shutdown():
    """Spegne il container gestito (chiusura del processo). Idempotente."""
    global _started, _keeper
    with _lock:
        _keeper = None
        if not _started or CAMOFOX_URL:
            return
        _started = False
        engine = _engine()
    if engine:
        subprocess.run([engine, "rm", "-f", _NAME],
                       capture_output=True, timeout=30)


# --- Una tab per chiamata ---------------------------------------------------------

class _Tab:
    """Apri -> usa -> chiudi: nessuno stato condiviso tra conversazioni."""

    def __enter__(self):
        # niente url alla creazione (nasce su about:blank): il server accetta
        # solo http/https come destinazioni esplicite. La chiusura dell'ULTIMA
        # tab smonta il contesto in modo asincrono, e una creazione arrivata
        # in quella finestra risponde 500 ("window is null", bug upstream):
        # un paio di retry brevi la coprono.
        out, last = None, None
        for attempt in range(3):
            try:
                out = _http("POST", "/tabs", {"userId": _USER,
                                              "sessionKey": uuid.uuid4().hex})
                break
            except BrowserError as e:
                last = e
                if "HTTP 5" not in str(e) or attempt == 2:
                    raise
                time.sleep(1.5)
        if out is None:
            raise last
        self.tab_id = out.get("tabId")
        if not self.tab_id:
            raise BrowserError("camofox non ha aperto la tab.")
        return self

    def __exit__(self, *exc):
        try:
            httpx.request("DELETE", f"{_base_url}/tabs/{self.tab_id}",
                          json={"userId": _USER},
                          headers=({"Authorization": f"Bearer {_api_key}"}
                                   if _api_key else {}),
                          timeout=15)
        except httpx.HTTPError:
            pass                        # tab orfana: la chiude il server

    def navigate(self, url):
        """Naviga e attende il DOM (il server usa domcontentloaded, 30 s
        cablati). Un timeout NON è fatale: la pagina caricata a metà spesso
        ha già tutto il testo — decide l'estrazione che segue. Sulle pagine
        molto pesanti il DOM è completo ben prima del load."""
        try:
            return _http("POST", f"/tabs/{self.tab_id}/navigate",
                         {"userId": _USER, "url": url})
        except httpx.TimeoutException:
            # timeout lato client (il server non risponde nemmeno col suo
            # 500 a 30 s): la tab è avvelenata come nel caso sotto, stessa
            # sorte — None, così read() scende su statico e terza gamba (è
            # la fine tipica dei portali di news pieni di tracker)
            return None
        except BrowserError as e:
            # il server risponde 500 sia per un timeout di caricamento sia
            # per una navigazione fallita, senza distinguerli nel body: si
            # lascia decidere all'estrazione (una tab rimasta su about:blank
            # = navigazione fallita, una pagina a metà ha già il testo)
            if "HTTP 5" in str(e):
                return None
            raise

    def evaluate(self, expression, timeout_ms=10000, http_timeout=60):
        # il server serializza le operazioni sulla stessa tab (withTabLock) e
        # una navigazione scaduta TIENE il lock finché la pagina non smette
        # di caricare: su una pagina molto pesante l'evaluate parte dopo oltre
        # un minuto, sui siti di news pieni di tracker non parte mai.
        # 60 s di attesa, poi ci pensa il ripiego statico di read().
        out = _http("POST", f"/tabs/{self.tab_id}/evaluate",
                    {"userId": _USER, "expression": expression,
                     "timeout": timeout_ms},
                    timeout=http_timeout)
        if out.get("ok") is False:
            # il server distingue timeout dell'esecuzione (main thread
            # strangolato dagli script: capita sulle news) e js_error: qui
            # NON è un troncamento, ridurre il taglio non aiuta — meglio
            # passare subito al ripiego statico
            raise BrowserError("Estrazione nella pagina fallita "
                               f"({out.get('errorType') or 'errore'}): "
                               f"{str(out.get('error'))[:120]}")
        return out.get("result"), bool(out.get("truncated"))


def _wrapped(exc, prefix):
    if isinstance(exc, BrowserError):
        return exc
    return BrowserError(f"{prefix}: {exc}")


# le firme di una tab uccisa dal teardown asincrono del contesto (o di una
# risposta corrotta dallo stesso stato): un ritento con tab fresca basta
_TRANSIENT = ("Tab not found", "troncata o non valida", "window is null")


def _retry_transient(fn):
    """Esegue `fn` (una sessione completa apri-tab -> usa -> chiudi) e la
    ritenta UNA volta sui guasti transitori, dopo aver ripiantato la tab di
    ancoraggio (che il server potrebbe aver reclamato)."""
    global _keeper
    try:
        return fn()
    except BrowserError as e:
        if not any(sig in str(e) for sig in _TRANSIENT):
            raise
        _keeper = None
        time.sleep(1.0)
        _ensure_keeper()
        return fn()


# --- Estrazione -------------------------------------------------------------------

# Il contenuto leggibile della pagina, estratto NEL browser (dove il DOM è
# renderizzato): via script/nav/boilerplate/elementi nascosti, link
# preservati in forma [testo](url). La tab è usa-e-getta, quindi si può
# mutare il DOM vivo (innerText è layout-aware solo lì). `cap` limita il
# risultato per stare nei limiti di /evaluate; `WAITMS` è l'attesa in-page
# dell'assestamento (0 sul percorso primario: il navigate ha già atteso).
#
# Il preambolo AMMUTOLISCE gli scheduler della pagina PRIMA di ogni altra
# cosa: il server camofox attribuisce all'evaluate qualunque timer/rAF
# programmato mentre il suo token è attivo e non risponde finché il
# conteggio non torna a zero — e sulle pagine vive (Clarity registra la
# sessione con un MutationObserver, i tracker si ri-schedulano a catena)
# QUALSIASI evaluate resta appeso per sempre, a caso: il client va in timeout
# dopo ~60 s a vuoto, e muoiono anche i poll che leggono solo readyState.
# Coi no-op nessuno può più accodare
# lavoro sul token: questo è l'UNICO evaluate ammesso su una pagina vera,
# e l'attesa dell'assestamento sta qui dentro (sleep sul setTimeout
# originale, conservato in window.__realSetTimeout: transita dal tracker ma
# si scarica da solo; il rendering React/fetch usa microtask e MessageChannel
# e sopravvive al silenziamento). La tab muore subito dopo: la pagina rotta
# non è un problema.
_EXTRACT_JS = """
(async () => {
  try {
    window.__realSetTimeout = window.__realSetTimeout
                              || window.setTimeout.bind(window);
    const noop = () => 0;
    window.setTimeout = noop; window.setInterval = noop;
    window.requestAnimationFrame = noop;
    window.queueMicrotask = () => {};
  } catch (e) {}
  const sleep = (ms) => new Promise((r) => window.__realSetTimeout(r, ms));
  const deadline = Date.now() + WAITMS;
  while (Date.now() < deadline) {
    const ready = document.readyState === 'complete';
    const text = document.body ? (document.body.innerText || '') : '';
    if (ready && text.trim()) break;
    await sleep(250);
  }
  const kill = ['script','style','noscript','iframe','svg','canvas','nav',
                'header','footer','aside','form','button','select','video',
                'audio','[hidden]','[aria-hidden="true"]'];
  for (const sel of kill) {
    try { document.querySelectorAll(sel).forEach(n => n.remove()); }
    catch (e) {}
  }
  const main = document.querySelector('main,article,[role="main"]')
               || document.body;
  if (main) {
    main.querySelectorAll('a[href]').forEach(a => {
      const href = a.href || '';
      const label = (a.textContent || '').trim().replace(/\\s+/g, ' ');
      if (/^https?:/i.test(href) && label && label.length < 200) {
        a.textContent = '[' + label + '](' + href + ')';
      }
    });
  }
  const raw = main ? main.innerText || '' : '';
  const text = raw.replace(/\\n{3,}/g, '\\n\\n').trim();
  return JSON.stringify({
    title: document.title || '',
    contentType: document.contentType || '',
    url: location.href,
    total: text.length,
    text: text.slice(0, CAP),
  });
})()
"""

# I risultati di DuckDuckGo HTML. Selettori del layout "html.duckduckgo.com"
# (stabile da anni); un motore diverso via BLOCKINGBEAR_WEB_SEARCH_URL usa il
# ripiego generico sui link della pagina.
_DDG_RESULTS_JS = """
(() => {
  const out = [];
  for (const r of document.querySelectorAll('.result')) {
    if (r.className.includes('result--ad')) continue;
    const a = r.querySelector('a.result__a');
    if (!a || !a.href) continue;
    const s = r.querySelector('.result__snippet');
    out.push({title: (a.textContent || '').trim(),
              url: a.href,
              snippet: s ? (s.textContent || '').trim() : ''});
  }
  return JSON.stringify(out.slice(0, 20));
})()
"""

_GENERIC_RESULTS_JS = """
(() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href^="http"]')) {
    const title = (a.textContent || '').trim().replace(/\\s+/g, ' ');
    if (!title || title.length < 15 || seen.has(a.href)) continue;
    seen.add(a.href);
    out.push({title, url: a.href, snippet: ''});
  }
  return JSON.stringify(out.slice(0, 20));
})()
"""


def _evaluate_json(tab, expression, **kw):
    result, truncated = tab.evaluate(expression, **kw)
    if truncated or not isinstance(result, str):
        raise BrowserError("Risposta del browser troncata o non valida.")
    try:
        return json.loads(result)
    except ValueError:
        raise BrowserError("Estrazione della pagina fallita (JSON non valido).")


def _extract(tab, cap, wait_ms=0):
    """L'unico evaluate ammesso su una pagina vera (vedi _EXTRACT_JS):
    ammutolisce la pagina, attende fino a `wait_ms` di assestamento in-page
    ed estrae. Il taglio si restringe finché la risposta non ce la fa; ma
    solo il TRONCAMENTO si cura riducendo il taglio — un evaluate fallito
    (timeout/js_error) fallirebbe uguale a ogni misura: si lascia decidere
    al chiamante (ripiego statico o ritento sul documento nuovo)."""
    for attempt_cap in (cap, 60000, 30000, 15000):
        js = (_EXTRACT_JS.replace("WAITMS", str(wait_ms))
              .replace("CAP", str(attempt_cap)))
        try:
            return _evaluate_json(tab, js, timeout_ms=wait_ms + 10000,
                                  http_timeout=wait_ms // 1000 + 30)
        except BrowserError as e:
            if attempt_cap == 15000 or "troncata o non valida" not in str(e):
                raise


def _clean_result_url(url):
    """DuckDuckGo HTML fa passare i risultati da un redirect
    (duckduckgo.com/l/?uddg=<url vero>): al modello serve l'URL vero."""
    try:
        parsed = urlparse(url)
        if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
            uddg = parse_qs(parsed.query).get("uddg")
            if uddg:
                return uddg[0]
    except ValueError:
        pass
    return url


# --- API per i tool ---------------------------------------------------------------

def search(query, max_results=5):
    """Cerca sul web e ritorna [{title, url, snippet}] (al più max_results).
    Il motore è WEB_SEARCH_URL (default DuckDuckGo HTML). Bloccante."""
    _ensure()
    target = WEB_SEARCH_URL.format(q=quote_plus(query))
    js = (_DDG_RESULTS_JS if "duckduckgo.com" in WEB_SEARCH_URL
          else _GENERIC_RESULTS_JS)

    def attempt():
        try:
            with _Tab() as tab:
                tab.navigate(target)
                return _evaluate_json(tab, js)
        except httpx.HTTPError as e:
            raise _wrapped(e, "Ricerca web fallita")

    rows = _retry_transient(attempt)
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("url"):
            continue
        out.append({"title": str(row.get("title") or "")[:300],
                    "url": _clean_result_url(str(row["url"]))[:2000],
                    "snippet": str(row.get("snippet") or "")[:500]})
        if len(out) >= max_results:
            break
    return out


def _looks_pdf(url):
    try:
        return urlparse(url).path.lower().endswith(".pdf")
    except ValueError:
        return False


# --- Fetch diretto dal host (PDF e ripiego statico) -------------------------------

_UA = ("Mozilla/5.0 (Windows NT 10.0; rv:130.0) Gecko/20100101 Firefox/130.0")


def _assert_public(url):
    """I fetch diretti partono dal HOST, non dal container: il browser blocca
    da solo le reti private ("Blocked private network target"), qui bisogna
    farlo a mano o read_page diventa una porta sui servizi interni."""
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise BrowserError(f"Host non risolvibile: {host[:100]}")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not ip.is_global:
            raise BrowserError("URL verso una rete privata o locale: "
                               "bloccato.")


def _fetch_public(url, max_bytes):
    """GET con redirect seguiti A MANO: ogni salto ripassa dal controllo di
    rete pubblica (un redirect verso 127.0.0.1 non deve essere seguito)."""
    for _hop in range(6):
        if not re.match(r"^https?://", url, re.IGNORECASE):
            raise BrowserError(f"URL non valido (serve http/https): "
                               f"{url[:200]}")
        _assert_public(url)
        r = httpx.get(url, timeout=_REQUEST_TIMEOUT, follow_redirects=False,
                      headers={"User-Agent": _UA})
        if r.status_code in (301, 302, 303, 307, 308):
            target = r.headers.get("location")
            if not target:
                break
            url = str(httpx.URL(url).join(target))
            continue
        r.raise_for_status()
        if len(r.content) > max_bytes:
            raise BrowserError("Contenuto troppo grande per essere letto "
                               f"({len(r.content) // (1024 * 1024)} MB).")
        return r
    raise BrowserError(f"Troppi redirect: {url[:200]}")


def _read_pdf(url):
    """Un PDF non si legge col browser: si scarica e passa dal motore PyMuPDF
    esistente."""
    from ..engine.pdf import extract_text
    try:
        r = _fetch_public(url, _PDF_MAX_BYTES)
    except httpx.HTTPError as e:
        raise BrowserError(f"Download del PDF fallito: {e}")
    if len(r.content) > _PDF_MAX_BYTES:
        raise BrowserError("PDF troppo grande per essere letto "
                           f"({len(r.content) // (1024 * 1024)} MB).")
    try:
        text, _pages = extract_text(r.content, allow_empty=True)
    except Exception as e:
        raise BrowserError(f"PDF non leggibile: {e}")
    if not text.strip():
        raise BrowserError("Il PDF non contiene testo estraibile "
                           "(probabile scansione).")
    return {"url": str(r.url), "title": urlparse(str(r.url)).path.rsplit(
        "/", 1)[-1], "text": text, "total": len(text)}


# quello che nel browser toglie la kill-list di _EXTRACT_JS
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas",
              "iframe", "nav", "header", "footer", "aside", "form", "button",
              "select", "video", "audio"}
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2",
               "h3", "h4", "h5", "h6", "section", "article", "main",
               "blockquote", "figure", "figcaption", "dt", "dd"}


class _TextExtractor(HTMLParser):
    """Testo leggibile dall'HTML statico, stessa resa di _EXTRACT_JS:
    boilerplate via, link riscritti [testo](url)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title = [], ""
        self._skip, self._in_title = 0, False
        self._href, self._link_start = None, None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith(("http://", "https://")):
                self._href, self._link_start = href, len(self.parts)
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._href is not None:
            label = re.sub(r"\s+", " ",
                           "".join(self.parts[self._link_start:])).strip()
            if label and len(label) < 200:
                del self.parts[self._link_start:]
                self.parts.append(f"[{label}]({self._href})")
            self._href, self._link_start = None, None
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        else:
            self.parts.append(data)


def _html_to_text(html):
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass                    # HTML marcio: si tiene quel che c'è
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return parser.title.strip(), text.strip()


def _read_static(url):
    """Ripiego SENZA browser: GET diretto + estrazione dal solo HTML.
    Serve per le pagine che non smettono mai di caricare (news piene di
    tracker): lì il navigate scade e l'evaluate resta in coda dietro il lock
    per-tab del server, ma il contenuto è quasi sempre server-rendered e un
    GET lo porta a casa in pochi secondi. Niente JS: le SPA restano al
    browser (qui uscirebbero vuote e si tiene l'errore primario)."""
    try:
        r = _fetch_public(url, _PDF_MAX_BYTES)
    except httpx.HTTPError as e:
        raise BrowserError(f"Lettura diretta fallita: {e}")
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype == "application/pdf":
        return _read_pdf(str(r.url))
    if ctype and "html" not in ctype and "xml" not in ctype \
            and not ctype.startswith("text/"):
        raise BrowserError(f"Contenuto non leggibile ({ctype[:60]}).")
    title, text = _html_to_text(r.text)
    if not text.strip():
        raise BrowserError("La pagina non contiene testo leggibile.")
    return {"url": str(r.url), "title": title, "text": text,
            "total": len(text)}


# Budget della terza gamba _read_spa: attesa complessiva che la navigazione
# committi e la pagina si assesti (l'attesa vera vive DENTRO _EXTRACT_JS).
_SPA_SETTLE = 25


def _read_spa(url, cap):
    """Terza gamba di read(), per i siti dove il navigate scade E lo statico
    esce senza testo (SPA a guscio vuoto). Lì il goto del server può restare
    pendente PER SEMPRE anche
    con la pagina pronta in 2-3 s (bug upstream, innescato dal redirect
    client-side di React Router durante il load) e il suo timeout interno non
    scatta: la tab del navigate è avvelenata (lock mai libero) e va
    abbandonata. Qui si naviga in una tab FRESCA assegnando location.href da
    un evaluate su about:blank (ritorna subito, nessun goto da tracciare, il
    route-guard del container continua a bloccare le reti private) e si passa
    DRITTI all'estrazione, che ammutolisce la pagina e aspetta da dentro:
    niente poll dall'esterno, che sulle pagine vive si appendono al tracker
    del server a caso (vedi _EXTRACT_JS). Se la navigazione committa a metà
    attesa il contesto JS muore ("Execution context was destroyed"): si
    rilancia sul documento nuovo finché c'è budget."""

    def attempt():
        try:
            with _Tab() as tab:
                tab.evaluate(f"location.href = {json.dumps(url)}; 'ok'",
                             timeout_ms=5000, http_timeout=15)
                deadline = time.monotonic() + _SPA_SETTLE
                while True:
                    time.sleep(1.0)
                    wait_ms = max(2000,
                                  int((deadline - time.monotonic()) * 1000))
                    try:
                        page = _extract(tab, cap, wait_ms=wait_ms)
                        break
                    except BrowserError as e:
                        if (time.monotonic() >= deadline
                                or any(sig in str(e) for sig in _TRANSIENT)):
                            raise
                        continue    # documento sostituito: si riprova
                if (page.get("url") or "").startswith("about:"):
                    # mai committata: l'URL non risponde
                    raise BrowserError("Navigazione fallita, pagina non "
                                       f"raggiungibile: {url[:200]}")
                return page
        except httpx.HTTPError as e:
            raise _wrapped(e, "Lettura della pagina fallita")

    return _retry_transient(attempt)


def read(url, cap=120000):
    """Naviga `url` (rendering JS vero) e ritorna il contenuto leggibile:
    {url, title, text, total}. `total` è la lunghezza PRIMA del taglio del
    browser; il troncamento verso il modello lo applica il handler. Se l'URL
    punta a un PDF si scarica e si estrae con PyMuPDF. Se il navigate scade
    si passa SUBITO all'HTML statico e, se anche quello esce vuoto (SPA a
    guscio vuoto), a una tab fresca navigata via location.href (_read_spa);
    lo statico copre anche ogni altro guasto del rendering. Bloccante."""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise BrowserError(f"URL non valido (serve http/https): {url[:200]}")
    if _looks_pdf(url):
        return _read_pdf(url)
    _ensure()

    def attempt():
        try:
            with _Tab() as tab:
                nav = tab.navigate(url)
                if nav is not None:
                    # un evaluate fallito qui esce subito verso il ripiego
                    # statico di read()
                    return _extract(tab, cap)
        except httpx.HTTPError as e:
            raise _wrapped(e, "Lettura della pagina fallita")
        # navigate scaduto: quasi sempre è una pagina che non smette mai di
        # caricare (o un goto che non si risolve proprio) e il lock per-tab
        # resta preso, quindi un evaluate su QUELLA tab non partirebbe mai —
        # la tab è appena stata chiusa uscendo dal `with`. Il GET statico
        # risponde in pochi secondi (news server-rendered); se esce vuoto
        # (SPA a guscio vuoto) si riprova con una tab fresca via
        # location.href, che il goto non deve tracciare.
        try:
            return _read_static(url)
        except BrowserError:
            pass
        return _read_spa(url, cap)

    try:
        page = _retry_transient(attempt)
        if (page.get("url") or "").startswith("about:"):
            raise BrowserError(f"Navigazione fallita, pagina non "
                               f"raggiungibile: {url[:200]}")
        if (page.get("contentType") or "").lower() == "application/pdf":
            return _read_pdf(page.get("url") or url)
        text = page.get("text") or ""
        if not text.strip():
            raise BrowserError("La pagina non contiene testo leggibile "
                               "(possibile CAPTCHA, paywall o contenuto solo "
                               "grafico).")
    except BrowserError as primary:
        try:
            return _read_static(url)
        except BrowserError:
            raise primary       # l'errore del browser è il più fedele
    return {"url": str(page.get("url") or url), "title": str(page.get("title")
            or ""), "text": text, "total": int(page.get("total") or len(text))}

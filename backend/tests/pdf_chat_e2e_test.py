"""Chat anonimizzata -> il modello produce un PDF -> l'utente lo scarica coi
valori reali. Il giro completo, senza finzioni.

Tutto quello che tocca è quello di esercizio:
  - OpenRouter VERO (la chiave di data/openrouter.key, e SPENDE crediti);
  - modello PII VERO (rizzo-pii): il registro della conversazione nasce dal
    rilevamento, non da uno stub;
  - sandbox Docker VERA: il PDF lo genera il modello con python-docx +
    `soffice --convert-to pdf`, come in produzione;
  - le route vere via TestClient, con DB e cartelle isolati.

Cosa si pretende:
  1. verso OpenRouter escono i segnaposto, non i valori;
  2. il modello consegna un PDF (in chat anonimizzata non è più vietato);
  3. il PDF che l'utente scarica contiene i valori REALI e nessun segnaposto
     del registro;
  4. il testo del PDF non è danneggiato dalla riscrittura;
  5. l'avviso mostrato in UI dice la verità su cosa è stato ripristinato.

Serve Docker attivo con l'immagine della sandbox:
    docker build -t blockingbear-sandbox:1 backend/sandbox
Uso:
    python backend/tests/pdf_chat_e2e_test.py \
        [modello] [file da allegare] [richiesta]

Senza argomenti usa il promemoria di prova qui sotto; con file e richiesta si
riproduce una conversazione vera (utile per rifare un caso segnalato).
"""

import json
import mimetypes
import os
import re
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

# PRIMA di importare app.*: dati isolati, un solo container. Nessun
# BLOCKINGBEAR_OPENROUTER_BASE_URL: si parla con OpenRouter davvero.
DATA = HERE / "data" / "test_pdf_e2e"
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_SANDBOX_POOL"] = "1"
shutil.rmtree(DATA, ignore_errors=True)        # ogni run parte pulito
DATA.mkdir(parents=True, exist_ok=True)
REAL_KEY = HERE / "data" / "openrouter.key"
if not REAL_KEY.is_file():
    sys.exit("Manca backend/data/openrouter.key: configura la chiave dal "
             "pannello admin e riprova.")
shutil.copyfile(REAL_KEY, DATA / "openrouter.key")

import fitz                                                        # noqa: E402
import httpx                                                      # noqa: E402
from fastapi import FastAPI                                       # noqa: E402
from fastapi.testclient import TestClient                         # noqa: E402

from app import chat_anonymization as chat_anon                   # noqa: E402
from app import db, jobs                                          # noqa: E402
from app.auth import seed_admin                                   # noqa: E402
from app.chat_anonymization import conversation_mapping           # noqa: E402
from app.db import init_db                                        # noqa: E402
from app.engine.docx import extract_text as extract_docx          # noqa: E402
from app.engine.pdf import extract_text as extract_pdf            # noqa: E402
from app.engine.pdf_export import PLACEHOLDER_RE                  # noqa: E402
from app.engine.pdf_export import _latin1 as latin1               # noqa: E402
from app.engine.pptx import extract_text as extract_pptx          # noqa: E402
from app.engine.xlsx import extract_text as extract_xlsx          # noqa: E402
from app.openrouter import client as or_client                    # noqa: E402
from app.openrouter import sandbox                                # noqa: E402
from app.routes import auth_routes, chat_routes                   # noqa: E402

# prefisso container diverso da quello di esercizio: lo sweep degli orfani non
# deve azzerare il pool di un backend di sviluppo attivo
sandbox._NAME_PREFIX = "blockingbear-sbxe-"

MODEL = sys.argv[1] if len(sys.argv) > 1 else "anthropic/claude-sonnet-4.5"
ATTACH = Path(sys.argv[2]) if len(sys.argv) > 2 else None

# Il documento allegato: PII italiane vere per forma, inventate nei valori.
DOCUMENT = """Promemoria interno - pratica AB-77/2026

Cliente: Fratelli Bianchi & Figli S.r.l.
Referente: Mario Rossi
Indirizzo: Via Roma 10, 20121 Milano
Email: mario.rossi@fratellibianchi.example.com
Telefono: +39 02 5512 8890
IBAN: IT60X0542811101000000123456

Fattura 2026/114 del 12/06/2026, importo 1.240,00 EUR, scaduta il 12/07/2026.
Secondo referente: Giulia Verdi, giulia.verdi@fratellibianchi.example.com
Nota: il pagamento era atteso entro trenta giorni dalla data fattura.
"""

DEFAULT_PROMPT = ("Con i dati del promemoria allegato prepara un PDF di "
                  "sollecito di pagamento intestato al cliente: una riga di "
                  "intestazione col nome del cliente, il corpo del sollecito "
                  "che citi referente, indirizzo, email e telefono, e una "
                  "tabella con numero fattura, importo, data di scadenza e "
                  "IBAN su cui pagare. Consegnami il PDF.")
PROMPT = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_PROMPT

# I valori che DEVONO tornare leggibili nel PDF scaricato. Sono quelli che il
# rilevatore trova sul promemoria di prova e che un sollecito cita per forza;
# con un allegato dell'utente non si sa a priori cosa citerà, e vale il
# controllo generale (ogni segnaposto dichiarato ripristinato è leggibile).
MUST_APPEAR = ([] if ATTACH else
               ["Fratelli Bianchi", "Mario Rossi",
                "IT60X0542811101000000123456"])

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


def ws(text):
    return re.sub(r"\s+", "", text or "")


def pdf_text(data):
    with fitz.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def readable_text(path):
    """Il testo di un file qualsiasi, con gli estrattori dell'app: la copia
    protetta di un allegato può essere un .docx, e leggerla come UTF-8
    darebbe byte di zip (nessun segnaposto, nessun valore: un controllo che
    passa sempre e non verifica niente)."""
    data = Path(path).read_bytes()
    ext = Path(path).suffix.lower()
    if ext == ".docx":
        return extract_docx(data)
    if ext == ".pdf":
        return extract_pdf(data)[0]
    if ext == ".pptx":
        return extract_pptx(data, full=True)
    if ext == ".xlsx":
        return extract_xlsx(data)
    return data.decode("utf-8", "replace")


def main():
    print(f"Modello: {MODEL}")
    print("Avvio sandbox Docker...")
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(chat_routes.router)
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)
    sandbox.start()
    deadline = time.time() + 120
    while time.time() < deadline and not (sandbox.status()["available"]
                                          and sandbox.status()["pool_ready"]):
        time.sleep(0.5)
    status = sandbox.status()
    if not status["available"]:
        sys.exit(f"Docker non disponibile: {status}")

    print("Carico il modello PII (rizzo-pii)...")
    t0 = time.time()
    jobs.ENGINES[0].load()
    print(f"  caricato in {time.time() - t0:.0f}s")

    client = TestClient(app)
    client.timeout = httpx.Timeout(900.0)      # un turno vero può durare
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    r = client.get("/api/openrouter/status", headers=auth).json()
    if not r.get("configured"):
        sys.exit(f"Chiave OpenRouter non valida: {r}")
    print(f"  crediti OpenRouter: {r.get('credits')}")

    conv = client.post("/api/chats", json={"model": MODEL, "anonymized": True},
                       headers=auth).json()
    cid = conv["id"]
    check("chat anonimizzata creata", conv["anonymized"] is True, cid)

    if ATTACH:
        payload = (ATTACH.name, ATTACH.read_bytes(),
                   mimetypes.guess_type(ATTACH.name)[0]
                   or "application/octet-stream")
        print(f"  allegato: {ATTACH.name} ({len(payload[1])} byte)")
    else:
        payload = ("promemoria_AB-77.txt", DOCUMENT.encode("utf-8"),
                   "text/plain")
    up = client.post(f"/api/chats/{cid}/attachments",
                     files={"file": payload}, headers=auth)
    check("allegato accettato in chat anonimizzata", up.status_code == 200,
          up.text[:160])
    print(f"  richiesta: {PROMPT[:110]}")

    print("Turno in corso (anonimizzazione -> modello -> sandbox -> "
          "ripristino)...")
    t0 = time.time()
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": PROMPT}, headers=auth) as resp:
        events = sse_events(resp)
    elapsed = time.time() - t0
    kinds = [e["type"] for e in events]
    errors = [e for e in events if e["type"] == "error"]
    print(f"  turno concluso in {elapsed:.0f}s, eventi: {len(events)} "
          f"({', '.join(sorted(set(kinds)))})")
    for e in errors:
        print(f"  ERRORE dal turno: {e.get('message')}")
    done = next((e for e in reversed(events) if e["type"] == "done"), None)
    if done:
        usage = done.get("usage") or {}
        print(f"  iterazioni: {done.get('iterations')} costo: "
              f"${usage.get('cost', 0):.4f} token: "
              f"{usage.get('prompt_tokens')}+{usage.get('completion_tokens')}")
    check("il turno si è chiuso senza errori fatali",
          done is not None and not [e for e in errors if e.get("fatal")],
          str([e.get("message") for e in errors])[:200])

    # 1. il modello ha visto i segnaposto, non i valori. Il prompt non contiene
    # PII (sta tutto nell'allegato), quindi quel che conta è la COPIA PROTETTA
    # che la sandbox monta, la sua scheda e il nome neutro del file.
    with SessionLocal() as s:
        mapping = conversation_mapping(s, cid)
        umsg = (s.query(db.ChatMessage).filter_by(conv_id=cid, role="user")
                .order_by(db.ChatMessage.seq).first())
        canonical = umsg.to_openrouter()["content"]
        canonical = (canonical if isinstance(canonical, str)
                     else json.dumps(canonical, ensure_ascii=False))
        att_in = (s.query(db.Attachment)
                  .filter_by(conv_id=cid, direction="in").first())
        seen = readable_text(chat_anon.attachment_path(att_in))
        seen_name = chat_anon.attachment_model_name(att_in)
        briefing = att_in.model_briefing_json or ""
    print(f"  registro della conversazione: {len(mapping)} entità "
          f"({', '.join(sorted(mapping)[:6])}...)")
    print(f"  il modello ha letto: {seen_name}")
    outgoing = canonical + "\n" + seen + "\n" + briefing + "\n" + seen_name
    leaked = [v for v in MUST_APPEAR if v in outgoing]
    check("il modello ha visto segnaposto, non valori (allegato, scheda, nome)",
          bool(PLACEHOLDER_RE.search(seen)) and not leaked,
          f"valori sfuggiti: {leaked}" if leaked else
          f"segnaposto nel file protetto: "
          f"{len(set(PLACEHOLDER_RE.findall(seen)))}")

    # 2. il modello ha consegnato un PDF
    atts = [a for e in events for a in (e.get("attachments") or [])]
    pdfs = [a for a in atts if (a["filename"] or "").lower().endswith(".pdf")]
    check("il modello ha consegnato un PDF",
          bool(pdfs), f"artifact: {[a['filename'] for a in atts]}")
    if not pdfs:
        return summary()
    art = pdfs[-1]
    report = art.get("anonymization_report") or {}
    print(f"  artifact: {art['filename']} ({art['size']} byte) "
          f"stato={art['anonymization_status']}")
    print(f"  report: {json.dumps(report, ensure_ascii=False)[:400]}")

    # 3. il file scaricato ha i valori reali e nessun segnaposto del registro
    got = client.get(f"/api/chats/{cid}/attachments/{art['id']}", headers=auth)
    check("il PDF si scarica", got.status_code == 200
          and got.content[:5] == b"%PDF-", str(got.status_code))
    text = pdf_text(got.content)
    missing = [v for v in MUST_APPEAR if ws(v) not in ws(text)]
    check("i valori reali sono nel PDF scaricato", not missing,
          f"mancanti: {missing}")
    # Controllo indipendente dal documento: il report dice quali segnaposto
    # sostiene di aver riscritto, e ognuno di quei valori deve essere davvero
    # leggibile. È la verifica che non si può barare.
    declared = report.get("by_placeholder") or {}
    unreadable = [ph for ph, n in declared.items()
                  if n and ph in mapping
                  and ws(latin1(mapping[ph])[0]) not in ws(text)]
    check("ogni segnaposto dichiarato ripristinato è leggibile nel PDF",
          bool(declared) and not unreadable,
          f"dichiarati={sum(declared.values())} non leggibili={unreadable}")
    left = sorted(set(PLACEHOLDER_RE.findall(text)))
    known_left = [ph for ph in left if ph in mapping]
    check("nessun segnaposto del registro è rimasto nel PDF",
          not known_left, f"rimasti: {known_left}")
    if left and not known_left:
        print(f"  nota: segnaposto NON del registro nel file (inventati dal "
              f"modello): {left}")
    check("lo stato dell'allegato dice ripristinato",
          art["anonymization_status"] == "restored"
          and report.get("restored", 0) > 0,
          f"stato={art['anonymization_status']} restored={report.get('restored')}")
    check("nessun valore rimosso senza essere riscritto",
          not report.get("lost"), str(report.get("lost")))

    # 4. il PDF è un documento sano: il testo non è a pezzi
    words = re.findall(r"[A-Za-zÀ-ÿ]{4,}", text)
    check("il PDF contiene testo estraibile e continuo", len(words) > 25,
          f"parole={len(words)}")

    # 5. l'avviso in UI è veritiero
    notice = report.get("notice") or ""
    coherent = (str(report.get("restored")) in notice
                and (not report.get("shrunk") or "corpo ridotto" in notice)
                and (not report.get("images") or "immagini" in notice))
    check("l'avviso mostrato in UI corrisponde al report", coherent, notice[:220])

    out = DATA / "artifact_ripristinato.pdf"
    out.write_bytes(got.content)
    print(f"\nPDF consegnato salvato in: {out}")
    return summary()


def summary():
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        sandbox.shutdown()
    sys.exit(code)

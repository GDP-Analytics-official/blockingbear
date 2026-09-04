"""Chat anonimizzata -> il modello disegna un grafico -> l'utente lo scarica
con i nomi veri nelle etichette. Il giro completo, senza finzioni.

Tutto quello che tocca è quello di esercizio:
  - OpenRouter VERO (la chiave di data/openrouter.key, e SPENDE crediti);
  - modello PII VERO (rizzo-pii): il registro nasce dal rilevamento;
  - sandbox Docker VERA: il grafico lo disegna matplotlib nel container;
  - le route vere via TestClient, con DB e cartelle isolati.

Cosa si pretende:
  1. verso OpenRouter escono i segnaposto, non i valori;
  2. il modello segue la regola di sistema e lascia l'SVG accanto al png;
  3. il png scaricato disegna i VALORI REALI (catena di prove senza OCR:
     l'SVG consegnato contiene i valori ed è esattamente ciò che il png
     rasterizza);
  4. il grafico incastonato nel .docx/.pdf è quello ripristinato;
  5. l'avviso mostrato in UI dice la verità.

Serve Docker attivo con l'immagine della sandbox:
    docker build -t blockingbear-sandbox:1 backend/sandbox
Uso:
    python backend/tests/chart_chat_e2e_test.py [modello]
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

DATA = HERE / "data" / "test_chart_e2e"
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_SANDBOX_POOL"] = "1"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True, exist_ok=True)
REAL_KEY = HERE / "data" / "openrouter.key"
if not REAL_KEY.is_file():
    sys.exit("Manca backend/data/openrouter.key: configura la chiave dal "
             "pannello admin e riprova.")
shutil.copyfile(REAL_KEY, DATA / "openrouter.key")

import fitz                                                        # noqa: E402
import httpx                                                       # noqa: E402
from fastapi import FastAPI                                        # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

from app import chat_anonymization as chat_anon                    # noqa: E402
from app import db, jobs                                           # noqa: E402
from app.auth import seed_admin                                    # noqa: E402
from app.chat_anonymization import conversation_mapping            # noqa: E402
from app.db import init_db                                         # noqa: E402
from app.engine.docx import extract_text as extract_docx           # noqa: E402
from app.engine.pdf_export import PLACEHOLDER_RE                   # noqa: E402
from app.openrouter import client as or_client                     # noqa: E402
from app.openrouter import sandbox                                 # noqa: E402
from app.routes import auth_routes, chat_routes                    # noqa: E402

sandbox._NAME_PREFIX = "blockingbear-sbxg-"

MODEL = sys.argv[1] if len(sys.argv) > 1 else "anthropic/claude-sonnet-4.5"

# I numeri sono CONTEGGI, non importi: un importo tipo "96.150 EUR" viene
# rilevato come AMOUNT e sostituito, e il modello — giustamente — si ferma a
# chiedere il valore invece di disegnare il grafico (successo dell'
# anonimizzazione, ma il test non arriverebbe mai a misurare i grafici).
DOCUMENT = """Riepilogo commerciale 2026 - riservato

Cliente principale: Fratelli Bianchi & Figli S.r.l. - commesse chiuse: 24
Secondo cliente: Costruzioni Meridionali S.p.A. - commesse chiuse: 18
Terzo cliente: Officine Rinaldi S.n.c. - commesse chiuse: 9

Referente commerciale: Mario Rossi, mario.rossi@fratellibianchi.example.com
Nota: il conteggio è al netto delle commesse annullate.
"""

PROMPT = ("Con i dati del riepilogo allegato fammi un grafico a barre delle "
          "commesse chiuse per cliente, con il nome del cliente sotto ogni "
          "barra. Consegnami sia il grafico da solo sia un documento Word che "
          "lo contiene, con sotto una tabella cliente/commesse, e il PDF "
          "corrispondente.")

MUST_APPEAR = ["Fratelli Bianchi", "Costruzioni Meridionali",
               "Officine Rinaldi"]

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


def svg_text(data):
    with fitz.open(stream=data, filetype="svg") as doc:
        return "\n".join(page.get_text() for page in doc)


def distance(a, b, ext_a="png", ext_b="png", width=600):
    """Differenza media per canale tra due immagini riportate alla stessa
    larghezza (LibreOffice ricodifica e ridimensiona quello che incastona)."""
    def thumb(data, ext):
        with fitz.open(stream=data, filetype=ext) as doc:
            page = doc[0]
            zoom = width / page.rect.width
            return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)

    pa, pb = thumb(a, ext_a), thumb(b, ext_b)
    if (pa.width, pa.height) != (pb.width, pb.height):
        return 255.0
    return sum(abs(x - y) for x, y in zip(pa.samples, pb.samples)) / len(pa.samples)


def pdf_images(pdf_bytes):
    out = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            for img in page.get_images(full=True):
                info = doc.extract_image(img[0])
                out.append((info["image"], info["ext"]))
    return out


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
    if not sandbox.status()["available"]:
        sys.exit(f"Docker non disponibile: {sandbox.status()}")

    print("Carico il modello PII (rizzo-pii)...")
    t0 = time.time()
    jobs.ENGINES[0].load()
    print(f"  caricato in {time.time() - t0:.0f}s")

    client = TestClient(app)
    client.timeout = httpx.Timeout(900.0)
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
    client.post(f"/api/chats/{cid}/attachments",
                files={"file": ("riepilogo_2026.txt",
                                DOCUMENT.encode("utf-8"), "text/plain")},
                headers=auth)

    print("Turno in corso (anonimizzazione -> modello -> sandbox -> "
          "ripristino)...")
    t0 = time.time()
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": PROMPT}, headers=auth) as resp:
        events = sse_events(resp)
    elapsed = time.time() - t0
    errors = [e for e in events if e["type"] == "error"]
    done = next((e for e in reversed(events) if e["type"] == "done"), None)
    print(f"  turno concluso in {elapsed:.0f}s, eventi: {len(events)}")
    for e in errors:
        print(f"  ERRORE dal turno: {e.get('message')}")
    if done:
        usage = done.get("usage") or {}
        print(f"  iterazioni: {done.get('iterations')} costo: "
              f"${usage.get('cost', 0):.4f}")
    check("il turno si è chiuso senza errori fatali",
          done is not None and not [e for e in errors if e.get("fatal")],
          str([e.get("message") for e in errors])[:200])

    # 1. il modello ha visto i segnaposto, non i valori
    with SessionLocal() as s:
        mapping = conversation_mapping(s, cid)
        att_in = (s.query(db.Attachment)
                  .filter_by(conv_id=cid, direction="in").first())
        seen = chat_anon.attachment_path(att_in).read_text("utf-8", "replace")
    leaked = [v for v in MUST_APPEAR if v in seen]
    print(f"  registro: {len(mapping)} entità ({', '.join(sorted(mapping))})")
    check("il modello ha letto segnaposto, non valori", not leaked
          and bool(PLACEHOLDER_RE.search(seen)), f"sfuggiti: {leaked}")

    # 2. il modello ha seguito la regola: SVG accanto al png
    atts = [a for e in events for a in (e.get("attachments") or [])]
    names = [a["filename"] for a in atts]
    print(f"  artifact: {names}")
    pngs = [a for a in atts if a["filename"].lower().endswith(".png")]
    svgs = [a for a in atts if a["filename"].lower().endswith(".svg")]
    check("il modello ha consegnato il grafico", bool(pngs), str(names))
    check("il modello ha lasciato l'SVG sorgente accanto al png",
          bool(svgs), str(names))
    if not pngs or not svgs:
        return summary()

    def download(att):
        got = client.get(f"/api/chats/{cid}/attachments/{att['id']}",
                         headers=auth)
        return got.content

    png_att = pngs[-1]
    report = png_att.get("anonymization_report") or {}
    print(f"  grafico: {png_att['filename']} stato="
          f"{png_att['anonymization_status']}")
    print(f"  report: {json.dumps(report, ensure_ascii=False)[:300]}")
    check("il grafico è dichiarato ripristinato",
          png_att["anonymization_status"] == "restored"
          and report.get("chart") and report.get("restored", 0) > 0,
          f"restored={report.get('restored')}")

    # 3. il png scaricato disegna i valori reali. Prova in due passi, senza
    # OCR: l'SVG consegnato contiene i valori, e il png È quell'SVG
    # rasterizzato (differenza per pixel ~0).
    png_data = download(png_att)
    svg_data = download(svgs[-1])
    drawn = svg_text(chat_anon._svg_host_fonts(
        svg_data.decode("utf-8")).encode("utf-8"))
    shown = [v for v in MUST_APPEAR if v in drawn]
    check("le etichette del grafico sono i valori reali",
          len(shown) >= 2, f"trovati: {shown}")
    left = [ph for ph in set(PLACEHOLDER_RE.findall(drawn)) if ph in mapping]
    check("nessun segnaposto del registro è rimasto nel grafico",
          not left, f"rimasti: {left}")
    width = fitz.Pixmap(png_data).width
    d = distance(png_data, chat_anon._rasterize_svg(
        svg_data.decode("utf-8"), width))
    check("il png consegnato è esattamente l'SVG ripristinato",
          d < 1.0, f"differenza per pixel={d:.2f}")

    # 4. il grafico dentro i documenti è quello ripristinato
    docs = [a for a in atts if a["filename"].lower().endswith(".docx")]
    pdfs = [a for a in atts if a["filename"].lower().endswith(".pdf")]
    if docs:
        doc_data = download(docs[-1])
        text = extract_docx(doc_data)
        missing = [v for v in MUST_APPEAR if v not in text]
        check("il .docx ha i valori reali nel testo", not missing,
              f"mancanti: {missing}")
        rep = docs[-1].get("anonymization_report") or {}
        check("il grafico incastonato nel .docx è stato scambiato",
              rep.get("images_swapped", 0) >= 1, str(rep.get("notice"))[:160])
    if pdfs:
        pdf_data = download(pdfs[-1])
        rep = pdfs[-1].get("anonymization_report") or {}
        check("il PDF è rigenerato dal .docx col grafico già scambiato",
              rep.get("rerendered") and rep.get("images_swapped", 0) >= 1,
              str(rep.get("notice"))[:200])
        images = pdf_images(pdf_data)
        best = min((distance(i, png_data, ext) for i, ext in images),
                   default=255.0)
        # LibreOffice ricodifica in jpeg e ridimensiona: si pretende lo stesso
        # grafico, non gli stessi byte (la prova che sia la versione
        # ripristinata e non quella coi segnaposto la dà chart_restore_test,
        # dove i due candidati esistono entrambi)
        check("nel PDF c'è il grafico consegnato", best < 8.0,
              f"immagini={len(images)} differenza minima={best:.2f}")

    # 5. l'avviso in UI dice la verità
    notice = report.get("notice") or ""
    check("l'avviso del grafico corrisponde al report",
          str(report.get("restored")) in notice and "grafico" in notice.lower(),
          notice[:200])

    out = DATA / "grafico_consegnato.png"
    out.write_bytes(png_data)
    print(f"\nGrafico consegnato salvato in: {out}")
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

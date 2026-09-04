"""Anteprima pre-invio del turno anonimizzato (app/chat_staging.py +
route staged di chat_routes.py) da un capo all'altro, con app FastAPI ridotta
(auth + chat), finto OpenRouter e modello PII vero. Niente Docker: si usa
test/plain-model (senza tool), la sandbox non serve.

Copre: preparazione (preview=True) con eventi SSE e stato staged, pagine PNG
e estrazione da rettangolo, anonimizza-in-più ([TAG_n] nel registro),
deanonimizza (entità excluded, ri-redazione del turno), ri-inclusione,
invio from_staged (controllo di uscita incluso: il valore deanonimizzato
parte in chiaro senza far scattare l'egress check), pulizia dello staged e
l'anonimizza-in-più di un valore che sta SOLO nel corpus OCR (ocr_corpus_case).

Uso:
    python backend/tests/chat_staging_test.py
"""
import io
import json
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import chat_mock_openrouter as mock                              # noqa: E402

os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_staging")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_OPENROUTER_BASE_URL"] = mock.BASE_URL

import httpx                                                     # noqa: E402
from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import settings_store                                  # noqa: E402
from app.auth import seed_admin                                  # noqa: E402
from app.db import User, init_db                                 # noqa: E402
from app.openrouter import client as or_client                   # noqa: E402
from app.routes import auth_routes, chat_routes                  # noqa: E402

PASS = 0
FAIL = 0

TXT = ("Cliente: Mario Rossi\n"
       "Email: mario.rossi@example.com\n"
       "IBAN: IT60X0542811101000000123456\n"
       "Nota: inventario ricambi da consegnare al cliente.\n")
PROMPT = ("Riassumi il file allegato per Mario Rossi e cita l'inventario "
          "ricambi nel report.")

# Il caso "valore che sta SOLO nei pixel": il layer testuale scrive
# "valutare", l'OCR dell'immagine legge "yalutare" (una v sottolineata dal
# correttore diventa una y). È la stringa letta a dover finire nel registro:
# è quella che copre i pixel.
OCR_VALUE = "yalutare"
OCR_LINE = f"da {OCR_VALUE} col referente di zona"
TEXT_LAYER = "Documento da valutare, poi archiviare in sede."


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


def of_type(events, t):
    return [e for e in events if e["type"] == t]


def sent():
    return httpx.get(mock.DEBUG_URL).json()


def user_text(request):
    msg = [m for m in request["messages"] if m["role"] == "user"][-1]
    if isinstance(msg["content"], str):
        return msg["content"]
    return "".join(p.get("text", "") for p in msg["content"]
                   if p["type"] == "text")


def item_by_kind(staged, kind):
    return next(i for i in staged["items"] if i["kind"] == kind)


class FakeOCR:
    """Una riga fissa al centro di qualunque immagine (come in
    ocr_selection_test): serve la MECCANICA del corpus, non la lettura."""

    def __call__(self, arr):
        h, w = arr.shape[0], arr.shape[1]
        return types.SimpleNamespace(
            boxes=[[[w * 0.20, h * 0.40], [w * 0.80, h * 0.40],
                    [w * 0.80, h * 0.60], [w * 0.20, h * 0.60]]],
            txts=[OCR_LINE], scores=[0.95])


def _png(w, h, color=(235, 235, 240)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _pdf_with_image():
    """Una pagina con un layer testuale NON vuoto più un'immagine: la forma
    del caso reale (un .docx con dentro uno screenshot della stessa frase)."""
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), TEXT_LAYER, fontsize=12)
    page.insert_image(fitz.Rect(60, 300, 460, 400), stream=_png(400, 100))
    out = doc.tobytes()
    doc.close()
    return out


def ocr_corpus_case(client, auth, SessionLocal):
    """L'anonimizza-in-più deve cercare il valore anche nel CORPUS OCR.

    La selezione nell'anteprima estrae il testo con l'OCR, quindi il valore
    scelto può vivere SOLO nei pixel — tipicamente perché l'OCR l'ha letto
    male, ed è comunque QUELLA la stringa da coprire. Se `anonymize_text`
    interrogasse il solo layer testuale (chat_staging._full_text), la preview
    lo offrirebbe e poi lo rifiuterebbe come non presente nel messaggio né
    negli allegati. Il caso si vede alla PRIMA modifica di un'anteprima
    fresca: dalla seconda in poi item["text"] arriva da _rebuild_turn, che il
    corpus lo accoda comunque. Una sola regola per chat e progetti:
    chat_staging.redigible_text."""
    from app.engine import image_ocr
    if not image_ocr.available():
        check("corpus OCR nell'anonimizza-in-più", True,
              "SALTATO: stack OCR non installato")
        return
    image_ocr._ocr = FakeOCR()                  # niente lettura vera nel test
    image_ocr.detect_regions = lambda pil: []

    conv = client.post("/api/chats", json={"anonymized": True},
                       headers=auth).json()
    cid = conv["id"]
    client.patch(f"/api/chats/{cid}", json={"model": "test/plain-model"},
                 headers=auth)
    client.post(f"/api/chats/{cid}/attachments",
                files={"file": ("scansione.pdf", _pdf_with_image(),
                                "application/pdf")}, headers=auth)
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": "Controlla la scansione allegata.",
                             "preview": True, "ocr": True},
                       headers=auth) as resp:
        events = sse_events(resp)
    staged_evs = of_type(events, "staged")
    check("anteprima con OCR pronta", len(staged_evs) == 1,
          json.dumps([e["type"] for e in events])[:160])
    if not staged_evs:
        return

    check("il valore OCR non è nel layer testuale (è il punto del test)",
          OCR_VALUE not in TEXT_LAYER, TEXT_LAYER)

    # PRIMA modifica dell'anteprima: è il caso in cui il corpus OCR non è
    # ancora passato da _rebuild_turn
    r = client.post(f"/api/chats/{cid}/staged/anonymize-text",
                    json={"text": OCR_VALUE}, headers=auth)
    check("anonimizza-in-più di un valore letto SOLO dall'OCR",
          r.status_code == 200
          and r.json().get("added", "").startswith("[CUSTOM_"),
          f"{r.status_code} {r.text[:160]}")
    if r.status_code != 200:
        return
    staged = r.json()
    ph = staged["added"]
    file_item = item_by_kind(staged, "attachment")
    check("il valore OCR è in mappa sull'allegato",
          file_item["mapping"].get(ph) == OCR_VALUE,
          json.dumps(file_item["mapping"])[:160])
    boxes = [b for bl in (file_item.get("anonymized_boxes") or {}).values()
             for b in bl]
    check("box OCR disegnato nei pixel per il valore",
          any(b.get("ph") == ph and b.get("ocr") for b in boxes),
          json.dumps(boxes)[:200])

    # il layer testuale continua a rispondere per conto suo
    r = client.post(f"/api/chats/{cid}/staged/anonymize-text",
                    json={"text": "archiviare in sede"}, headers=auth)
    check("valore del solo layer testuale: invariato", r.status_code == 200,
          f"{r.status_code} {r.text[:120]}")
    r = client.post(f"/api/chats/{cid}/staged/anonymize-text",
                    json={"text": "stringa che non esiste da nessuna parte"},
                    headers=auth)
    check("valore assente da testo E corpus: ancora 422", r.status_code == 422,
          f"{r.status_code} {r.text[:120]}")


def ocr_deanonymize_case(client, auth):
    """Deanonimizzare dall'anteprima deve TOGLIERE il box giallo, per ogni
    provenienza del box:
      - box OCR pianificato al RILEVAMENTO (qui una partita IVA sintetica con
        checksum valido, letta dall'OCR finto: la trova la rete regex);
      - box OCR aggiunto a mano (anonimizza-in-più);
      - box del layer testuale (redazione normale).
    Il caso storico: boxes_for confrontava il piano con `ph in mapping`, e il
    dizionario canonico di una ReplacementMapping conserva anche le entità
    escluse (servono a decodificare ciò che è già partito): il box del
    rilevamento sopravviveva al deanonimizza, quello aggiunto a mano no."""
    from app.engine import image_ocr
    if not image_ocr.available():
        check("deanonimizza toglie i box OCR", True,
              "SALTATO: stack OCR non installato")
        return
    piva = "12345678903"                      # sintetica, supera il Luhn
    ocr_line = f"Fornitore di prova P.IVA {piva} magazzino"

    class PivaOCR(FakeOCR):
        def __call__(self, arr):
            res = super().__call__(arr)
            res.txts = [ocr_line]
            return res

    image_ocr._ocr = PivaOCR()
    image_ocr.detect_regions = lambda pil: []

    import fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Referente: Mario Rossi, archiviare.",
                     fontsize=12)
    page.insert_image(fitz.Rect(60, 300, 460, 400), stream=_png(400, 100))
    pdf = doc.tobytes()
    doc.close()

    conv = client.post("/api/chats", json={"anonymized": True},
                       headers=auth).json()
    cid = conv["id"]
    client.patch(f"/api/chats/{cid}", json={"model": "test/plain-model"},
                 headers=auth)
    client.post(f"/api/chats/{cid}/attachments",
                files={"file": ("scansione.pdf", pdf, "application/pdf")},
                headers=auth)
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": "Controlla il fornitore.",
                             "preview": True, "ocr": True},
                       headers=auth) as resp:
        events = sse_events(resp)
    staged_evs = of_type(events, "staged")
    check("deanonimizza: anteprima con OCR pronta", len(staged_evs) == 1,
          json.dumps([e["type"] for e in events])[:160])
    if not staged_evs:
        return
    item = item_by_kind(staged_evs[0], "attachment")

    def boxes_of(it):
        return [b for bl in (it.get("anonymized_boxes") or {}).values()
                for b in bl]

    ph_piva = next((ph for ph, v in item["mapping"].items() if piva in v), None)
    check("P.IVA letta dall'OCR rilevata all'invio (box da piano)",
          ph_piva is not None
          and any(b["ph"] == ph_piva and b.get("ocr") for b in boxes_of(item)),
          json.dumps(item["mapping"])[:200])
    ph_name = next((ph for ph, v in item["mapping"].items()
                    if v == "Mario Rossi"), None)
    check("nome nel layer testuale rilevato (box testo)",
          ph_name is not None
          and any(b["ph"] == ph_name and not b.get("ocr")
                  for b in boxes_of(item)),
          json.dumps(item["mapping"])[:200])

    # --- box OCR da piano: deanonimizza -> sparisce ------------------------------
    if ph_piva:
        r = client.post(f"/api/chats/{cid}/staged/deanonymize",
                        json={"placeholder": ph_piva}, headers=auth)
        it = item_by_kind(r.json(), "attachment") if r.status_code == 200 else {}
        check("deanonimizza P.IVA (box OCR da rilevamento): box sparito",
              r.status_code == 200 and ph_piva not in it.get("mapping", {})
              and not any(b["ph"] == ph_piva for b in boxes_of(it)),
              f"{r.status_code} {json.dumps(boxes_of(it))[:200]}")
        # ri-inclusione dalla selezione: il box torna
        r = client.post(f"/api/chats/{cid}/staged/anonymize-text",
                        json={"text": piva}, headers=auth)
        it = item_by_kind(r.json(), "attachment") if r.status_code == 200 else {}
        check("ri-anonimizza la P.IVA: stesso placeholder e box tornato",
              r.status_code == 200 and r.json().get("added") == ph_piva
              and any(b["ph"] == ph_piva and b.get("ocr") for b in boxes_of(it)),
              f"{r.status_code} {r.text[:160]}")

    # --- box OCR aggiunto a mano: deanonimizza -> sparisce -----------------------
    r = client.post(f"/api/chats/{cid}/staged/anonymize-text",
                    json={"text": "Fornitore di prova"}, headers=auth)
    ph_custom = r.json().get("added") if r.status_code == 200 else None
    it = item_by_kind(r.json(), "attachment") if ph_custom else {}
    check("box OCR aggiunto a mano disegnato",
          ph_custom is not None
          and any(b["ph"] == ph_custom and b.get("ocr") for b in boxes_of(it)),
          f"{r.status_code} {r.text[:160]}")
    if ph_custom:
        r = client.post(f"/api/chats/{cid}/staged/deanonymize",
                        json={"placeholder": ph_custom}, headers=auth)
        it = item_by_kind(r.json(), "attachment") if r.status_code == 200 else {}
        check("deanonimizza il valore aggiunto a mano: box sparito",
              r.status_code == 200
              and not any(b["ph"] == ph_custom for b in boxes_of(it)),
              f"{r.status_code} {json.dumps(boxes_of(it))[:200]}")

    # --- box del layer testuale: deanonimizza -> sparisce -------------------------
    if ph_name:
        r = client.post(f"/api/chats/{cid}/staged/deanonymize",
                        json={"placeholder": ph_name}, headers=auth)
        it = item_by_kind(r.json(), "attachment") if r.status_code == 200 else {}
        check("deanonimizza il nome (testo normale): box sparito",
              r.status_code == 200 and ph_name not in it.get("mapping", {})
              and not any(b["ph"] == ph_name for b in boxes_of(it)),
              f"{r.status_code} {json.dumps(boxes_of(it))[:200]}")


def ocr_local_deanonymize_case(client, auth):
    """Deanonimizzare le voci LOCALI della cache OCR — [UNREADABLE_n] (riga a
    bassa confidenza) e [SIGNATURE_n] (regione firma/timbro) — deve togliere
    il box come per ogni altro placeholder. Non stanno nel registro della
    conversazione (avvelenerebbero i turni successivi), quindi il
    deanonimizza rispondeva 404 "Segnaposto non presente nella mappa";
    l'esclusione vive ora nella cache OCR dell'allegato. Copre: valore
    mostrato nella mappa dell'item, deanonimizza per placeholder e per
    categoria, ri-inclusione della riga illeggibile dalla selezione (senza
    allocare un [CUSTOM_n]), 404 su una voce inesistente."""
    from app.engine import image_ocr
    if not image_ocr.available():
        check("deanonimizza voci locali OCR", True,
              "SALTATO: stack OCR non installato")
        return
    unreadable_line = "prodotto in offerta"       # nessuna PII, letto male

    class LocalOCR(FakeOCR):
        def __call__(self, arr):
            h, w = arr.shape[0], arr.shape[1]

            def band(y0, y1):
                return [[w * .2, h * y0], [w * .8, h * y0],
                        [w * .8, h * y1], [w * .2, h * y1]]
            return types.SimpleNamespace(
                boxes=[band(.10, .30), band(.45, .60)],
                txts=["Nota di consegna merce", unreadable_line],
                scores=[0.95, 0.40])

    image_ocr._ocr = LocalOCR()
    image_ocr.detect_regions = lambda pil: [
        {"b": [int(pil.width * .05), int(pil.height * .80),
               int(pil.width * .30), int(pil.height * .98)], "s": 0.9}]
    try:
        import fitz
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "Documento di prova.", fontsize=12)
        page.insert_image(fitz.Rect(60, 300, 460, 500), stream=_png(400, 200))
        pdf = doc.tobytes()
        doc.close()

        conv = client.post("/api/chats", json={"anonymized": True},
                           headers=auth).json()
        cid = conv["id"]
        client.patch(f"/api/chats/{cid}", json={"model": "test/plain-model"},
                     headers=auth)
        client.post(f"/api/chats/{cid}/attachments",
                    files={"file": ("scan.pdf", pdf, "application/pdf")},
                    headers=auth)
        with client.stream("POST", f"/api/chats/{cid}/messages",
                           json={"content": "Guarda la scansione.",
                                 "preview": True, "ocr": True},
                           headers=auth) as resp:
            events = sse_events(resp)
        staged_evs = of_type(events, "staged")
        check("voci locali: anteprima con OCR pronta", len(staged_evs) == 1,
              json.dumps([e["type"] for e in events])[:160])
        if not staged_evs:
            return
        item = item_by_kind(staged_evs[0], "attachment")

        def boxes_of(it):
            return [b for bl in (it.get("anonymized_boxes") or {}).values()
                    for b in bl]

        phs = {b["ph"] for b in boxes_of(item)}
        unr = next((p for p in phs if p.startswith("[UNREADABLE_")), None)
        sig = next((p for p in phs if p.startswith("[SIGNATURE_")), None)
        check("box [UNREADABLE_n] e [SIGNATURE_n] nell'anteprima",
              unr is not None and sig is not None, str(sorted(phs)))
        if not (unr and sig):
            return
        check("la mappa dell'item mostra il valore delle voci locali",
              item["mapping"].get(unr) == unreadable_line
              and item["mapping"].get(sig) == image_ocr.SIG_VALUE,
              json.dumps(item["mapping"])[:200])

        # --- per placeholder: la firma --------------------------------------------
        r = client.post(f"/api/chats/{cid}/staged/deanonymize",
                        json={"placeholder": sig}, headers=auth)
        it = item_by_kind(r.json(), "attachment") if r.status_code == 200 else {}
        check("deanonimizza [SIGNATURE_n]: 200 e box sparito",
              r.status_code == 200 and sig in r.json().get("removed", [])
              and not any(b["ph"] == sig for b in boxes_of(it))
              and sig not in it.get("mapping", {}),
              f"{r.status_code} {r.text[:160]}")
        check("la riga illeggibile resta coperta",
              any(b["ph"] == unr for b in boxes_of(it)))
        r = client.post(f"/api/chats/{cid}/staged/deanonymize",
                        json={"placeholder": sig}, headers=auth)
        check("seconda volta sulla stessa firma: 404", r.status_code == 404,
              f"{r.status_code} {r.text[:100]}")

        # --- per categoria: le righe illeggibili --------------------------------------
        r = client.post(f"/api/chats/{cid}/staged/deanonymize",
                        json={"label": "UNREADABLE"}, headers=auth)
        it = item_by_kind(r.json(), "attachment") if r.status_code == 200 else {}
        check("deanonimizza categoria UNREADABLE: 200 e box sparito",
              r.status_code == 200 and unr in r.json().get("removed", [])
              and not any(b["ph"] == unr for b in boxes_of(it)),
              f"{r.status_code} {r.text[:160]}")

        # --- ri-inclusione dalla selezione: stesso placeholder, niente CUSTOM ---------
        r = client.post(f"/api/chats/{cid}/staged/anonymize-text",
                        json={"text": unreadable_line}, headers=auth)
        it = item_by_kind(r.json(), "attachment") if r.status_code == 200 else {}
        check("riselezionare la riga illeggibile la ricopre come [UNREADABLE_n]",
              r.status_code == 200 and r.json().get("added") == unr
              and any(b["ph"] == unr and b.get("ocr") for b in boxes_of(it)),
              f"{r.status_code} {r.text[:160]}")
        check("nessun [CUSTOM_n] allocato per la lettura storpiata",
              not any(ph.startswith("[CUSTOM_") for ph in it.get("mapping", {})),
              json.dumps(it.get("mapping"))[:160])

        # --- voce inesistente -----------------------------------------------------------
        r = client.post(f"/api/chats/{cid}/staged/deanonymize",
                        json={"placeholder": "[SIGNATURE_99]"}, headers=auth)
        check("voce locale inesistente: 404", r.status_code == 404,
              f"{r.status_code}")
    finally:
        image_ocr._ocr = FakeOCR()
        image_ocr.detect_regions = lambda pil: []


def main():
    stop_mock = mock.serve()

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(chat_routes.router)
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)
        # il data dir del test è RIUSATO tra i run: un run precedente può
        # aver salvato "inventario ricambi" nei termini fissi (save_term) e
        # lo staging lo anonimizzerebbe subito, rompendo anonymize-text.
        # Vanno azzerati ENTRAMBI i livelli: save_term scrive sulla
        # lista personale, e ripulire solo quella globale non basta più.
        settings_store.set_anon_defaults(s, [], [])
        settings_store.set_user_terms(
            s, s.query(User).filter_by(username="admin").one(), [])

    client = TestClient(app)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    mock.grant_key(SessionLocal)

    # --- conversazione anonimizzata + allegato -------------------------------
    conv = client.post("/api/chats", json={"anonymized": True},
                       headers=auth).json()
    cid = conv["id"]
    client.patch(f"/api/chats/{cid}", json={"model": "test/plain-model"},
                 headers=auth)
    att = client.post(
        f"/api/chats/{cid}/attachments",
        files={"file": ("clienti.txt", TXT.encode(), "text/plain")},
        headers=auth).json()
    check("upload allegato pending", att["anonymization_status"] == "pending")
    check("niente nome anonimizzato da pending",
          att["anonymized_filename"] is None)
    r = client.get(f"/api/chats/{cid}/attachments/{att['id']}/anonymized",
                   headers=auth)
    check("download anonimizzato: 404 prima dell'anonimizzazione",
          r.status_code == 404)

    # --- preparazione (preview=True): niente parte ---------------------------
    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": PROMPT, "preview": True},
                       headers=auth) as resp:
        events = sse_events(resp)
    check("stream di anteprima: anon_start",
          bool(of_type(events, "anon_start")))
    check("stream di anteprima: nessun turno modello",
          not of_type(events, "start") and not of_type(events, "done"))
    staged_evs = of_type(events, "staged")
    check("evento staged finale", len(staged_evs) == 1,
          json.dumps([e["type"] for e in events]))
    staged = staged_evs[0]
    check("staged con 2 elementi (file + messaggio)",
          staged["total"] == 2 and len(staged["items"]) == 2)
    check("niente inviato al finto OpenRouter", len(sent()) == n_reqs)

    prompt_item = item_by_kind(staged, "prompt")
    file_item = item_by_kind(staged, "attachment")
    check("item del messaggio per ultimo",
          staged["items"][-1]["kind"] == "prompt")
    check("mappa del turno non vuota",
          len(prompt_item["mapping"]) >= 1 and len(file_item["mapping"]) >= 1,
          f"prompt={list(prompt_item['mapping'])} "
          f"file={list(file_item['mapping'])}")
    check("box su entrambi i lati (file)",
          file_item["original_boxes"] and file_item["anonymized_boxes"])
    check("dimensioni pagina per lato",
          len(prompt_item["page_sizes"]["original"]) >= 1
          and len(prompt_item["page_sizes"]["anonymized"]) >= 1)
    check("allegato protetto nello staged",
          staged["attachments"][0]["anonymization_status"] == "protected")

    full = client.get(f"/api/chats/{cid}", headers=auth).json()
    check("nessun messaggio persistito",
          full["messages"] == [] and
          full["attachments"][0]["message_id"] is None)
    check("nome proposto per il download anonimizzato",
          full["attachments"][0]["anonymized_filename"]
          == "clienti_anonimizzato.txt")

    # --- download della copia redatta (scritta dallo staging) ----------------
    r = client.get(f"/api/chats/{cid}/attachments/{att['id']}/anonymized",
                   headers=auth)
    check("download anonimizzato dopo lo staging", r.status_code == 200
          and "clienti_anonimizzato.txt" in r.headers["content-disposition"])
    check("la copia redatta non contiene i valori reali",
          "Mario Rossi" not in r.text and "[" in r.text, r.text[:80])
    r = client.get(f"/api/chats/{cid}/attachments/{att['id']}",
                   headers=auth)
    check("il download originale resta l'originale",
          r.status_code == 200 and "Mario Rossi" in r.text)

    # --- pagine PNG + estrazione ---------------------------------------------
    ok_png = True
    for item in staged["items"]:
        for source in ("original", "anonymized"):
            r = client.get(f"/api/chats/{cid}/staged/{item['id']}"
                           f"/pages/{source}/0.png", headers=auth)
            ok_png = ok_png and r.status_code == 200 \
                and r.headers["content-type"] == "image/png"
    check("pagine PNG di tutti gli item", ok_png)

    size = prompt_item["page_sizes"]["anonymized"][0]
    r = client.post(f"/api/chats/{cid}/staged/prompt/extract",
                    json={"source": "anonymized", "page": 0,
                          "rect": [0, 0, size["width"], size["height"]]},
                    headers=auth)
    check("estrazione da rettangolo", r.status_code == 200
          and "[" in r.json()["text"], r.json().get("text", "")[:60])

    # --- anonimizza in più ([CUSTOM_n] nel registro) -------------------------
    # save_term: la checkbox «anonimizza anche in futuro» del popup aggiunge il
    # testo ai termini fissi PERSONALI di chi clicca (non alla
    # lista globale dell'admin: vedi user_terms_test)
    def saved_terms():
        with SessionLocal() as s:
            return settings_store.user_terms(
                s.query(User).filter_by(username="admin").one())

    # (la tabula rasa è a inizio main(): qui il turno è già stato preparato)
    r = client.post(f"/api/chats/{cid}/staged/anonymize-text",
                    json={"text": "inventario ricambi", "save_term": True,
                          "term_tag": "nota interna"},
                    headers=auth)
    # col tag scelto il segnaposto è [NOTAINTERNA_n], non [CUSTOM_n]: chiedere
    # un tag e poi ignorarlo nell'anteprima non avrebbe senso (e i file
    # successivi userebbero comunque NOTAINTERNA per lo stesso termine)
    check("anonimizza selezione", r.status_code == 200
          and r.json().get("added") == "[NOTAINTERNA_1]"
          and r.json()["rev"] == 1, r.text[:120])
    check("save_term: termine nei propri termini fissi col tag scelto",
          r.json().get("saved_term") is True and saved_terms()
          == [{"text": "inventario ricambi", "tag": "NOTAINTERNA"}],
          saved_terms())
    staged = r.json()
    prompt_item = item_by_kind(staged, "prompt")
    file_item = item_by_kind(staged, "attachment")
    check("custom in mappa su messaggio E file",
          "[NOTAINTERNA_1]" in prompt_item["mapping"]
          and "[NOTAINTERNA_1]" in file_item["mapping"],
          f"file mapping={list(file_item['mapping'])}")

    r = client.post(f"/api/chats/{cid}/staged/anonymize-text",
                    json={"text": "inventario ricambi", "save_term": True},
                    headers=auth)
    check("doppione rifiutato (409)", r.status_code == 409, r.text[:80])
    check("409: nessun doppione nei termini", len(saved_terms()) == 1)
    r = client.post(f"/api/chats/{cid}/staged/anonymize-text",
                    json={"text": "consegnare al cliente", "save_term": True,
                          "term_tag": "!!!"}, headers=auth)
    check("tag invalido -> 422 senza toccare il turno", r.status_code == 422
          and len(saved_terms()) == 1, r.text[:100])
    r = client.post(f"/api/chats/{cid}/staged/anonymize-text",
                    json={"text": "testo che non esiste da nessuna parte"},
                    headers=auth)
    check("valore inesistente rifiutato (422)", r.status_code == 422)

    # --- deanonimizza (entità excluded) --------------------------------------
    target_ph = next((ph for ph, v in prompt_item["mapping"].items()
                      if v == "Mario Rossi"), None) \
        or next(ph for ph in prompt_item["mapping"] if ph != "[NOTAINTERNA_1]")
    target_val = prompt_item["mapping"][target_ph]
    r = client.post(f"/api/chats/{cid}/staged/deanonymize",
                    json={"placeholder": target_ph}, headers=auth)
    check("deanonimizza segnaposto", r.status_code == 200
          and target_ph in r.json().get("removed", []), r.text[:120])
    staged = r.json()
    prompt_item = item_by_kind(staged, "prompt")
    check("segnaposto sparito dalla mappa del turno",
          target_ph not in prompt_item["mapping"]
          and target_ph not in item_by_kind(staged, "attachment")["mapping"])

    # ri-inclusione: anonimizzare di nuovo lo stesso valore riusa l'entità
    r = client.post(f"/api/chats/{cid}/staged/anonymize-text",
                    json={"text": target_val}, headers=auth)
    check("ri-inclusione riusa il placeholder", r.status_code == 200
          and r.json().get("added") == target_ph, r.text[:120])
    check("senza save_term i termini fissi non cambiano", len(saved_terms()) == 1)
    # e si ri-deanonimizza per l'invio finale (verifica dell'egress check)
    r = client.post(f"/api/chats/{cid}/staged/deanonymize",
                    json={"placeholder": target_ph}, headers=auth)
    check("seconda deanonimizzazione", r.status_code == 200)
    staged = r.json()

    # --- invio from_staged -----------------------------------------------------
    r = client.post(f"/api/chats/{cid}/messages",
                    json={"content": PROMPT + " x", "from_staged": True},
                    headers=auth)
    check("contenuto diverso dall'anteprima rifiutato (409)",
          r.status_code == 409, r.text[:80])
    # il 409 ha scartato l'anteprima: va rigenerata prima dell'invio vero
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": PROMPT, "preview": True},
                       headers=auth) as resp:
        events = sse_events(resp)
    staged = of_type(events, "staged")[0]
    prompt_item = item_by_kind(staged, "prompt")
    check("rigenerata: il valore deanonimizzato resta in chiaro",
          target_ph not in prompt_item["mapping"]
          and "[NOTAINTERNA_1]" in prompt_item["mapping"],
          json.dumps(prompt_item["mapping"], ensure_ascii=False))

    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": PROMPT, "from_staged": True},
                       headers=auth) as resp:
        events = sse_events(resp)
    check("invio from_staged: niente ri-anonimizzazione",
          not of_type(events, "anon_start")
          and events[0]["type"] == "turn"       # apertura per il riaggancio
          and events[1]["type"] == "start", events[0]["type"])
    check("turno completato", events[-1]["type"] == "done")

    req = sent()[n_reqs]
    txt = user_text(req)
    check("custom anonimizzato verso il modello",
          "[NOTAINTERNA_1]" in txt and "inventario ricambi" not in txt)
    check("valore deanonimizzato in chiaro verso il modello (egress ok)",
          target_val in txt, f"cercato {target_val!r}")
    check("scheda allegato nel contesto", "<allegati>" in txt)

    full = client.get(f"/api/chats/{cid}", headers=auth).json()
    check("turno persistito e allegato agganciato",
          [m["role"] for m in full["messages"]] == ["user", "assistant"]
          and full["attachments"][0]["message_id"] is not None)
    r = client.get(f"/api/chats/{cid}/staged/prompt/pages/anonymized/0.png",
                   headers=auth)
    check("anteprima scartata dopo l'invio", r.status_code == 404)

    # --- un turno non deve poter riproporre le pagine del precedente ----------
    # L'URL di un pezzo dell'anteprima è IDENTICO a ogni turno (stessa chat,
    # item "prompt", rev che riparte da 0): se la risposta è memorizzabile, dal
    # secondo messaggio in poi il browser rimostrerebbe il PRIMO.
    def stage_prompt_png(text):
        with client.stream("POST", f"/api/chats/{cid}/messages",
                           json={"content": text, "preview": True},
                           headers=auth) as resp:
            sse_events(resp)
        return client.get(f"/api/chats/{cid}/staged/prompt"
                          f"/pages/anonymized/0.png", headers=auth)

    r1 = stage_prompt_png("Primo messaggio: parliamo di Luca Bianchi.")
    r2 = stage_prompt_png("Secondo messaggio: tutt'altro argomento, i motori.")
    check("pagina del prompt diversa a ogni turno",
          r1.status_code == 200 and r2.status_code == 200
          and r1.content != r2.content,
          f"{r1.status_code}/{r2.status_code}")
    check("pagine dell'anteprima non memorizzabili dal browser",
          "no-store" in r2.headers.get("cache-control", ""),
          r2.headers.get("cache-control", ""))

    # --- invalidazioni ----------------------------------------------------------
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": PROMPT, "preview": True},
                       headers=auth) as resp:
        events = sse_events(resp)
    check("seconda anteprima pronta", bool(of_type(events, "staged")))
    client.post(f"/api/chats/{cid}/attachments",
                files={"file": ("nota.txt", b"nota senza pii", "text/plain")},
                headers=auth)
    r = client.post(f"/api/chats/{cid}/messages",
                    json={"content": PROMPT, "from_staged": True},
                    headers=auth)
    check("upload nuovo invalida l'anteprima (409)", r.status_code == 409,
          r.text[:80])

    ocr_corpus_case(client, auth, SessionLocal)
    ocr_deanonymize_case(client, auth)
    ocr_local_deanonymize_case(client, auth)

    stop_mock()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

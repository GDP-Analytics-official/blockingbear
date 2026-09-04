"""Colonne xlsx e rielaborazione con OCR nell'ANTEPRIMA PRE-INVIO della chat.

Sono le due funzioni che esistevano solo sulla preview dei file di progetto:
qui si verifica che nell'anteprima di un turno facciano la stessa cosa, con la
stessa grammatica (stessi contratti del viewer) e sul registro della
conversazione invece che su quello del progetto.

Si lavora sugli asset VERSIONATI di assets/, così la suite gira su qualunque
clone del repository:
  - listino_marchi.xlsx     colonne (un foglio, quattro colonne)
  - scansione_nuda.pdf      scansione senza layer testuale
  - (parte 3) un PDF costruito col ritaglio di quella scansione dentro una
    pagina che il testo ce l'ha: è l'unico modo di arrivare in anteprima
    SENZA cache OCR, cioè il caso che la rielaborazione deve rimediare.

Modello PII e OCR sono quelli veri: il test dura qualche minuto. Si può
eseguire una parte per volta:

    python backend/tests/chat_column_ocr_test.py [xlsx|ocr|mixed]
"""
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import chat_mock_openrouter as mock                              # noqa: E402

DATA_DIR = HERE / "data" / "test_chat_column_ocr"
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA_DIR)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_OPENROUTER_BASE_URL"] = mock.BASE_URL

import fitz                                                      # noqa: E402
from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import chat_staging, jobs, settings_store               # noqa: E402
from app.auth import seed_admin                                  # noqa: E402
from app.db import Job, init_db                                  # noqa: E402
from app.engine import convert, image_ocr                        # noqa: E402
from app.engine.xlsx import extract_text as extract_xlsx         # noqa: E402
from app.openrouter import client as or_client                   # noqa: E402
from app.routes import auth_routes, chat_routes                  # noqa: E402

# profilo LibreOffice isolato: il backend di sviluppo può essere acceso e i
# due processi non possono condividerlo
convert._PROFILE_DIR = DATA_DIR / "lo_profile"

XLSX = Path(__file__).resolve().parent / "assets" / "listino_marchi.xlsx"
# scansione SENZA layer testuale: la prima prova della parte OCR è proprio che
# senza OCR il turno si fermi, e serve un file in cui non c'è testo da leggere.
SCAN = Path(__file__).resolve().parent / "assets" / "scansione_nuda.pdf"

SHEET = "Listino"
# "Codice a barre" la copre già il percorso tabellare automatico; "Modello"
# e "Marchio" no — ed è esattamente il caso in cui serve l'azione manuale:
# l'utente vede una colonna scoperta e la anonimizza per intero.
# "SKU" NON è qui: la classificazione la manda al percorso "modello sugli
# unici" (non "column"), quindi i suoi valori entrano in mappa ma la colonna
# non viene dichiarata in table_columns. Quale soglia debba coprirla è una
# domanda aperta, non un'aspettativa di questo test.
COLUMN, HEADER = "D", "Modello"
# la colonna ha un valore distinto per riga: i conteggi dell'anonimizzazione
# e della deanonimizzazione valgono N_VALUES, non 1.
N_VALUES = 5
VALUE = "Serie 10"
# la seconda colonna serve solo a verificare che le informazioni tabellari si
# ACCUMULINO. Su questo asset "Marchio" non va bene: i tre marchi il modello li
# rileva uno per uno, la colonna risulta già coperta e l'azione manuale
# risponde 409. "SKU" invece resta interamente scoperta (0 su 5 in mappa).
COLUMN2, HEADER2 = "B", "SKU"
# Nessuna colonna arriva coperta dal percorso tabellare AUTOMATICO: con cinque
# righe il foglio sta sotto la soglia a cui quel percorso si attiva, e infatti
# table_columns nasce vuoto. Qui si prova la strada MANUALE — l'utente vede una
# colonna scoperta e la copre per intero; il percorso automatico lo provano
# column_smoke_test.py e xlsx_header_test.py.
MIXED_IMG_RECT = [40, 140, 555, 420]

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
    out = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def of_type(events, t):
    return [e for e in events if e["type"] == t]


def stage(client, auth, cid, content, ocr=False):
    """Prepara il turno in anteprima e ritorna (stato staged | None, eventi)."""
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": content, "preview": True, "ocr": ocr},
                       headers=auth) as resp:
        events = sse_events(resp)
    staged = of_type(events, "staged")
    return (staged[0] if staged else None), events


def attachment_item(staged):
    return next(i for i in staged["items"] if i["kind"] == "attachment")


def new_chat(client, auth, filename, data, mime):
    conv = client.post("/api/chats", json={"anonymized": True},
                       headers=auth).json()
    cid = conv["id"]
    client.patch(f"/api/chats/{cid}", json={"model": "test/plain-model"},
                 headers=auth)
    att = client.post(f"/api/chats/{cid}/attachments",
                      files={"file": (filename, data, mime)},
                      headers=auth).json()
    return cid, att


def protected_bytes(client, auth, cid, att_id):
    r = client.get(f"/api/chats/{cid}/attachments/{att_id}/anonymized",
                   headers=auth)
    return r.content if r.status_code == 200 else b""


def _mixed_pdf():
    """Pagina con testo VERO + un'immagine che contiene testo (il ritaglio in
    alto della scansione). Con l'OCR spento il turno passa (il layer testuale
    c'è) ma nei pixel non si redige niente: è il caso che «Rielabora con
    OCR» deve risolvere."""
    with fitz.open(SCAN) as src:
        page = src[0]
        clip = fitz.Rect(0, 0, page.rect.width, page.rect.height * 0.26)
        png = page.get_pixmap(matrix=fitz.Matrix(1.7, 1.7),
                              clip=clip).tobytes("png")
    out = fitz.open()
    page = out.new_page(width=595, height=842)
    page.insert_text((60, 80), "Verbale interno di prova.", fontsize=12)
    page.insert_text((60, 102), "Referente: Giulia Neri, Bologna.", fontsize=12)
    page.insert_image(fitz.Rect(*MIXED_IMG_RECT), stream=png)
    data = out.tobytes()
    out.close()
    return data


# --- 1. colonne xlsx ---------------------------------------------------------

def part_xlsx(client, auth):
    print("\n--- colonne xlsx nell'anteprima della chat "
          f"({XLSX.name}) ---")
    cid, att = new_chat(
        client, auth, XLSX.name, XLSX.read_bytes(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    staged, events = stage(client, auth, cid,
                           "Riassumi il listino allegato.")
    if staged is None:
        check("anteprima preparata", False,
              json.dumps([e.get("message", e["type"]) for e in events])[:200])
        return
    item = attachment_item(staged)
    iid = item["id"]
    base = f"/api/chats/{cid}/staged/{iid}"
    check("anteprima preparata (file + messaggio)", staged["total"] == 2)
    check("estensione effettiva dichiarata al viewer", item["ext"] == ".xlsx",
          str(item.get("ext")))

    # --- layout delle colonne cliccabili -------------------------------------
    r = client.get(f"{base}/columns", headers=auth)
    layout = r.json()
    pages_o = layout.get("original") or {}
    pages_a = layout.get("anonymized") or {}
    check("colonne cliccabili su entrambe le anteprime",
          r.status_code == 200 and pages_o and pages_a,
          f"orig={list(pages_o)} anon={list(pages_a)}")
    first = pages_o.get(next(iter(pages_o), None)) or {}
    check("foglio e colonne riconosciuti",
          first.get("sheet") == SHEET and len(first.get("cols") or []) >= 1,
          str(first.get("sheet")))
    r = client.get(f"/api/chats/{cid}/staged/prompt/columns", headers=auth)
    check("il messaggio non ha colonne",
          r.json() == {"original": {}, "anonymized": {}}, r.text[:80])

    # --- conteggi della colonna ----------------------------------------------
    r = client.post(f"{base}/column-info", headers=auth,
                    json={"sheet": SHEET, "column": COLUMN})
    info = r.json()
    check("column-info legge il file intero, non l'anteprima troncata",
          r.status_code == 200 and info.get("header") == HEADER
          and info.get("values") == N_VALUES
          and info.get("usable") == N_VALUES,
          r.text[:160])
    check("la colonna «Modello» è rimasta scoperta dall'automatico",
          info.get("mapped") == 0, r.text[:160])

    # --- anonimizzazione della colonna ---------------------------------------
    before = extract_xlsx(protected_bytes(client, auth, cid, att["id"]),
                          full=True)
    check("il valore era in chiaro nella copia protetta", VALUE in before)
    r = client.post(f"{base}/anonymize-column", headers=auth,
                    json={"sheet": SHEET, "column": COLUMN})
    check("anonimizza-colonna: risposta sincrona con lo stato staged",
          r.status_code == 200 and "items" in r.json(),
          f"{r.status_code} {r.text[:160]}")
    if r.status_code != 200:
        return
    desc = r.json()
    check("un segnaposto nuovo per ogni valore della colonna",
          desc.get("added_column") == N_VALUES,
          str(desc.get("added_column")))
    item = attachment_item(desc)
    check("anteprima rigenerata (rev avanzata)", desc["rev"] >= 1,
          str(desc["rev"]))
    check("colonna dichiarata in anteprima",
          any(c.get("column") == HEADER
              for c in (item["report"].get("table_columns") or [])),
          str(item["report"].get("table_columns"))[:200])

    after = extract_xlsx(protected_bytes(client, auth, cid, att["id"]),
                         full=True)
    check("il valore non è più nella copia protetta", VALUE not in after)
    check("al suo posto c'è un segnaposto CUSTOM", "[CUSTOM_" in after)

    # il registro della conversazione ha imparato davvero
    ents = client.get(f"/api/chats/{cid}/entities", headers=auth).json()
    check("il valore è nel registro della conversazione",
          any(e["value"] == VALUE for e in ents["entities"]),
          str([e["value"] for e in ents["entities"]][:3]))

    r = client.post(f"{base}/column-info", headers=auth,
                    json={"sheet": SHEET, "column": COLUMN})
    check("column-info: ora la colonna risulta mappata",
          r.json().get("mapped") == N_VALUES, r.text[:160])

    # --- seconda volta: non c'è più niente da coprire ----------------------
    r = client.post(f"{base}/anonymize-column", headers=auth,
                    json={"sheet": SHEET, "column": COLUMN})
    check("colonna già anonimizzata: 409 con spiegazione",
          r.status_code == 409 and "già" in r.text, r.text[:120])

    # --- una seconda colonna, e le informazioni tabellari si accumulano ------
    r = client.post(f"{base}/anonymize-column", headers=auth,
                    json={"sheet": SHEET, "column": COLUMN2})
    check("seconda colonna anonimizzata", r.status_code == 200,
          f"{r.status_code} {r.text[:120]}")
    if r.status_code == 200:
        cols = [c.get("column") for c in
                (attachment_item(r.json())["report"].get("table_columns") or [])]
        check("tutte le colonne restano dichiarate dopo la ri-redazione",
              all(c in cols for c in (HEADER, HEADER2)), str(cols))

    # --- una modifica NON di colonna non deve perdere quell'informazione -----
    # un placeholder qualunque, purché NON sia uno di quelli creati dalle due
    # colonne fatte a mano: la ri-redazione che ne segue non è un'operazione di
    # colonna, e non deve far perdere le informazioni tabellari.
    ph = next((e["placeholder"] for e in
               client.get(f"/api/chats/{cid}/entities",
                          headers=auth).json()["entities"]
               if e["label"] not in ("CUSTOM",)), None)
    if ph:
        r = client.post(f"/api/chats/{cid}/staged/deanonymize", headers=auth,
                        json={"placeholder": ph})
        check("deanonimizza un segnaposto qualunque", r.status_code == 200,
              r.text[:120])
        if r.status_code == 200:
            cols = [c.get("column") for c in
                    (attachment_item(r.json())["report"]
                     .get("table_columns") or [])]
            check("le colonne tabellari sopravvivono a ogni ri-redazione",
                  HEADER in cols and HEADER2 in cols, str(cols))

    # --- deanonimizzazione dell'intera colonna -------------------------------
    r = client.post(f"{base}/deanonymize-column", headers=auth,
                    json={"sheet": SHEET, "column": COLUMN})
    check("deanonimizza-colonna: stato staged aggiornato",
          r.status_code == 200
          and len(r.json().get("removed") or []) == N_VALUES,
          f"{r.status_code} {r.text[:120]}")
    clear = extract_xlsx(protected_bytes(client, auth, cid, att["id"]),
                         full=True)
    check("la colonna è di nuovo in chiaro", VALUE in clear)
    r = client.post(f"{base}/deanonymize-column", headers=auth,
                    json={"sheet": SHEET, "column": COLUMN})
    check("niente da deanonimizzare: 404 con spiegazione",
          r.status_code == 404, r.text[:120])

    # --- guardie -------------------------------------------------------------
    r = client.post(f"/api/chats/{cid}/staged/prompt/anonymize-column",
                    headers=auth, json={"sheet": SHEET, "column": COLUMN})
    check("colonne rifiutate sul messaggio (422)", r.status_code == 422,
          r.text[:120])
    r = client.post(f"{base}/anonymize-column", headers=auth,
                    json={"sheet": SHEET, "column": "1"})
    check("colonna non valida (422)", r.status_code == 422, r.text[:120])
    client.delete(f"/api/chats/{cid}/staged", headers=auth)
    r = client.post(f"{base}/anonymize-column", headers=auth,
                    json={"sheet": SHEET, "column": COLUMN})
    check("senza anteprima: 409 «ripeti l'invio»", r.status_code == 409,
          r.text[:120])


# --- 2. OCR su una scansione vera -------------------------------------------

def part_ocr(client, auth):
    print(f"\n--- OCR nell'anteprima della chat ({SCAN.name}) ---")
    cid, att = new_chat(client, auth, SCAN.name, SCAN.read_bytes(),
                        "application/pdf")

    # senza OCR una scansione non ha nulla da anonimizzare: il turno si ferma
    staged, events = stage(client, auth, cid, "Riassumi l'incarico allegato.")
    errs = of_type(events, "error")
    check("scansione senza OCR: turno fermato con spiegazione",
          staged is None and errs and "testo" in errs[0]["message"],
          (errs[0]["message"][:120] if errs else "nessun errore"))

    staged, events = stage(client, auth, cid, "Riassumi l'incarico allegato.",
                           ocr=True)
    if staged is None:
        check("anteprima con OCR preparata", False,
              json.dumps([e.get("message", e["type"]) for e in events])[:200])
        return
    item = attachment_item(staged)
    iid = item["id"]
    base = f"/api/chats/{cid}/staged/{iid}"
    check("anteprima con OCR preparata", staged["total"] == 2)
    check("entità lette nei pixel e coperte",
          item["n_entities"] > 0 and any(item["anonymized_boxes"].values()),
          f"n={item['n_entities']}")
    check("cache OCR persistita accanto all'allegato",
          (DATA_DIR / "chats" / cid / f"{att['id']}_ocr.json").is_file())

    # selezione su un'area della scansione: il testo lo dà l'OCR
    r = client.post(f"{base}/extract", headers=auth, json={
        "source": "original", "page": 0, "rect": [60, 60, 540, 200]})
    j = r.json()
    check("selezione: fallback OCR sul ritaglio",
          r.status_code == 200 and j["text"] and j["ocr"] is True, r.text[:160])
    check("selezione redigibile (c'è la cache OCR)",
          j.get("ocr_redactable") is True, r.text[:160])

    # --- la rielaborazione va in coda come i caricamenti ---------------------
    r = client.post(f"{base}/reprocess-ocr", headers=auth)
    job_desc = r.json()
    check("reprocess-ocr accodato (202, kind=chat_reprocess)",
          r.status_code == 202 and job_desc.get("kind") == "chat_reprocess",
          f"{r.status_code} {r.text[:160]}")
    check("allegato occupato durante il job (doc_busy)",
          jobs.doc_busy(att["id"]))
    r = client.post(f"/api/chats/{cid}/staged/deanonymize", headers=auth,
                    json={"label": "FULLNAME"})
    check("modifiche dell'anteprima rifiutate a job in corso (409)",
          r.status_code == 409, f"{r.status_code} {r.text[:120]}")
    r = client.post(f"{base}/reprocess-ocr", headers=auth)
    check("secondo reprocess rifiutato (409)", r.status_code == 409,
          f"{r.status_code} {r.text[:120]}")
    r = client.post(f"/api/chats/{cid}/messages",
                    json={"content": "x", "from_staged": True}, headers=auth)
    check("invio rifiutato a job in corso (409)", r.status_code == 409,
          f"{r.status_code} {r.text[:120]}")
    with init_db()() as s:
        jobs.cancel(s.get(Job, job_desc["id"]), s)   # nessun worker nel test
    check("annullamento libera l'allegato", not jobs.doc_busy(att["id"]))

    # --- il lavoro vero, come lo farebbe il worker ---------------------------
    rev_before = chat_staging.state(cid)["rev"]
    with init_db()() as s:
        out = chat_staging.reprocess_ocr(
            {"conv_id": cid, "item_id": iid, "att_id": att["id"]},
            jobs.ENGINES[0], s)
    check("reprocess_ocr rielabora lo STESSO allegato", out.id == att["id"])
    st = chat_staging.state(cid)
    check("turno ri-redatto (rev avanzata)", st["rev"] > rev_before,
          f"{rev_before} -> {st['rev']}")
    item = attachment_item(chat_staging.descriptor(cid))
    check("dopo la rielaborazione le coperture ci sono ancora",
          item["n_entities"] > 0 and any(item["anonymized_boxes"].values()),
          f"n={item['n_entities']}")
    check("cache OCR ancora al suo posto",
          (DATA_DIR / "chats" / cid / f"{att['id']}_ocr.json").is_file())


# --- 3. allegato preparato SENZA OCR: la rielaborazione lo rimedia ----------

def part_mixed(client, auth):
    print("\n--- rielaborazione con OCR di un allegato preparato senza OCR ---")
    cid, att = new_chat(client, auth, "verbale_misto.pdf", _mixed_pdf(),
                        "application/pdf")
    staged, events = stage(client, auth, cid, "Controlla il verbale allegato.")
    if staged is None:
        check("anteprima senza OCR preparata", False,
              json.dumps([e.get("message", e["type"]) for e in events])[:200])
        return
    item = attachment_item(staged)
    iid = item["id"]
    base = f"/api/chats/{cid}/staged/{iid}"
    cache = DATA_DIR / "chats" / cid / f"{att['id']}_ocr.json"
    check("anteprima preparata dal solo layer testuale", staged["total"] == 2)
    check("nessuna cache OCR (l'utente ha risposto «solo testo»)",
          not cache.is_file())

    r = client.post(f"{base}/extract", headers=auth, json={
        "source": "original", "page": 0, "rect": MIXED_IMG_RECT})
    j = r.json()
    read = (j.get("text") or "").strip()
    check("selezione sull'immagine: il testo arriva dall'OCR",
          r.status_code == 200 and read and j["ocr"] is True, r.text[:160])
    check("dichiarata NON redigibile: serve la rielaborazione",
          j.get("ocr_redactable") is False, r.text[:160])
    value = next((w for w in read.split() if len(w) > 5), "")
    r = client.post(f"/api/chats/{cid}/staged/anonymize-text", headers=auth,
                    json={"text": value})
    check("anonimizza-in-più rifiutato: il valore non è nel testo (422)",
          r.status_code == 422, f"{r.status_code} {r.text[:120]}")

    with init_db()() as s:
        chat_staging.reprocess_ocr(
            {"conv_id": cid, "item_id": iid, "att_id": att["id"]},
            jobs.ENGINES[0], s)
    check("la rielaborazione scrive la cache OCR", cache.is_file())
    lines = []
    if cache.is_file():
        blob = json.loads(cache.read_text(encoding="utf-8"))
        lines = [ln["t"] for img in blob["images"] for ln in img["lines"]]
    check("le righe della scansione sono state lette", len(lines) >= 5,
          str(lines[:3]))

    r = client.post(f"{base}/extract", headers=auth, json={
        "source": "original", "page": 0, "rect": MIXED_IMG_RECT})
    check("ora la selezione è redigibile",
          r.json().get("ocr_redactable") is True, r.text[:160])
    item = attachment_item(chat_staging.descriptor(cid))
    check("i box compaiono anche dentro l'immagine",
          any(b.get("ocr") for bl in item["anonymized_boxes"].values()
              for b in bl),
          str(list(item["anonymized_boxes"]))[:120])


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if not XLSX.is_file() or not SCAN.is_file():
        print(f"file di prova mancanti: {XLSX} / {SCAN}")
        return 1
    if not image_ocr.available():
        print("stack OCR non installato: le parti 2 e 3 non sono eseguibili")
        if which in ("ocr", "mixed"):
            return 1
    stop_mock = mock.serve()

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(chat_routes.router)
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)
        settings_store.set_anon_defaults(s, [], [])

    client = TestClient(app)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    mock.grant_key(SessionLocal)

    try:
        if which in ("all", "xlsx"):
            part_xlsx(client, auth)
        if which in ("all", "ocr"):
            part_ocr(client, auth)
        if which in ("all", "mixed"):
            part_mixed(client, auth)
    finally:
        stop_mock()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

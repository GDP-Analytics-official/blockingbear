"""Progetti (app/project_files.py + app/routes/projects.py + chat di
progetto) da un capo all'altro: app FastAPI ridotta (auth + chat + progetti),
finto OpenRouter e modello PII vero. Niente Docker: si usa test/plain-model
(senza tool), la sandbox non serve; l'elaborazione dei file viene invocata
direttamente (project_files.process_upload) invece che dalla coda job.

Copre: creazione progetto nei due modi, elaborazione file sul REGISTRO
condiviso (stesso placeholder in file diversi), descrittore/PNG/download,
modifiche pre-messaggi (anonimizza-in-più, deanonimizza, ri-inclusione),
controllo di uscita alla conferma + riallineamento, chat di progetto (modo
ereditato e bloccato, categorie gestite dal progetto, lista separata),
esposizione dei soli file confermati nel system prompt, coerenza dei
placeholder tra file e prompt, regola additiva dopo il primo messaggio,
sopravvivenza del registro alla cancellazione di una chat, progetto in
chiaro (file com'è, visibile subito), cancellazione del progetto.

Uso:
    python backend/tests/projects_test.py
"""
import base64
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import chat_mock_openrouter as mock                              # noqa: E402

os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_projects")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_OPENROUTER_BASE_URL"] = mock.BASE_URL

import fitz                                                      # noqa: E402
import httpx                                                     # noqa: E402
from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import jobs, project_files, settings_store              # noqa: E402
from app.auth import seed_admin                                  # noqa: E402
from app.db import ConversationEntity, init_db                   # noqa: E402
from app.openrouter import client as or_client                   # noqa: E402
from app.openrouter import sandbox                               # noqa: E402
from app.routes import auth_routes, chat_routes, projects        # noqa: E402

PASS = 0
FAIL = 0

FILE1 = ("Cliente: Mario Rossi\n"
         "Email: mario.rossi@example.com\n"
         "Nota: inventario ricambi da consegnare al cliente.\n")
FILE2 = ("Fornitore: Beta Costruzioni Srl\n"
         "Referente: Mario Rossi\n"
         "Nota: inventario ricambi in magazzino.\n")
PROMPT = "Riassumi i file del progetto e cita Mario Rossi nel report."


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


def system_text(request):
    return next(m["content"] for m in request["messages"]
                if m["role"] == "system")


def ph_for(mapping, value):
    return next((ph for ph, v in mapping.items() if value in v), None)


def seed_pair(SessionLocal, scope, short, full, n):
    """Due entità della stessa label che il resolver tiene separate ma che
    merge_suggestions propone (insieme di token della prima incluso nella
    seconda). Sono righe di registro come quelle che l'anonimizzazione scrive:
    quello che si prova qui è il flusso, non il rilevatore."""
    with SessionLocal() as session:
        rows = [ConversationEntity(conv_id=scope, label="FULLNAME",
                                   placeholder=f"[FULLNAME_{n}]",
                                   canonical_value=short),
                ConversationEntity(conv_id=scope, label="FULLNAME",
                                   placeholder=f"[FULLNAME_{n + 1}]",
                                   canonical_value=full)]
        session.add_all(rows)
        session.commit()
        return {r.canonical_value: r.id for r in rows}


def main():
    stop_mock = mock.serve()

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(chat_routes.router)
    app.include_router(projects.router)
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)
        # data dir riusato tra i run: niente termini fissi residui
        settings_store.set_anon_defaults(s, [], [])

    client = TestClient(app)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    mock.grant_key(SessionLocal)

    engine = jobs.ENGINES[0]
    # questa macchina può avere Docker: sandbox spenta, così il finto
    # happy-model (multimodale) risponde in modalità puramente testuale
    sandbox.status = lambda: {"available": False, "detecting": False}

    # --- creazione -----------------------------------------------------------
    p = client.post("/api/projects", json={
        "name": "Commessa Alfa", "anonymized": True}, headers=auth).json()
    pid = p["id"]
    check("progetto anonimizzato creato",
          p["anonymized"] is True and p["n_files"] == 0 and p["n_chats"] == 0)
    lst = client.get("/api/projects", headers=auth).json()
    check("lista progetti", any(x["id"] == pid for x in lst))

    # --- elaborazione file 1 (direttamente, senza coda) ------------------------
    with SessionLocal() as s:
        pf1 = project_files.process_upload(
            FILE1.encode(), "clienti.txt", pid, engine, s)
        pf1_id = pf1.id
    d1 = client.get(f"/api/projects/{pid}/files/{pf1_id}",
                    headers=auth).json()
    check("file 1 elaborato, non confermato",
          d1["confirmed"] is False and d1["n_pages"] >= 1)
    ph_rossi = ph_for(d1["mapping"], "Mario Rossi")
    check("mappa del file 1 con Mario Rossi", ph_rossi is not None,
          json.dumps(d1["mapping"]))
    check("box su entrambi i lati",
          d1["original_boxes"] and d1["anonymized_boxes"])
    r = client.get(f"/api/projects/{pid}/files/{pf1_id}"
                   "/pages/anonymized/0.png", headers=auth)
    check("pagina PNG", r.status_code == 200
          and r.headers["content-type"] == "image/png")
    r = client.get(f"/api/projects/{pid}/files/{pf1_id}/download",
                   headers=auth)
    check("download anonimizzato senza valori reali",
          r.status_code == 200 and "Mario Rossi" not in r.text
          and ph_rossi in r.text, r.text[:80])
    r = client.get(f"/api/projects/{pid}/files/{pf1_id}/download-original",
                   headers=auth)
    check("download originale intatto", "Mario Rossi" in r.text)

    # --- file 2: REGISTRO condiviso -------------------------------------------
    with SessionLocal() as s:
        pf2 = project_files.process_upload(
            FILE2.encode(), "fornitori.txt", pid, engine, s)
        pf2_id = pf2.id
    d2 = client.get(f"/api/projects/{pid}/files/{pf2_id}",
                    headers=auth).json()
    check("stesso placeholder per Mario Rossi nei due file",
          ph_for(d2["mapping"], "Mario Rossi") == ph_rossi,
          json.dumps(d2["mapping"]))

    # --- modifiche pre-messaggi -------------------------------------------------
    # anonimizza-in-più su file 2: entra nel registro del progetto
    r = client.post(f"/api/projects/{pid}/files/{pf2_id}/anonymize-text",
                    json={"text": "inventario ricambi"}, headers=auth)
    check("anonimizza-in-più (CUSTOM nel registro)", r.status_code == 200
          and r.json().get("added", "").startswith("[CUSTOM_"),
          r.text[:120])
    custom_ph = r.json()["added"]
    r = client.get(f"/api/projects/{pid}/files/{pf2_id}/download",
                   headers=auth)
    check("file 2 ri-redatto col termine custom",
          "inventario ricambi" not in r.text and custom_ph in r.text)

    # conferma file 1: il registro conosce ora una superficie che il file
    # contiene ancora in chiaro -> controllo di uscita KO, poi riallineamento
    r = client.post(f"/api/projects/{pid}/files/{pf1_id}/confirm",
                    headers=auth)
    check("conferma bloccata dal controllo di uscita", r.status_code == 409,
          r.text[:120])
    r = client.post(f"/api/projects/{pid}/files/{pf1_id}/realign",
                    headers=auth)
    check("riallineamento del file non confermato", r.status_code == 200)
    r = client.post(f"/api/projects/{pid}/files/{pf1_id}/confirm",
                    headers=auth)
    check("conferma dopo il riallineamento", r.status_code == 200)
    r = client.post(f"/api/projects/{pid}/files/{pf1_id}/realign",
                    headers=auth)
    check("riallineamento rifiutato su file confermato", r.status_code == 409)

    # deanonimizza (distruttiva, ammessa: nessun messaggio ancora)
    r = client.post(f"/api/projects/{pid}/files/{pf2_id}/deanonymize",
                    json={"placeholder": custom_ph}, headers=auth)
    check("deanonimizza pre-messaggi", r.status_code == 200
          and custom_ph in r.json().get("removed", []), r.text[:120])
    r = client.get(f"/api/projects/{pid}/files/{pf2_id}/download",
                   headers=auth)
    check("valore escluso tornato in chiaro", "inventario ricambi" in r.text)
    # ri-inclusione: l'inverso esatto
    r = client.post(f"/api/projects/{pid}/files/{pf2_id}/anonymize-text",
                    json={"text": "inventario ricambi"}, headers=auth)
    check("ri-inclusione riusa il placeholder", r.status_code == 200
          and r.json().get("added") == custom_ph, r.text[:120])
    r = client.post(f"/api/projects/{pid}/files/{pf2_id}/confirm",
                    headers=auth)
    check("conferma file 2", r.status_code == 200)

    # «non mostrare più l'avviso sui file da confermare»: preferenza DEL
    # progetto (il frontend la legge per decidere se aprire il popup)
    p0 = client.get(f"/api/projects/{pid}", headers=auth).json()
    check("avviso file da confermare attivo di default",
          p0.get("skip_unconfirmed_warning") is False,
          json.dumps(p0.get("skip_unconfirmed_warning")))
    r = client.patch(f"/api/projects/{pid}",
                     json={"skip_unconfirmed_warning": True}, headers=auth)
    check("avviso disattivabile via PATCH", r.status_code == 200
          and r.json().get("skip_unconfirmed_warning") is True, r.text[:120])
    p0 = client.get(f"/api/projects/{pid}", headers=auth).json()
    check("scelta ricordata dal progetto",
          p0.get("skip_unconfirmed_warning") is True)
    r = client.patch(f"/api/projects/{pid}", json={"name": p0["name"]},
                     headers=auth)
    check("la scelta sopravvive a una PATCH che non la tocca",
          r.json().get("skip_unconfirmed_warning") is True)
    client.patch(f"/api/projects/{pid}",
                 json={"skip_unconfirmed_warning": False}, headers=auth)

    # --- chat di progetto ---------------------------------------------------------
    conv = client.post("/api/chats", json={
        "project_id": pid, "anonymized": False}, headers=auth).json()
    cid = conv["id"]
    check("chat di progetto eredita il modo (anonimizzata)",
          conv["anonymized"] is True and conv["project_id"] == pid)
    free = client.get("/api/chats", headers=auth).json()
    check("chat di progetto fuori dalla lista libera",
          all(c["id"] != cid for c in free))
    inproj = client.get(f"/api/chats?project={pid}", headers=auth).json()
    check("chat nella lista del progetto",
          any(c["id"] == cid for c in inproj))
    r = client.patch(f"/api/chats/{cid}", json={"anonymized": False},
                     headers=auth)
    check("modo della chat di progetto non cambiabile", r.status_code == 409)
    r = client.patch(f"/api/chats/{cid}",
                     json={"anon_options": {"excluded_tags": []}},
                     headers=auth)
    check("categorie gestite dal progetto (PATCH chat rifiutato)",
          r.status_code == 409)
    client.patch(f"/api/chats/{cid}", json={"model": "test/plain-model"},
                 headers=auth)

    # --- turno: file confermati nel system prompt, placeholder coerenti -----------
    n_reqs = len(sent())
    with client.stream("POST", f"/api/chats/{cid}/messages",
                       json={"content": PROMPT}, headers=auth) as resp:
        events = sse_events(resp)
    check("turno completato", bool(of_type(events, "done")),
          json.dumps([e["type"] for e in events])[:200])
    reqs = sent()
    check("una richiesta al finto OpenRouter", len(reqs) == n_reqs + 1)
    req = reqs[-1]
    sys_text = system_text(req)
    check("schede dei file di progetto nel system prompt",
          "<file di progetto>" in sys_text and "progetto_01" in sys_text
          and "progetto_02" in sys_text, sys_text[-200:])
    check("nomi reali dei file non nel system prompt",
          "clienti" not in sys_text and "fornitori" not in sys_text)
    utext = user_text(req)
    check("prompt anonimizzato con lo stesso placeholder dei file",
          "Mario Rossi" not in utext and ph_rossi in utext, utext[:160])

    full = client.get(f"/api/chats/{cid}", headers=auth).json()
    check("decodifica locale nel thread",
          "Mario Rossi" in full["messages"][0]["content"])

    # --- regola additiva dopo il primo messaggio ------------------------------------
    r = client.post(f"/api/projects/{pid}/files/{pf2_id}/deanonymize",
                    json={"placeholder": custom_ph}, headers=auth)
    check("deanonimizza rifiutata dopo i messaggi", r.status_code == 409,
          r.text[:120])
    r = client.post(f"/api/projects/{pid}/files/{pf2_id}/anonymize-text",
                    json={"text": "magazzino"}, headers=auth)
    check("modifica additiva ancora ammessa", r.status_code == 200,
          r.text[:120])

    # --- file multimodali: il PDF confermato viaggia anche inline --------------------
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 100), "Contratto firmato da Mario Rossi.")
    pdf_bytes = pdf.tobytes()
    pdf.close()
    with SessionLocal() as s:
        pf3 = project_files.process_upload(pdf_bytes, "contratto.pdf",
                                           pid, engine, s)
        pf3_id = pf3.id
    r = client.post(f"/api/projects/{pid}/files/{pf3_id}/confirm",
                    headers=auth)
    check("nuovo PDF caricato e confermato dopo i messaggi (additivo)",
          r.status_code == 200, r.text[:120])
    d3 = client.get(f"/api/projects/{pid}/files/{pf3_id}",
                    headers=auth).json()
    proj_files = client.get(f"/api/projects/{pid}",
                            headers=auth).json()["files"]
    row3 = next(x for x in proj_files if x["id"] == pf3_id)
    check("scheda briefing esposta (descrittore e riga dell'elenco)",
          (d3.get("briefing") or {}).get("label") == "PDF, 1 pagina"
          and (row3.get("briefing") or {}).get("label") == "PDF, 1 pagina",
          json.dumps([d3.get("briefing"), row3.get("briefing")])[:160])
    conv3 = client.post("/api/chats", json={"project_id": pid},
                        headers=auth).json()
    client.patch(f"/api/chats/{conv3['id']}",
                 json={"model": "test/happy-model"}, headers=auth)
    with client.stream("POST", f"/api/chats/{conv3['id']}/messages",
                       json={"content": "Guarda il contratto."},
                       headers=auth) as resp:
        events = sse_events(resp)
    check("turno col modello multimodale completato",
          bool(of_type(events, "done")),
          json.dumps([e["type"] for e in events])[:200])
    req = sent()[-1]
    msgs = req["messages"]
    synth = msgs[1]
    check("messaggio sintetico coi file di progetto dopo il system",
          msgs[0]["role"] == "system" and synth["role"] == "user"
          and isinstance(synth["content"], list),
          json.dumps([m["role"] for m in msgs]))
    parts = [p for p in synth["content"] if p.get("type") == "file"]
    check("inline solo il PDF (i txt restano schede+sandbox)",
          len(parts) == 1
          and parts[0]["file"]["filename"] == "progetto_03.pdf"
          and all(p.get("type") in ("text", "file")
                  for p in synth["content"]),
          json.dumps([p.get("type") for p in synth["content"]]))
    blob = parts[0]["file"]["file_data"]
    inline_text = ""
    if blob.startswith("data:application/pdf;base64,"):
        with fitz.open(stream=base64.b64decode(blob.split(",", 1)[1]),
                       filetype="pdf") as doc:
            inline_text = "".join(pg.get_text() for pg in doc)
    check("inline viaggia la copia PROTETTA",
          "Mario Rossi" not in inline_text and ph_rossi in inline_text,
          inline_text[:120])
    sys3 = system_text(req)
    check("nota inline sulla sola scheda del PDF",
          sys3.count("(allegato anche nel primo messaggio") == 1
          and "progetto_03.pdf" in sys3, sys3[-260:])

    # --- registro condiviso tra chat + sopravvivenza alla cancellazione -------------
    conv2 = client.post("/api/chats", json={"project_id": pid},
                        headers=auth).json()
    cid2 = conv2["id"]
    ents = client.get(f"/api/chats/{cid2}/entities", headers=auth).json()
    check("registro del progetto visibile dalla seconda chat",
          any(e["placeholder"] == ph_rossi for e in ents["entities"]))
    r = client.delete(f"/api/chats/{cid}", headers=auth)
    check("cancellazione chat di progetto", r.status_code == 200)
    ents = client.get(f"/api/chats/{cid2}/entities", headers=auth).json()
    check("registro sopravvive alla cancellazione della chat",
          any(e["placeholder"] == ph_rossi for e in ents["entities"]))
    with SessionLocal() as s:
        n = s.query(ConversationEntity).filter_by(conv_id=pid).count()
    check("entità con scope = progetto", n >= 2, f"n={n}")

    # --- registro del progetto: le fusioni dalla revisione del file -----------------
    # /api/projects/{id}/entities sono i gemelli degli endpoint della chat con
    # lo scope del progetto: alimentano l'ultimo step del modal di revisione,
    # che è l'UNICO punto in cui un file si conferma. La lista si chiede lì,
    # al momento dell'apertura (dieci file caricati prima la cambiano).
    ids = seed_pair(SessionLocal, pid, "Ferrari", "Giulia Ferrari", 90)
    r = client.get(f"/api/projects/{pid}/entities", headers=auth)
    check("registro del progetto leggibile dal progetto", r.status_code == 200,
          r.text[:80])
    body = r.json()
    check("il registro è quello condiviso coi file",
          any(e["placeholder"] == ph_rossi for e in body["entities"]))
    check("fusione proposta a scope progetto",
          [(s["source_value"], s["target_value"])
           for s in body["suggestions"]] == [("Ferrari", "Giulia Ferrari")],
          json.dumps(body["suggestions"], ensure_ascii=False))
    r = client.post(
        f"/api/projects/{pid}/entities/{ids['Ferrari']}/keep-separate",
        headers=auth)
    check("«mantieni separate» a scope progetto",
          r.status_code == 200 and r.json()["suggestions"] == [], r.text[:80])

    ids2 = seed_pair(SessionLocal, pid, "Conti", "Elena Conti", 92)
    r = client.get(f"/api/projects/{pid}/entities", headers=auth)
    check("la coppia nuova è proposta (lista ricalcolata ogni volta)",
          [(s["source_value"], s["target_value"])
           for s in r.json()["suggestions"]] == [("Conti", "Elena Conti")],
          json.dumps(r.json()["suggestions"], ensure_ascii=False))
    r = client.post(f"/api/projects/{pid}/entities/{ids2['Conti']}/merge",
                    json={"into": ids2["Elena Conti"]}, headers=auth)
    check("fusione a scope progetto accettata",
          r.status_code == 200 and r.json()["suggestions"] == [], r.text[:80])
    check("entità assorbita nel registro",
          any(e["id"] == ids2["Conti"]
              and e["merged_into"] == ids2["Elena Conti"]
              for e in r.json()["entities"]))
    r = client.post(f"/api/projects/{pid}/entities/{ids2['Conti']}/merge",
                    json={"into": "non-esiste"}, headers=auth)
    check("destinazione inesistente rifiutata", r.status_code == 404)

    # la scelta arriva al modello: equivalenza dei segnaposto nel turno dopo
    client.patch(f"/api/chats/{cid2}", json={"model": "test/plain-model"},
                 headers=auth)
    with client.stream("POST", f"/api/chats/{cid2}/messages",
                       json={"content": "Chi è il referente?"},
                       headers=auth) as resp:
        events = sse_events(resp)
    check("turno dopo la fusione completato", bool(of_type(events, "done")),
          json.dumps([e["type"] for e in events])[:200])
    sysm = system_text(sent()[-1])
    check("equivalenza dei segnaposto nel system prompt",
          "[FULLNAME_92] = [FULLNAME_93]" in sysm, sysm[-300:])

    # --- progetto in chiaro -----------------------------------------------------------
    pc = client.post("/api/projects", json={
        "name": "In chiaro", "anonymized": False}, headers=auth).json()
    pcid = pc["id"]
    r = client.post(f"/api/projects/{pcid}/files",
                    files={"file": ("note.txt", FILE1.encode(),
                                    "text/plain")}, headers=auth)
    check("upload in chiaro immediato", r.status_code == 202
          and r.json().get("file", {}).get("confirmed") is True, r.text[:120])
    fid = r.json()["file"]["id"]
    r = client.get(f"/api/projects/{pcid}/files/{fid}/download",
                   headers=auth)
    check("file in chiaro servito com'è", "Mario Rossi" in r.text)
    convc = client.post("/api/chats", json={"project_id": pcid},
                        headers=auth).json()
    check("chat del progetto in chiaro non anonimizzata",
          convc["anonymized"] is False)
    client.patch(f"/api/chats/{convc['id']}",
                 json={"model": "test/plain-model"}, headers=auth)
    with client.stream("POST", f"/api/chats/{convc['id']}/messages",
                       json={"content": "Che file ci sono?"},
                       headers=auth) as resp:
        events = sse_events(resp)
    check("turno in chiaro completato", bool(of_type(events, "done")))
    req = sent()[-1]
    check("scheda del file in chiaro nel system prompt",
          "<file di progetto>" in system_text(req)
          and "progetto_01" in system_text(req))

    # --- cancellazione del progetto ----------------------------------------------------
    r = client.delete(f"/api/projects/{pid}", headers=auth)
    check("cancellazione progetto", r.status_code == 200)
    check("progetto sparito",
          client.get(f"/api/projects/{pid}", headers=auth).status_code == 404)
    check("chat del progetto sparite",
          client.get(f"/api/chats/{cid2}", headers=auth).status_code == 404)
    with SessionLocal() as s:
        n = s.query(ConversationEntity).filter_by(conv_id=pid).count()
    check("registro del progetto rimosso", n == 0, f"n={n}")
    check("directory file rimossa",
          not project_files.project_dir(pid).exists())

    stop_mock()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

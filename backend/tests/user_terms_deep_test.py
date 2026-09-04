"""Termini a due livelli: la battuta di caccia ai casi limite.

Gemello di user_terms_test.py, che copre il percorso felice. Qui si va a
cercare i punti dove la separazione fra lista globale e lista personale può
rompersi davvero:

  1. la MIGRAZIONE di un database che la colonna non ce l'ha;
  2. la CHAT libera (l'altro percorso di redazione: user_terms_test guarda
     solo i file di progetto);
  3. una chat di progetto aperta dall'AMMINISTRATORE dentro il progetto di un
     altro — il caso che decide se «di chi è la lista» è definito bene;
  4. la collisione TARDIVA (l'admin globalizza un termine che un utente aveva
     già come personale);
  5. le categorie escluse contro il tag di un termine;
  6. proprietario sparito / JSON corrotto sotto una redazione;
  7. lo stato condiviso fra due chiamate ad anon_options;
  8. l'isolamento con tre utenti e le scritture incrociate;
  9. la checkbox «anche in futuro» cliccata sul file di un ALTRO;
 10. i file già caricati quando il termine arriva dopo.

I punti 5, 9 e 10 fissano dei LIMITI noti, non dei desideri: l'asserzione dice
cosa fa il programma oggi, così se qualcuno lo cambia il test lo dichiara
invece di tacere.

Uso:  python backend/tests/user_terms_deep_test.py
"""
import io
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DATA_DIR = HERE / "data" / "test_user_terms_deep"
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA_DIR)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import (chat_anonymization as ca, chat_staging, db,     # noqa: E402
                 jobs, project_files, settings_store)
from app.auth import seed_admin                                  # noqa: E402
from app.db import Conversation, Project, User, init_db          # noqa: E402
from app.engine import convert                                   # noqa: E402
from app.routes import auth_routes, projects, settings_routes, users  # noqa: E402

convert._PROFILE_DIR = DATA_DIR / "lo_profile"

PASS = 0
FAIL = 0

# Un tag per livello, così ogni asserzione guarda il PLACEHOLDER e non il
# valore: se controllassimo il testo non sapremmo mai se a coprirlo è stato il
# termine giusto o il modello per conto suo (una «pratica» è anche un
# indirizzo plausibile).
G_TERM, G_TAG = "commessa Belvedere", "GLOBALE"
M_TERM, M_TAG = "dossier Cormorano", "DIMARIO"
A_TERM, A_TAG = "pratica Ostro", "DIADMIN"

TXT = (f"Cliente: Mario Rossi.\n"
       f"Riferimento: {G_TERM}, consegna a settembre.\n"
       f"Nota interna: il {M_TERM} segue la {A_TERM}.\n"
       f"Deposito: Rimessa Turchese, ingresso da via Larga.\n")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def user_by(session, username):
    return session.query(User).filter_by(username=username).one()


def phs(mapping):
    """I prefissi di tag presenti in una mappa {placeholder: valore}."""
    return {p.split("_")[0].lstrip("[") for p in mapping}


def upload(session, project_id, text, name):
    pf = project_files.process_upload(text.encode("utf-8"), name, project_id,
                                      jobs.ENGINES[0], session)
    session.commit()
    return pf


def protected_text(pf):
    path = project_files.protected_path(pf)
    return chat_staging._full_text(path.read_bytes(), project_files.file_ext(pf))


def main():
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)

    # --- 1. adozione di un database senza la colonna -------------------------
    # È il caso di chi arriva da un blockingbear.db nato prima di Alembic: le
    # tabelle ci sono, una colonna no, e non c'è alembic_version. La revisione
    # baseline deve riconoscere quello stato e portarlo a head SENZA perdere i
    # dati (ramo di adozione, backend/alembic/versions/0001_baseline.py).
    print("\n[1] adozione di un database senza users.anon_terms_json")
    with SessionLocal() as s:
        settings_store.set_user_terms(s, user_by(s, "admin"),
                                      [{"text": "termine preesistente",
                                        "tag": "VECCHIO"}])
    with db._engine.connect() as conn:
        conn.exec_driver_sql("ALTER TABLE users DROP COLUMN anon_terms_json")
        # via anche la versione: un database già bollato è a head, e le
        # migrazioni - giustamente - non hanno niente da fare
        conn.exec_driver_sql("DROP TABLE alembic_version")
        conn.commit()
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(users)")}
    check("colonna rimossa (database pre-Alembic simulato)",
          "anon_terms_json" not in cols)
    SessionLocal = init_db()            # <- il ramo di adozione della baseline
    with db._engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(users)")}
        version = conn.exec_driver_sql(
            "SELECT version_num FROM alembic_version").scalar()
    check("l'adozione riaggiunge la colonna", "anon_terms_json" in cols)
    check("e bolla il database a head", bool(version), str(version))
    with SessionLocal() as s:
        admin = user_by(s, "admin")
        check("l'utente è ancora lì e si legge", admin.username == "admin")
        check("chi aggiorna parte senza termini personali (NULL -> [])",
              settings_store.user_terms(admin) == [],
              repr(admin.anon_terms_json))

    # --- utenti e termini di partenza ----------------------------------------
    app = FastAPI()
    for mod in (auth_routes, projects, settings_routes, users):
        app.include_router(mod.router)
    client = TestClient(app)

    def login(username, password):
        r = client.post("/api/auth/login",
                        json={"username": username, "password": password})
        return {"Authorization": f"Bearer {r.json()['token']}"}

    admin_h = login("admin", "admin")
    for name in ("mario", "lucia"):
        client.post("/api/users", json={"username": name,
                                        "password": f"{name}12345",
                                        "role": "standard"}, headers=admin_h)
    mario_h = login("mario", "mario12345")
    lucia_h = login("lucia", "lucia12345")

    client.put("/api/settings/anonymization",
               json={"excluded_tags": [],
                     "custom_terms": [{"text": G_TERM, "tag": G_TAG}]},
               headers=admin_h)
    client.put("/api/settings/my-terms",
               json={"custom_terms": [{"text": M_TERM, "tag": M_TAG}]},
               headers=mario_h)
    client.put("/api/settings/my-terms",
               json={"custom_terms": [{"text": A_TERM, "tag": A_TAG}]},
               headers=admin_h)

    with SessionLocal() as s:
        mario_id = user_by(s, "mario").id
        admin_id = user_by(s, "admin").id

    # --- 2. chat LIBERA -------------------------------------------------------
    # user_terms_test guarda i file di progetto; la chat è l'altra metà del
    # programma e passa dallo stesso anon_options solo perché registry_holder
    # sa tornare anche una Conversation.
    print("\n[2] chat libera: valgono i termini del proprietario della chat")
    with SessionLocal() as s:
        conv = Conversation(owner_id=mario_id, model="test/model", anonymized=1,
                            title="chat di mario")
        s.add(conv)
        s.commit()
        conv_id = conv.id
    model_content, _version, _descs = ca.anonymize_turn(conv_id, TXT, [])
    check("il termine GLOBALE è coperto nel messaggio",
          f"[{G_TAG}_" in model_content, model_content[:0])
    check("il termine PERSONALE di mario è coperto nel messaggio",
          f"[{M_TAG}_" in model_content)
    check("il termine personale dell'ADMIN non entra nella chat di mario",
          f"[{A_TAG}_" not in model_content)
    check("il valore non resta in chiaro", M_TERM not in model_content,
          model_content.replace("\n", " ")[:120])

    # --- 3. chat di PROGETTO aperta dall'amministratore -----------------------
    # L'admin può aprire il progetto di chiunque (_get_project lo lascia
    # passare). Se la lista applicata fosse quella di CHI CHIEDE, lo stesso
    # progetto verrebbe redatto in due modi diversi a seconda di chi lo tocca.
    print("\n[3] chat di progetto: comanda il proprietario del PROGETTO")
    pid = client.post("/api/projects", json={"name": "Pratiche di mario",
                                             "anonymized": True},
                      headers=mario_h).json()["id"]
    with SessionLocal() as s:
        conv = Conversation(owner_id=admin_id, project_id=pid, anonymized=1,
                            model="test/model", title="l'admin guarda")
        s.add(conv)
        s.commit()
        pconv_id = conv.id
        holder = ca.registry_holder(s, s.get(Conversation, pconv_id))
        check("il portatore del registro è il progetto, non la chat",
              isinstance(holder, Project) and holder.id == pid)
    model_content, _v, _d = ca.anonymize_turn(pconv_id, TXT, [])
    check("valgono i termini di MARIO (proprietario del progetto)",
          f"[{M_TAG}_" in model_content)
    check("NON valgono quelli dell'admin, che pure sta scrivendo lui",
          f"[{A_TAG}_" not in model_content, model_content.replace("\n", " ")[:120])

    # --- 4. collisione tardiva -----------------------------------------------
    # Lucia si fissa un termine; l'admin lo mette POI in lista globale con un
    # altro tag. Da quel momento deve valere il tag dell'installazione, o lo
    # stesso valore uscirebbe [MIOTAG_1] nei documenti di lucia e [UFFICIALE_1]
    # in quelli di tutti gli altri.
    print("\n[4] collisione tardiva: il globale si prende il termine")
    client.put("/api/settings/my-terms",
               json={"custom_terms": [{"text": "Rimessa Turchese",
                                       "tag": "MIOTAG"}]}, headers=lucia_h)
    lpid = client.post("/api/projects", json={"name": "Deposito",
                                              "anonymized": True},
                       headers=lucia_h).json()["id"]
    client.put("/api/settings/anonymization",
               json={"excluded_tags": [],
                     "custom_terms": [{"text": G_TERM, "tag": G_TAG},
                                      {"text": "rimessa   turchese",
                                       "tag": "UFFICIALE"}]}, headers=admin_h)
    with SessionLocal() as s:
        terms = ca.anon_options(s, s.get(Project, lpid))["custom_terms"]
    # le chiavi passano da term_key: il testo salvato è quello NORMALIZZATO
    # (l'admin ha scritto tre spazi, in lista ce n'è uno)
    tags = {settings_store.term_key(t["text"]): t["tag"] for t in terms}
    check("il termine compare UNA volta sola",
          sum(1 for t in terms
              if settings_store.term_key(t["text"]) == "rimessa turchese") == 1,
          terms)
    check("col tag dell'amministratore, non con quello di lucia",
          tags.get("rimessa turchese") == "UFFICIALE", tags)
    r = client.get("/api/settings/anonymization", headers=lucia_h).json()
    eff = {settings_store.term_key(t["text"]): t["scope"]
           for t in r["effective_custom_terms"]}
    check("la lista effettiva mostrata a lucia dichiara «globale»",
          eff.get("rimessa turchese") == "global", r["effective_custom_terms"])
    check("LIMITE: la sua voce resta nella sua lista, ma non ha più effetto",
          r["my_custom_terms"] == [{"text": "Rimessa Turchese",
                                    "tag": "MIOTAG"}], r["my_custom_terms"])
    with SessionLocal() as s:
        pf = upload(s, lpid, TXT, "deposito.txt")
        mapping = ca.conversation_mapping(s, lpid)
    check("nel file di lucia esce il tag dell'amministratore",
          "UFFICIALE" in phs(mapping) and "MIOTAG" not in phs(mapping),
          sorted(phs(mapping)))
    # si rimette la lista globale com'era, il resto del test la vuole pulita
    client.put("/api/settings/anonymization",
               json={"excluded_tags": [],
                     "custom_terms": [{"text": G_TERM, "tag": G_TAG}]},
               headers=admin_h)

    # --- 5. categorie escluse contro il tag di un termine --------------------
    # engine.core.analyze aggiunge i termini custom DOPO il filtro `excluded`
    # («sono espliciti»), ma l'esclusione morde una seconda volta più avanti,
    # su ReplacementMapping: il placeholder viene allocato nel registro e la
    # SOSTITUZIONE viene soppressa. Risultato netto: scegliere per un termine
    # un tag che è anche una categoria esclusa lo lascia in chiaro.
    print("\n[5] categorie escluse: spengono anche i termini fissi")
    xpid = client.post("/api/projects",
                       json={"name": "Con esclusioni", "anonymized": True,
                             "anon_options": {"excluded_tags": [M_TAG]}},
                       headers=mario_h).json()["id"]
    with SessionLocal() as s:
        pf = upload(s, xpid, TXT, "escluso.txt")
        mapping = ca.conversation_mapping(s, xpid)
    text = protected_text(pf)
    check("il termine globale resta coperto",
          G_TAG in phs(mapping) and G_TERM not in text, sorted(phs(mapping)))
    check("il termine escluso viene comunque RILEVATO e messo a registro",
          M_TAG in phs(mapping), sorted(phs(mapping)))
    check("TRAPPOLA: ma la sostituzione è soppressa e il valore resta in "
          "chiaro", M_TERM in text, text.replace("\n", " ")[:120])

    # --- 6. proprietario sparito / JSON corrotto ------------------------------
    # Una redazione non deve MAI fermarsi per un parametro illeggibile: i
    # termini globali continuano a valere e il file esce comunque protetto.
    print("\n[6] proprietario introvabile o JSON corrotto")
    with SessionLocal() as s:
        p = s.get(Project, pid)
        good = p.owner_id
        p.owner_id = 999999           # utente che non esiste
        s.flush()
        terms = ca.anon_options(s, p)["custom_terms"]
        check("proprietario introvabile -> restano i soli globali",
              [t["text"] for t in terms] == [G_TERM], terms)
        p.owner_id = good
        user_by(s, "mario").anon_terms_json = "{non è json"
        s.flush()
        terms = ca.anon_options(s, s.get(Project, pid))["custom_terms"]
        check("JSON corrotto -> restano i soli globali, nessuna eccezione",
              [t["text"] for t in terms] == [G_TERM], terms)
        s.rollback()
    with SessionLocal() as s:
        check("il rollback non ha lasciato danni",
              [t["text"] for t in
               ca.anon_options(s, s.get(Project, pid))["custom_terms"]]
              == [G_TERM, M_TERM])

    # --- 7. stato condiviso fra due chiamate ---------------------------------
    # anon_options torna liste e dizionari che finiscono dentro engine.analyze:
    # se fossero gli STESSI oggetti a ogni chiamata, un chiamante distratto
    # (o anon_terms_view, che ci aggiunge `scope`) avvelenerebbe i turni dopo.
    print("\n[7] nessuno stato condiviso fra le chiamate")
    with SessionLocal() as s:
        project = s.get(Project, pid)
        first = ca.anon_options(s, project)["custom_terms"]
        first.append({"text": "iniettato a mano", "tag": "SPORCO"})
        first[0]["tag"] = "SPORCO"
        second = ca.anon_options(s, project)["custom_terms"]
        check("la lista non è condivisa", len(second) == 2, second)
        check("nemmeno i dizionari dentro", second[0]["tag"] == G_TAG, second)
        view = ca.anon_terms_view(s, project)
        after = ca.anon_options(s, project)["custom_terms"]
        check("la vista annotata non sporca la lista del motore",
              all(set(t) == {"text", "tag"} for t in after)
              and all("scope" in t for t in view))

    # --- 8. isolamento e scritture incrociate --------------------------------
    print("\n[8] tre utenti: ognuno vede e scrive solo la sua")
    seen = {}
    for name, headers in (("admin", admin_h), ("mario", mario_h),
                          ("lucia", lucia_h)):
        body = client.get("/api/settings/anonymization", headers=headers).json()
        seen[name] = [t["text"] for t in body["my_custom_terms"]]
        check(f"{name}: la lista globale è la stessa per tutti",
              [t["text"] for t in body["custom_terms"]] == [G_TERM],
              body["custom_terms"])
    check("nessuno vede i termini degli altri",
          seen == {"admin": [A_TERM], "mario": [M_TERM],
                   "lucia": ["Rimessa Turchese"]}, seen)
    client.put("/api/settings/my-terms",
               json={"custom_terms": [{"text": M_TERM, "tag": M_TAG},
                                      {"text": "cantiere Aurora",
                                       "tag": "CANTIERE"}]}, headers=mario_h)
    after = {}
    for name, headers in (("admin", admin_h), ("lucia", lucia_h)):
        body = client.get("/api/settings/anonymization", headers=headers).json()
        after[name] = ([t["text"] for t in body["my_custom_terms"]],
                       [t["text"] for t in body["custom_terms"]])
    check("mario che scrive la sua non tocca gli altri né la globale",
          after == {"admin": ([A_TERM], [G_TERM]),
                    "lucia": (["Rimessa Turchese"], [G_TERM])}, after)
    r = client.put("/api/settings/my-terms", json={"custom_terms": []},
                   headers=lucia_h)
    check("svuotare la propria lista è lecito (la globale resta)",
          r.status_code == 200 and r.json()["my_custom_terms"] == []
          and [t["text"] for t in r.json()["custom_terms"]] == [G_TERM])

    # --- 9. la checkbox «anche in futuro» sul file di un ALTRO ---------------
    # L'admin apre il progetto di mario e anonimizza a mano un valore
    # spuntando «anche in futuro». La checkbox parla dei SUOI documenti, quindi
    # il termine va nella lista dell'admin; il valore resta comunque coperto
    # in questo progetto perché ci finisce dentro il REGISTRO, che è del
    # progetto e non di chi ha cliccato.
    print("\n[9] checkbox «anche in futuro» dentro il progetto di un altro")
    with SessionLocal() as s:
        pf = upload(s, pid, TXT, "selezione.txt")
        fid = pf.id
    r = client.post(f"/api/projects/{pid}/files/{fid}/anonymize-text",
                    json={"text": "Rimessa Turchese", "save_term": True,
                          "term_tag": "DEPOSITO"}, headers=admin_h)
    check("l'admin anonimizza a mano nel progetto di mario",
          r.status_code == 200, r.text[:150])
    check("il termine è salvato (nella lista di chi ha cliccato)",
          r.json().get("saved_term") is True, r.json().get("saved_term"))
    mine = client.get("/api/settings/anonymization",
                      headers=admin_h).json()["my_custom_terms"]
    theirs = client.get("/api/settings/anonymization",
                        headers=mario_h).json()["my_custom_terms"]
    check("finisce fra i termini dell'ADMIN",
          any(t["text"] == "Rimessa Turchese" for t in mine), mine)
    check("non finisce in quelli di mario, che non ha chiesto niente",
          not any(t["text"] == "Rimessa Turchese" for t in theirs), theirs)
    check("né nella lista globale (non serve essere admin per salvarlo)",
          [t["text"] for t in client.get("/api/settings/anonymization",
                                         headers=admin_h).json()["custom_terms"]]
          == [G_TERM])
    with SessionLocal() as s:
        mapping = ca.conversation_mapping(s, pid)
    check("ma il valore è coperto lo stesso: è nel REGISTRO del progetto",
          "DEPOSITO" in phs(mapping), sorted(phs(mapping)))
    with SessionLocal() as s:
        pf2 = upload(s, pid, "Consegna alla Rimessa Turchese entro lunedi.\n",
                     "dopo.txt")
        check("e vale anche per i file caricati dopo, in questo progetto",
              "Rimessa Turchese" not in protected_text(pf2),
              protected_text(pf2)[:100])

    # --- 10. un termine che arriva DOPO il caricamento ------------------------
    # Limite strutturale, non introdotto dai due livelli (valeva identico per
    # la sola lista globale): la ri-redazione riscrive il file con il REGISTRO,
    # non rifà il rilevamento, quindi un termine mai allocato non ha nessun
    # placeholder da applicare.
    print("\n[10] termine aggiunto dopo che il file era già caricato")
    npid = client.post("/api/projects", json={"name": "Tardivo",
                                              "anonymized": True},
                       headers=mario_h).json()["id"]
    with SessionLocal() as s:
        pf = upload(s, npid, "Il fascicolo Zenith passa al vaglio.\n",
                    "tardivo.txt")
        nfid = pf.id
        check("prima: il valore non è coperto (nessun termine lo copre)",
              "fascicolo Zenith" in protected_text(pf))
    client.put("/api/settings/my-terms",
               json={"custom_terms": [{"text": M_TERM, "tag": M_TAG},
                                      {"text": "fascicolo Zenith",
                                       "tag": "FASCICOLO"}]}, headers=mario_h)
    with SessionLocal() as s:
        project, pf = s.get(Project, npid), s.get(db.ProjectFile, nfid)
        check("LIMITE: il file non risulta «da riallineare»",
              project_files.stale_leaks(s, project, pf) == [])
        with ca.conversation_lock(npid):
            project_files.rebuild_file(s, project, pf)
            s.commit()
        check("LIMITE: la ri-redazione non lo copre (riscrive col registro, "
              "non rifà il rilevamento)",
              "fascicolo Zenith" in protected_text(pf))
    with SessionLocal() as s:
        pf3 = upload(s, npid, "Il fascicolo Zenith passa al vaglio.\n",
                     "ricaricato.txt")
        check("il rimedio è ricaricare il file: lì il termine vale",
              "fascicolo Zenith" not in protected_text(pf3),
              protected_text(pf3)[:80])

    print(f"\nRISULTATO: {PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

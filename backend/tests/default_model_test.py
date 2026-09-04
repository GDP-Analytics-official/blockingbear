"""Modello predefinito della chat: la regola madre (admin) e quella personale
di ogni utente, e la risoluzione alla creazione di una conversazione.

Due parti: la logica pura di openrouter/model_rules.py su un catalogo
sintetico (ordinamenti, filtri, forme non valide) e il giro completo dalle
route sul finto OpenRouter (chi può salvare cosa, cosa eredita chi).

Uso:
    python backend/tests/default_model_test.py
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import chat_mock_openrouter as mock                              # noqa: E402

# PRIMA di ogni import di app.*: dati isolati e OpenRouter finto
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_default_model")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_OPENROUTER_BASE_URL"] = mock.BASE_URL

from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import settings_store                                   # noqa: E402
from app.auth import hash_password, seed_admin                   # noqa: E402
from app.db import User, init_db                                 # noqa: E402
from app.openrouter import catalog, model_rules                  # noqa: E402
from app.openrouter import client as or_client                   # noqa: E402
from app.routes import auth_routes, chat_routes, settings_routes  # noqa: E402

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


# Catalogo sintetico: punteggi e prezzi diversi, più le trappole che il
# resolver deve saltare (variante di routing, niente tools, in scadenza, senza
# visione, senza provider ZDR, non valutato).
def _m(mid, prompt, completion, *, bench=None, tools=True, variant=None,
       expires=None, image=True, zdr=("Prov",)):
    return {"id": mid, "name": mid, "tools": tools, "variant_of": variant,
            "expiration_date": expires,
            "input_modalities": ["text", "image"] if image else ["text"],
            "zdr_providers": list(zdr),
            "pricing": {"prompt": str(prompt), "completion": str(completion)},
            "benchmarks": bench and dict(zip(("intelligence", "coding",
                                              "agentic"), bench))}


# prezzi in $/token; i punteggi (intelligenza, codice, strumenti) su 100
CAT = [
    _m("acme/top",       0.000030, 0.000030, bench=(65, 80, 60)),
    _m("acme/quasi-top", 0.000004, 0.000004, bench=(60, 78, 58)),
    _m("acme/medio",     0.000002, 0.000002, bench=(50, 40, 45)),
    _m("acme/base",      0.000001, 0.000001, bench=(40, 30, 30)),
    _m("altra/coder",    0.000001, 0.000001, bench=(45, 79, 20), image=False),
    _m("altra/no-zdr",   0.0000005, 0.0000005, bench=(64, 70, 59), zdr=()),
    _m("acme/senza-tools", 0.0000001, 0.0000001, bench=(99, 99, 99), tools=False),
    _m("acme/top:nitro", 0.001, 0.002, bench=(65, 80, 60), variant="acme/top"),
    _m("acme/in-scadenza", 0.1, 0.2, bench=(99, 99, 99), expires="2026-12-31"),
    _m("acme/non-valutato", 0.000001, 0.000001),
    _m("acme/senza-strumenti-index", 0.000001, 0.000001, bench=(70, 70, None)),
]


def _g(use, budget, images=False):
    return {"kind": "guided", "use": use, "budget": budget, "images": images}


def unit():
    print("\n== regole (logica pura) ==")

    # --- validazione ------------------------------------------------------
    check("clean_rule: None resta None", model_rules.clean_rule(None) is None)
    check("clean_rule: kind 'default' = nessuna scelta",
          model_rules.clean_rule({"kind": "default"}) is None)
    check("clean_rule: fisso normalizzato",
          model_rules.clean_rule({"kind": "fixed", "model": " acme/nuovo "})
          == {"kind": "fixed", "model": "acme/nuovo"})
    check("clean_rule: guidata normalizzata (solo le tre risposte)",
          model_rules.clean_rule({"kind": "guided", "use": "Documents",
                                  "budget": "BEST", "images": True,
                                  "extra": 1})
          == {"kind": "guided", "use": "documents", "budget": "best",
              "images": True})
    for bad, why in (({"kind": "boh"}, "criterio inventato"),
                     ({"kind": "latest", "author": "acme"}, "criterio ritirato"),
                     ({"kind": "fixed"}, "fisso senza modello"),
                     ({"kind": "guided", "use": "writing", "budget": "best"},
                      "guidata senza la risposta sulle immagini"),
                     ({"kind": "guided", "use": "writing", "budget": "best",
                       "images": "yes"}, "guidata con immagini non booleano"),
                     ({"kind": "guided", "use": "poesia", "budget": "best",
                       "images": False}, "guidata con uso inventato"),
                     ("stringa", "non è un oggetto")):
        try:
            model_rules.clean_rule(bad)
            ok = False
        except ValueError:
            ok = True
        check(f"clean_rule: rifiuta {why}", ok)

    # --- round trip -------------------------------------------------------
    rule = _g("mixed", "balanced", True)
    check("dump/load: giro completo",
          model_rules.load(model_rules.dump(rule)) == rule)
    check("dump: nessuna scelta -> stringa vuota", model_rules.dump(None) == "")
    check("load: JSON corrotto -> nessuna scelta (non esplode)",
          model_rules.load("{non json") is None)
    check("load: regola non più valida -> nessuna scelta",
          model_rules.load('{"kind": "latest", "author": "acme"}') is None)

    # --- risoluzione: fisso -----------------------------------------------
    def res(rule, **kw):
        return model_rules.resolve(rule, CAT, **kw)

    check("resolve: nessuna regola -> nessun modello", res(None) == "")
    check("resolve: fisso passa com'è",
          res({"kind": "fixed", "model": "acme/medio"}) == "acme/medio")
    check("resolve: fisso ritirato dal catalogo -> nessun modello",
          res({"kind": "fixed", "model": "acme/sparito"}) == "")
    check("resolve: fisso non guarda la deroga ZDR (scelta esplicita)",
          res({"kind": "fixed", "model": "altra/no-zdr"}, allow_non_zdr=False)
          == "altra/no-zdr")

    # --- risoluzione: guidata ---------------------------------------------
    # pool per «scrivere», senza immagini: top 65, no-zdr 64, quasi-top 60,
    # medio 50, coder 45, base 40 (saltati: senza tools, :nitro, in scadenza,
    # non valutato; senza-strumenti-index ha l'indice intelligenza -> 70!)
    check("guided best: il punteggio più alto",
          res(_g("writing", "best")) == "acme/senza-strumenti-index",
          "un indice mancante non esclude dagli altri usi")
    check("guided best su «strumenti»: chi non ha quell'indice è fuori",
          res(_g("documents", "best")) == "acme/top")
    check("guided best su «un po' di tutto»: servono tutti e tre gli indici",
          res(_g("mixed", "best")) == "acme/top",
          "senza-strumenti-index (70,70,None) non entra in gara")
    check("guided balanced: fra chi sta al 90% del migliore, il più economico",
          res(_g("documents", "balanced")) == "altra/no-zdr",
          "soglia 54: top, quasi-top, no-zdr; il più economico è no-zdr")
    check("guided balanced con deroga ZDR negata: no-zdr esce, resta quasi-top",
          res(_g("documents", "balanced"), allow_non_zdr=False)
          == "acme/quasi-top")
    check("guided balanced su codice: al 90% di 80 restano top, quasi-top, coder",
          res(_g("coding", "balanced")) == "altra/coder",
          "il più economico dei tre")
    check("guided economy: stessa regola all'80%, e scende di prezzo",
          res(_g("coding", "economy")) == "altra/no-zdr",
          "soglia 64: entrano anche no-zdr (70) e senza-strumenti-index (70);"
          " il più economico è no-zdr")
    check("guided images: chi non legge immagini è fuori",
          res(_g("coding", "best", images=True)) == "acme/top"
          and res(_g("coding", "best", images=False)) == "acme/top",
          "top 80 batte coder 79 in entrambi i casi")
    check("guided images + economy: chi non legge immagini resta fuori",
          res(_g("coding", "economy", images=True)) == "altra/no-zdr"
          and res(_g("coding", "economy", images=True), allow_non_zdr=False)
          == "acme/senza-strumenti-index",
          "senza coder e no-zdr: fra top, quasi-top, senza-strumenti-index (70,"
          " 2e-6) vince quest'ultimo")
    check("guided: le trappole non vincono mai",
          all(res(_g(u, b)) not in ("acme/senza-tools", "acme/top:nitro",
                                    "acme/in-scadenza", "acme/non-valutato")
              for u in model_rules.USES for b in model_rules.BUDGETS))
    check("guided: catalogo senza valutazioni -> nessun modello",
          model_rules.resolve(_g("mixed", "best"),
                              [_m("x/y", 1e-6, 1e-6)]) == "")
    check("guided: pool di un solo modello -> quello, per ogni budget",
          all(model_rules.resolve(_g("writing", b),
                                  [_m("x/solo", 1e-6, 1e-6, bench=(1, 1, 1))])
              == "x/solo" for b in model_rules.BUDGETS))

    # --- explain: quello che la UI mostra ---------------------------------
    ex = model_rules.explain(_g("documents", "best"), CAT)
    check("explain guidata: modello, pool e punteggi",
          ex == {"model": "acme/top",
                 "detail": {"pool": 6, "score": 60.0, "best_score": 60.0}},
          str(ex))
    ex = model_rules.explain(_g("mixed", "best"),
                             [_m("x/y", 1e-6, 1e-6)])
    check("explain guidata senza candidati: pool 0",
          ex == {"model": "", "detail": {"pool": 0}}, str(ex))
    check("explain fisso: nessun dettaglio",
          model_rules.explain({"kind": "fixed", "model": "acme/medio"}, CAT)
          == {"model": "acme/medio", "detail": None})


def routes():
    print("\n== route e creazione chat ==")
    db_file = Path(os.environ["BLOCKINGBEAR_DATA_DIR"]) / "blockingbear.db"
    db_file.unlink(missing_ok=True)
    catalog.invalidate()
    stop_mock = mock.serve()

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(chat_routes.router)
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)
        s.add(User(username="mario", password_hash=hash_password("mario"),
                   role="standard"))
        s.commit()

    client = TestClient(app)

    def login(u, p):
        r = client.post("/api/auth/login", json={"username": u, "password": p})
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def new_chat(auth):
        return client.post("/api/chats", json={"model": ""}, headers=auth)

    try:
        admin = login("admin", "admin")
        mario = login("mario", "mario")

        # --- di partenza: nessun default, niente cambia --------------------
        r = client.get("/api/settings/default-model", headers=mario)
        check("stato iniziale: nessuna regola",
              r.status_code == 200 and r.json() == {"rule": None,
                                                    "personal": None,
                                                    "resolved": "",
                                                    "allow_non_zdr": False},
              str(r.json()))
        r = new_chat(mario)
        check("senza default la chat nasce senza modello",
              r.status_code == 200 and r.json()["model"] == "",
              repr(r.json().get("model")))

        # --- la madre la fissa solo l'admin --------------------------------
        r = client.put("/api/settings/default-model", headers=mario,
                       json={"rule": {"kind": "fixed",
                                      "model": "test/plain-model"}})
        check("la regola madre è vietata all'utente", r.status_code == 403)

        r = client.put("/api/settings/default-model", headers=admin,
                       json={"rule": {"kind": "fixed",
                                      "model": "test/plain-model"}})
        check("l'admin fissa la regola madre",
              r.status_code == 200
              and r.json()["rule"]["model"] == "test/plain-model", str(r.json()))

        r = new_chat(mario)
        check("la chat dell'utente eredita la madre",
              r.json()["model"] == "test/plain-model", str(r.json()["model"]))
        r = new_chat(admin)
        check("anche l'admin eredita la madre",
              r.json()["model"] == "test/plain-model", str(r.json()["model"]))

        # --- la scelta personale vince -------------------------------------
        guided = {"kind": "guided", "use": "documents", "budget": "best",
                  "images": True}
        r = client.put("/api/settings/my-default-model", headers=mario,
                       json={"rule": guided})
        check("l'utente salva la sua regola (guidata)",
              r.status_code == 200 and r.json()["personal"] == guided,
              str(r.json()))
        r = new_chat(mario)
        check("la scelta personale vince sulla madre",
              r.json()["model"] == "test/happy-model",
              "l'unico con strumenti, visione e punteggi (presi dal pubblico)")
        r = new_chat(admin)
        check("la scelta di un utente non tocca gli altri",
              r.json()["model"] == "test/plain-model")

        r = client.get("/api/settings/default-model", headers=mario)
        check("il GET mostra entrambi i livelli",
              r.json()["rule"]["model"] == "test/plain-model"
              and r.json()["personal"]["kind"] == "guided", str(r.json()))
        check("il GET dice a quale modello si risolve adesso",
              r.json()["resolved"] == "test/happy-model",
              r.json()["resolved"])
        r = client.get("/api/settings/default-model", headers=admin)
        check("il risolto è quello di CHI chiede",
              r.json()["resolved"] == "test/plain-model",
              "l'admin non ha ancora una regola personale")

        # --- il modello esplicito del browser ha sempre la precedenza ------
        r = client.post("/api/chats", json={"model": "test/nozdr-model"},
                        headers=mario)
        check("un modello scelto a mano non viene sovrascritto",
              r.json()["model"] == "test/nozdr-model")

        # --- tornare su «default» ------------------------------------------
        r = client.put("/api/settings/my-default-model", headers=mario,
                       json={"rule": None})
        check("l'utente torna su «default»",
              r.status_code == 200 and r.json()["personal"] is None)
        r = new_chat(mario)
        check("su «default» si torna a seguire la madre",
              r.json()["model"] == "test/plain-model")

        # --- l'admin ha anche lui la sua regola personale -------------------
        r = client.put("/api/settings/my-default-model", headers=admin,
                       json={"rule": {"kind": "guided", "use": "coding",
                                      "budget": "best", "images": False}})
        check("l'admin salva la propria regola personale", r.status_code == 200)
        r = new_chat(admin)
        check("la personale dell'admin vince sulla sua stessa madre",
              r.json()["model"] == "test/happy-model", str(r.json()["model"]))
        check("guidata: con la deroga ZDR spenta web-model (senza provider ZDR)"
              " non viene scelto anche se è il più bravo a programmare",
              r.json()["model"] != "test/web-model")
        with SessionLocal() as s:
            settings_store.set_values(s, {"chat_allow_non_zdr": 1})
            s.commit()
        r = new_chat(admin)
        check("guidata: con la deroga ZDR accesa vince web-model",
              r.json()["model"] == "test/web-model", str(r.json()["model"]))
        with SessionLocal() as s:
            settings_store.set_values(s, {"chat_allow_non_zdr": 0})
            s.commit()

        # --- l'anteprima della regola guidata (senza salvare) --------------
        r = client.post("/api/settings/default-model/resolve", headers=mario,
                        json={"rule": {"kind": "guided", "use": "coding",
                                       "budget": "best", "images": True}})
        check("resolve: anteprima con dettaglio",
              r.status_code == 200 and r.json()["model"] == "test/happy-model"
              and r.json()["detail"] == {"pool": 1, "score": 70.0,
                                         "best_score": 70.0}, str(r.json()))
        r = client.post("/api/settings/default-model/resolve", headers=mario,
                        json={"rule": {"kind": "guided", "use": "coding"}})
        check("resolve: regola incompleta rifiutata con 422",
              r.status_code == 422, str(r.status_code))
        r = client.get("/api/settings/default-model", headers=admin)
        check("l'anteprima non ha salvato nulla",
              r.json()["personal"]["use"] == "coding"
              and r.json()["personal"]["images"] is False)

        # --- regole non valide e regole che non si risolvono ---------------
        r = client.put("/api/settings/my-default-model", headers=mario,
                       json={"rule": {"kind": "fixed", "model": ""}})
        check("regola incompleta rifiutata con 422", r.status_code == 422)

        r = client.put("/api/settings/my-default-model", headers=mario,
                       json={"rule": {"kind": "fixed",
                                      "model": "test/modello-ritirato"}})
        check("un modello sconosciuto si salva (il catalogo cambia)",
              r.status_code == 200)
        r = new_chat(mario)
        check("una regola che non si risolve non blocca la chat",
              r.status_code == 200 and r.json()["model"] == "",
              "nessun modello, lo sceglie l'utente")

        # --- la madre si può togliere -------------------------------------
        r = client.put("/api/settings/default-model", headers=admin,
                       json={"rule": None})
        check("l'admin toglie la regola madre",
              r.status_code == 200 and r.json()["rule"] is None)
    finally:
        stop_mock()


if __name__ == "__main__":
    unit()
    routes()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)

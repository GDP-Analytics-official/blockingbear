"""GET /api/tags (app/main.py) senza e con il checkpoint del modello PII.

Senza config.json nella cartella del modello — il caso della cartella vuota
creata da Docker quando si salta il download — la risposta deve essere un
503 con codice `pii_model_missing` e il percorso nei params, non un 500: il
wizard di installazione ci si appoggia per spiegare il rimedio e per non
lasciar concludere la configurazione. Con il checkpoint presente la risposta
elenca le categorie (model, regex_only, all, groups). Niente Docker.

Uso:
    python backend/tests/tags_endpoint_test.py
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

EMPTY = HERE / "data" / "test_tags_empty_model"
EMPTY.mkdir(parents=True, exist_ok=True)
REAL = HERE / "models" / "rizzo-pii-0.3B-v1.5.0"

# prima di importare l'app: config.py legge l'ambiente all'import
os.environ["PII_MODEL_DIR"] = str(EMPTY)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_tags")
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_SANDBOX_ENGINE"] = "off"

from fastapi.testclient import TestClient                        # noqa: E402

from app import jobs, main                                       # noqa: E402
from app.engine.core import PiiEngine                            # noqa: E402

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


def main_():
    # TestClient senza `with`: nessun evento di startup, quindi niente
    # worker, niente warm-up e niente sandbox
    client = TestClient(main.app)

    r = client.get("/api/tags")
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
    check("cartella modello vuota: 503", r.status_code == 503, str(r.status_code))
    check("codice pii_model_missing", body and body.get("code") == "pii_model_missing", str(body))
    check("params.dir = cartella del modello",
          body and body.get("params", {}).get("dir") == str(EMPTY), str(body and body.get("params")))
    check("detail leggibile con il percorso",
          body and str(EMPTY) in body.get("detail", ""), str(body and body.get("detail"))[:120])

    if not (REAL / "config.json").is_file():
        print(f"  SKIP  checkpoint reale assente in {REAL}: salto il caso positivo")
        return

    # caso positivo: stesso endpoint, cartella con il checkpoint. MODEL_DIR e'
    # un modulo-globale letto dall'endpoint; l'engine tiene il suo model_dir
    saved_dir, saved_engine = main.MODEL_DIR, jobs.ENGINES[0]
    main.MODEL_DIR = REAL
    jobs.ENGINES[0] = PiiEngine(REAL)
    try:
        r = client.get("/api/tags")
        body = r.json()
        check("checkpoint presente: 200", r.status_code == 200, str(r.status_code))
        check("chiavi model/regex_only/all/groups",
              set(body) == {"model", "regex_only", "all", "groups"}, str(sorted(body)))
        check("categorie non vuote e ordinate",
              body["all"] and body["all"] == sorted(body["all"]) and "FULLNAME" in body["all"],
              f"{len(body['all'])} tag")
        check("gruppo cyber contenuto in all",
              set(body["groups"].get("cyber", [])) <= set(body["all"]), "")
    finally:
        main.MODEL_DIR, jobs.ENGINES[0] = saved_dir, saved_engine


if __name__ == "__main__":
    main_()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

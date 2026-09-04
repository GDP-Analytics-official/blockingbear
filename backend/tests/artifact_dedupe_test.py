"""Artifact della sandbox senza doppioni e senza falsi avvisi (chat_routes.
_register_artifacts).

Due invarianti, entrambe osservabili in una sola chat:
  1. UN file prodotto più volte nello stesso turno = UNA riga Attachment. Lo
     stesso nome può comparire in `new_files` di due esecuzioni (la prima
     fallita DOPO il savefig, col modello che riesegue il codice) e ogni
     evento che creasse una riga nuova mostrerebbe in chat due png e due svg
     identici;
  2. in una chat anonimizzata ma SENZA entità nel registro un artifact è
     "raw", non "protected": senza entità non esiste alcun TAG, e l'avviso
     "contiene ancora i segnaposto" sarebbe falso.

Qui si esercita _register_artifacts direttamente, con staging finti: la
memoria per-turno `registered` riusa la riga (stesso id, byte nuovi, report
ricalcolato), il registro vuoto produce artifact "raw" senza report, e i
percorsi sani (formati non ripristinabili con mapping pieno, chiamate senza
`registered`) restano invariati.

Uso:
    python backend/tests/artifact_dedupe_test.py
"""
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

# PRIMA di ogni import di app.*: dati isolati dal DB di esercizio
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_artifact_dedupe")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

from app import db                                                 # noqa: E402
from app.chat_anonymization import MediaPool                       # noqa: E402
from app.db import Attachment, init_db                             # noqa: E402
from app.routes import chat_routes                                 # noqa: E402

WORK = HERE / "data" / "test_artifact_dedupe" / "work"

PASS = 0
FAIL = 0

MAPPING = {"[FULLNAME_1]": "Mario Rossi", "[ORG_1]": "ACME S.p.A."}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def _staging(named_bytes):
    """Una finta cartella outputs della sandbox: {nome: byte} -> {rel: host}."""
    staging = WORK / "staging" / os.urandom(4).hex()
    (staging / "outputs").mkdir(parents=True)
    files = {}
    for name, data in named_bytes.items():
        dst = staging / "outputs" / name
        dst.write_bytes(data)
        files[f"outputs/{name}"] = str(dst)
    return files


def _event(call_id, files, outcome="ok"):
    return {"type": "tool_result", "id": call_id,
            "result": {"stdout": "", "stderr": "", "outcome": outcome,
                       "new_files": sorted(files), "elapsed_ms": 5,
                       "files": files}}


def _rows(conv):
    with db.SessionLocal() as s:
        return s.query(Attachment).filter_by(conv_id=conv).all()


def test_dedupe_stesso_turno():
    """Il caso che conta: prima esecuzione fallita dopo il savefig, seconda
    che riscrive gli stessi file. Una riga sola per file, byte della seconda."""
    print("\n--- dedupe nello stesso turno (retry del modello) ---")
    conv = "convdedupe0000000000000000000001"
    registered, charts, sources = {}, MediaPool(), {}

    files1 = _staging({"grafico.txt": b"v1 [FULLNAME_1]\n",
                       "dati.csv": b"a;b\n1;[ORG_1]\n"})
    out1 = chat_routes._register_artifacts(
        conv, _event("call_1", files1, outcome="error"), anonymized=True,
        mapping=MAPPING, media=charts, sources=sources, registered=registered)
    ids1 = {a["filename"]: a["id"] for a in out1["attachments"]}
    check("prima esecuzione: 2 artifact", len(out1["attachments"]) == 2,
          sorted(ids1))

    files2 = _staging({"grafico.txt": b"v2 [FULLNAME_1] riveduto\n",
                       "dati.csv": b"a;b\n2;[ORG_1]\n"})
    out2 = chat_routes._register_artifacts(
        conv, _event("call_2", files2), anonymized=True,
        mapping=MAPPING, media=charts, sources=sources, registered=registered)
    ids2 = {a["filename"]: a["id"] for a in out2["attachments"]}

    rows = _rows(conv)
    check("dopo il retry: SEMPRE 2 righe, non 4", len(rows) == 2,
          [r.filename for r in rows])
    check("stessi id riusati (la UI aggancia per id)", ids1 == ids2,
          f"{ids1} vs {ids2}")
    by_name = {r.filename: r for r in rows}
    txt = by_name["grafico.txt"]
    content = Path(txt.original_path).read_text("utf-8-sig")
    check("i byte sul disco sono della SECONDA versione",
          content.startswith("v2 "), content.strip())
    check("il TAG è ripristinato anche dopo il riuso della riga",
          "Mario Rossi" in content and "[FULLNAME_1]" not in content,
          content.strip())
    check("stato restored dopo il ri-ripristino",
          txt.anonymization_status == "restored", txt.anonymization_status)
    rep = json.loads(txt.anonymization_report_json or "{}")
    check("report ricalcolato sulla nuova versione",
          rep.get("restored") == 1 and not rep.get("remaining"), rep)
    check("size in DB = size su disco",
          all(r.size == Path(r.original_path).stat().st_size for r in rows))


def test_registro_vuoto():
    """Chat anonimizzata SENZA entità: nessun TAG è mai esistito, quindi
    niente stato protected e niente avviso sui segnaposto."""
    print("\n--- registro vuoto: niente falso avviso ---")
    conv = "convdedupe0000000000000000000002"
    files = _staging({"disegno.svg": b"<svg><text>casa</text></svg>",
                      "note.txt": b"nessun dato personale\n"})
    out = chat_routes._register_artifacts(
        conv, _event("call_1", files), anonymized=True, mapping={},
        media=MediaPool(), sources={}, registered={})
    rows = _rows(conv)
    check("2 artifact registrati", len(rows) == 2)
    check("stato raw (nessuna etichetta ambra in UI)",
          all(r.anonymization_status == "raw" for r in rows),
          [r.anonymization_status for r in rows])
    check("nessun report sui segnaposto",
          all(not r.anonymization_report_json for r in rows),
          [r.anonymization_report_json for r in rows])
    check("byte intatti (niente ripristino da fare)",
          Path([r for r in rows if r.filename == "note.txt"][0].original_path)
          .read_bytes() == b"nessun dato personale\n")
    check("descriptor senza report", all(
        a["anonymization_report"] is None and
        a["anonymization_status"] == "raw" for a in out["attachments"]))


def test_non_ripristinabile_resta_avvisato():
    """Con un registro PIENO il formato binario non ripristinabile deve
    continuare a dichiarare i segnaposto: quel comportamento era giusto."""
    print("\n--- mapping pieno + formato non ripristinabile ---")
    conv = "convdedupe0000000000000000000003"
    files = _staging({"blob.bin": b"\x00\x01dati opachi\x02"})
    chat_routes._register_artifacts(
        conv, _event("call_1", files), anonymized=True, mapping=MAPPING,
        media=MediaPool(), sources={}, registered={})
    (row,) = _rows(conv)
    check("stato protected", row.anonymization_status == "protected",
          row.anonymization_status)
    rep = json.loads(row.anonymization_report_json or "{}")
    check("avviso sui segnaposto presente",
          "segnaposto" in (rep.get("notice") or ""), rep)


def test_percorsi_diversi_niente_fusione():
    """File DIVERSI nello stesso turno restano righe diverse: il dedupe è
    per percorso relativo, non per nome o contenuto."""
    print("\n--- percorsi diversi: nessuna fusione indebita ---")
    conv = "convdedupe0000000000000000000004"
    registered = {}
    files1 = _staging({"report_v1.txt": b"uno\n"})
    files2 = _staging({"report_v2.txt": b"due\n"})
    chat_routes._register_artifacts(
        conv, _event("call_1", files1), registered=registered)
    chat_routes._register_artifacts(
        conv, _event("call_2", files2), registered=registered)
    rows = _rows(conv)
    check("2 righe per 2 percorsi diversi", len(rows) == 2,
          sorted(r.filename for r in rows))


def test_legacy_senza_registered():
    """Chiamata senza `registered` (test e chiamanti storici): comportamento
    di prima, una riga per evento."""
    print("\n--- senza registered: comportamento invariato ---")
    conv = "convdedupe0000000000000000000005"
    files1 = _staging({"doc.txt": b"uno\n"})
    files2 = _staging({"doc.txt": b"due\n"})
    chat_routes._register_artifacts(conv, _event("call_1", files1))
    chat_routes._register_artifacts(conv, _event("call_2", files2))
    rows = _rows(conv)
    check("2 righe senza memoria per-turno", len(rows) == 2)


def main():
    if WORK.parent.is_dir():
        shutil.rmtree(WORK.parent, ignore_errors=True)
    WORK.mkdir(parents=True)
    init_db()

    test_dedupe_stesso_turno()
    test_registro_vuoto()
    test_non_ripristinabile_resta_avvisato()
    test_percorsi_diversi_niente_fusione()
    test_legacy_senza_registered()

    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

"""Test del percorso OCR della chat: allegato IMMAGINE e PDF scansionato in
una conversazione anonimizzata (nessuna rete, nessun Docker; l'OCR gira
davvero, il detector PII è finto e deterministico).

Prerequisito: stack OCR installato (rapidocr+onnxruntime+pillow). Il documento
è l'asset versionato assets/scansione_con_immagini.pdf: per l'immagine si
ritaglia la sua ultima pagina, il foglio firme, che porta due firme disegnate e
una riga illeggibile oltre ai nomi nei pixel."""

import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
_tmp = tempfile.TemporaryDirectory(prefix="blockingbear-chat-ocr-",
                                   ignore_cleanup_errors=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = _tmp.name
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
sys.path.insert(0, str(HERE))

from app import chat_anonymization as ca            # noqa: E402
from app.db import Attachment, Conversation, init_db  # noqa: E402
from app.engine import image_ocr                    # noqa: E402

import fitz                                          # noqa: E402

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class FakeEngine:
    """Detector deterministico sui valori passati (come chat_anonymization_test)."""

    def __init__(self, entries):
        self.entries = entries

    def analyze(self, text, excluded=None, **_kwargs):
        skip = set(excluded or ())
        entities = []
        for label, value in self.entries:
            if label in skip:
                continue
            for match in re.finditer(re.escape(value), text, re.IGNORECASE):
                entities.append({
                    "label": label, "start": match.start(), "end": match.end(),
                    "value": text[match.start():match.end()], "source": "test",
                    "validated": True, "ph": "[LOCAL_1]",
                })
        entities.sort(key=lambda e: e["start"])
        return {"entities": entities, "anonymized_text": text, "mapping": {},
                "n_entities": len(entities), "n_unique": len(entities),
                "by_label": {}}


def main():
    if not image_ocr.available():
        print("stack OCR non installato: test saltato")
        return

    SessionLocal = init_db()
    src_pdf = Path(__file__).resolve().parent / "assets" / "scansione_con_immagini.pdf"
    if not src_pdf.is_file():
        print(f"file di test mancante: {src_pdf} — test saltato")
        return

    # l'ultima pagina (firme) come PNG: contiene nomi/telefoni leggibili
    with fitz.open(src_pdf) as doc:
        png = doc[len(doc) - 1].get_pixmap(dpi=150).tobytes("png")

    ca._base_engine_real = ca._base_engine
    ca._base_engine = lambda: FakeEngine([
        ("FULLNAME", "Luigi Verdi"),
        ("ORG", "Global Trade SPA"),
        ("ORG", "Acme Forniture"),
    ])
    try:
        with SessionLocal() as session:
            conv = Conversation(owner_id=1, anonymized=1)
            session.add(conv)
            session.flush()
            cid = conv.id
            att_dir = Path(_tmp.name) / "chats" / cid
            att_dir.mkdir(parents=True)

            att = Attachment(conv_id=cid, direction="in", filename="firma.png",
                             display_filename="firma.png", mime="image/png",
                             size=len(png), anonymization_status="pending")
            session.add(att)
            session.flush()
            p = att_dir / f"{att.id}_firma.png"
            p.write_bytes(png)
            att.original_path = str(p)
            att_id = att.id
            session.commit()

        # --- eligibility -----------------------------------------------------
        check("immagine ammessa all'upload",
              ca.upload_eligibility("firma.png", png) is None)
        check("conteggio immagini dell'allegato",
              image_ocr.count_images(png, ".png") == 1)

        # --- turno SENZA ocr: errore chiaro ----------------------------------
        try:
            ca.anonymize_turn(cid, "Ciao", [att_id], ocr=False)
            check("immagine senza OCR -> errore", False)
        except ca.TurnAnonymizationError as exc:
            check("immagine senza OCR -> errore", "OCR" in str(exc), str(exc))

        # --- turno CON ocr ----------------------------------------------------
        model_content, version, descs = ca.anonymize_turn(
            cid, "Ciao, ecco la firma", [att_id], ocr=True)
        d = descs[0]
        report = d["anonymization_report"]
        check("allegato protetto", d["anonymization_status"] == "protected")
        check("occorrenze nei pixel", (report or {}).get(
            "image_occurrences", 0) > 0, str(report.get("image_occurrences")))
        check("box per l'anteprima nel report",
              bool((report or {}).get("boxes")))
        with SessionLocal() as session:
            att = session.get(Attachment, att_id)
            protected = Path(att.protected_path)
            check("file protetto scritto (png)",
                  protected.is_file() and protected.suffix == ".png")
            cache_p = ca._ocr_cache_path(att)
            check("cache OCR persistita", cache_p.is_file())
            cache = json.loads(cache_p.read_text(encoding="utf-8"))
            planned = [pl["ph"] for img in cache["images"] for pl in img["plan"]]
            check("piano con entità e UNREADABLE",
                  any(not ph.startswith("[UNREADABLE") for ph in planned)
                  and any(ph.startswith("[UNREADABLE") for ph in planned),
                  str(planned[:8]))
            # la pagina di test è quella delle firme: il detector deve
            # trovare le regioni e il piano coprirle come [SIGNATURE_n]
            check("regioni firma/timbro nella cache",
                  any(img.get("regions") for img in cache["images"]),
                  str([img.get("regions") for img in cache["images"]]))
            check("piano con [SIGNATURE_",
                  any(ph.startswith("[SIGNATURE") for ph in planned))
            rows = session.query(ca.ConversationEntity).filter_by(
                conv_id=cid).all()
            check("SIGNATURE/UNREADABLE fuori dal registro",
                  all(not r.placeholder.startswith(("[SIGNATURE",
                                                    "[UNREADABLE"))
                      for r in rows))
            # il protetto non deve essere identico all'originale
            check("pixel modificati", protected.read_bytes() != png)

        # --- PDF scansionato in chat ------------------------------------------
        with SessionLocal() as session:
            att2 = Attachment(conv_id=cid, direction="in",
                              filename="nda.pdf", display_filename="nda.pdf",
                              mime="application/pdf",
                              size=src_pdf.stat().st_size,
                              anonymization_status="pending")
            session.add(att2)
            session.flush()
            p2 = Path(_tmp.name) / "chats" / cid / f"{att2.id}_nda.pdf"
            p2.write_bytes(src_pdf.read_bytes())
            att2.original_path = str(p2)
            att2_id = att2.id
            session.commit()

        _mc, _v, descs2 = ca.anonymize_turn(cid, "Anche il PDF", [att2_id],
                                            ocr=True)
        rep2 = descs2[0]["anonymization_report"] or {}
        check("PDF: occorrenze immagini", rep2.get("image_occurrences", 0) > 0,
              str(rep2.get("image_occurrences")))
        check("PDF: protetto", descs2[0]["anonymization_status"] == "protected")
    finally:
        ca._base_engine = ca._base_engine_real

    print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

"""Verifiche del CONTESTO che la chat costruisce, senza rete e senza Docker.

Copre i tre pezzi che decidono cosa il modello sa e riceve:
  - openrouter/briefing.py   la scheda di un allegato (fogli, colonne, pagine)
  - catalog.sanitize_options le opzioni della UI validate sui metadati
  - chat_routes._model_part  immagini/PDF allegati al messaggio (multimodale)

Gira in un paio di secondi sui file veri di test/: è il test da rilanciare a
ogni modifica di questi tre punti.

Uso:  python backend/tests/chat_context_test.py
"""
import io
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
os.environ.setdefault("BLOCKINGBEAR_DATA_DIR", str(HERE / "data" / "test_context"))
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from app.db import Attachment                                    # noqa: E402
from app.openrouter import briefing, catalog                     # noqa: E402
from app.routes import chat_routes                               # noqa: E402

ASSETS = Path(__file__).resolve().parent / "assets"
TMP = Path(os.environ["BLOCKINGBEAR_DATA_DIR"]) / "tmp"

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


def card(name, mime=None):
    return briefing.describe(str(ASSETS / name), name, mime)


def joined(c):
    return "\n".join(c.get("lines") or ())


# --- 1. schede degli allegati ------------------------------------------------

def test_briefing():
    c = card("cartella_multifoglio.xlsx")
    txt = joined(c)
    check("xlsx: fogli e intestazioni",
          c["kind"] == "excel" and "5 fogli" in c["label"]
          and 'foglio "Clienti": 11 righe × 7 colonne' in txt
          and "colonne: Nome | Email" in txt,
          c["label"])
    check("xlsx: righe di esempio", "mario.rossi@esempio.it" in txt)

    c = card("tabella_grande.xlsx")
    txt = joined(c)
    check("xlsx grande: dimensioni senza scorrerlo tutto",
          "4.001 righe × 22 colonne" in txt, c["label"])

    c = card("contratto.pdf")
    txt = joined(c)
    check("pdf: pagine e testo iniziale",
          c["kind"] == "pdf" and "2 pagine" in txt
          and "ACME FORNITURE" in txt, c["label"])

    c = card("contratto.docx")
    check("docx: paragrafi e attacco",
          c["kind"] == "word" and "138 paragrafi" in joined(c)
          and "CONTRATTO" in joined(c), c["label"])

    c = card("presentazione.pptx")
    check("pptx: diapositive e titoli",
          c["kind"] == "powerpoint" and "3 diapositive" in joined(c)
          and "Offerta commerciale" in joined(c), c["label"])

    c = card("lettera.txt")
    check("txt: codifica onesta (niente falso utf-8-sig)",
          c["kind"] == "text" and "codifica: utf-8 ·" in joined(c),
          joined(c).splitlines()[0])

    c = card("cartella_multifoglio.xls")
    check("formato legacy: istruzioni per la sandbox",
          c["kind"] == "legacy" and "--convert-to xlsx" in joined(c))

    TMP.mkdir(parents=True, exist_ok=True)
    csvp = TMP / "vendite.csv"
    csvp.write_text("Data;Cliente;Importo\n2026-01-03;Rossi SpA;1200,50\n"
                    "2026-01-04;Bianchi srl;98,00\n", encoding="utf-8")
    c = briefing.describe(str(csvp), "vendite.csv", "text/csv")
    txt = joined(c)
    check("csv: separatore riconosciuto e colonne",
          "separatore: punto e virgola" in txt
          and "colonne: Data | Cliente | Importo" in txt
          and "Rossi SpA" in txt, c["label"])

    jp = TMP / "conf.json"
    jp.write_text('{"alpha": 1, "beta": [1, 2, 3]}', encoding="utf-8")
    c = briefing.describe(str(jp), "conf.json", None)
    check("json: chiavi di primo livello",
          c["kind"] == "json" and "chiavi: alpha, beta" in joined(c))

    png = TMP / "img.png"
    png.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000001000000008008060000"))
    c = briefing.describe(str(png), "img.png", "image/png")
    check("png: dimensioni dall'intestazione",
          c["kind"] == "image" and "256×128" in c["label"], c["label"])

    rotto = TMP / "rotto.xlsx"
    rotto.write_bytes(b"non sono uno zip")
    c = briefing.describe(str(rotto), "rotto.xlsx", None)
    check("file corrotto: scheda povera, nessuna eccezione",
          c["kind"] == "binary" and c.get("error"), str(c.get("error"))[:60])

    lungo = {"kind": "text", "label": "x", "lines": ["a" * 5000]}
    p = briefing.as_prompt("grande.txt", 5000, lungo)
    check("scheda troncata al tetto",
          len(p) <= briefing.MAX_CARD_CHARS + 1 and p.endswith("…"),
          f"{len(p)} caratteri")


# --- 2. opzioni del modello --------------------------------------------------

ENTRY = {
    "id": "test/model",
    "supported_parameters": ["tools", "reasoning", "temperature", "max_tokens"],
    "max_completion_tokens": 4096,
    "context_length": 128000,
    "reasoning": {"mandatory": False, "default_enabled": False,
                  "supported_efforts": ["high", "medium", "low"],
                  "supports_max_tokens": True},
}


def test_options():
    r, p = catalog.sanitize_options({}, ENTRY)
    check("nessuna opzione: niente reasoning né parametri",
          r is None and p == {}, f"{r} {p}")

    r, p = catalog.sanitize_options(
        {"reasoning": {"enabled": True, "effort": "low"}}, ENTRY)
    check("ragionamento con impegno valido",
          r == {"enabled": True, "effort": "low"}, json.dumps(r))

    r, _ = catalog.sanitize_options(
        {"reasoning": {"enabled": True, "effort": "turbo"}}, ENTRY)
    check("impegno inventato scartato",
          r == {"enabled": True}, json.dumps(r))

    r, _ = catalog.sanitize_options(
        {"reasoning": {"enabled": True, "max_tokens": 2048,
                       "exclude": True}}, ENTRY)
    check("token di ragionamento + esclusione",
          r == {"enabled": True, "max_tokens": 2048, "exclude": True},
          json.dumps(r))

    no_budget = {**ENTRY, "reasoning": {**ENTRY["reasoning"],
                                        "supports_max_tokens": False}}
    r, _ = catalog.sanitize_options(
        {"reasoning": {"enabled": True, "max_tokens": 2048}}, no_budget)
    check("token di ragionamento non supportati: tolti",
          r == {"enabled": True}, json.dumps(r))

    forced = {**ENTRY, "reasoning": {"mandatory": True}}
    r, _ = catalog.sanitize_options({"reasoning": {"enabled": False}}, forced)
    check("ragionamento obbligatorio: non si spegne",
          r == {"enabled": True}, json.dumps(r))

    by_default = {**ENTRY, "reasoning": {"default_enabled": True}}
    r, _ = catalog.sanitize_options({"reasoning": {"enabled": False}},
                                    by_default)
    check("attivo per default: spegnerlo va chiesto",
          r == {"enabled": False}, json.dumps(r))

    senza = {"id": "x", "supported_parameters": ["max_tokens"]}
    r, _ = catalog.sanitize_options({"reasoning": {"enabled": True}}, senza)
    check("modello senza ragionamento: niente reasoning", r is None, str(r))

    _r, p = catalog.sanitize_options(
        {"params": {"temperature": 0.25, "top_k": 40, "inventato": 1}}, ENTRY)
    check("parametri filtrati su supported_parameters",
          p == {"temperature": 0.25}, json.dumps(p))

    _r, p = catalog.sanitize_options(
        {"params": {"temperature": 9, "max_tokens": 99999}}, ENTRY)
    check("valori riportati nell'intervallo",
          p == {"temperature": 2.0, "max_tokens": 4096}, json.dumps(p))

    _r, p = catalog.sanitize_options({"params": {"temperature": "caldo"}},
                                     ENTRY)
    check("valore non numerico ignorato", p == {}, json.dumps(p))

    r, p = catalog.sanitize_options(
        {"reasoning": {"enabled": True, "effort": "high"},
         "params": {"temperature": 0.5}}, None)
    check("modello sconosciuto: si passa quel che c'è",
          r == {"enabled": True, "effort": "high"}
          and p == {"temperature": 0.5}, f"{r} {p}")


# --- 3. allegati verso il modello (multimodale) ------------------------------

def _att(att_id, filename, mime, data):
    """Attachment NON persistito + i byte al percorso che _att_path si aspetta."""
    att = Attachment(id=att_id, conv_id="convtest", direction="in",
                     source="upload", filename=filename, mime=mime,
                     size=len(data))
    path = chat_routes._att_path(att)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return att


def test_model_parts():
    png = bytes.fromhex("89504e470d0a1a0a0000000d494844520000000100000001"
                        "08060000") + b"\x00" * 40
    img = _att("att1", "foto.png", "image/png", png)
    pdf = _att("att2", "contratto.pdf", "application/pdf",
               (ASSETS / "contratto.pdf").read_bytes())
    csvp = _att("att3", "dati.csv", "text/csv", b"a,b\n1,2\n")

    part = chat_routes._model_part(img, {"text", "image"}, 10 << 20)
    check("immagine a un modello che vede",
          part and part["type"] == "image_url"
          and part["image_url"]["url"].startswith("data:image/png;base64,"),
          str(part)[:60])

    check("immagine a un modello di solo testo: resta nella sandbox",
          chat_routes._model_part(img, {"text"}, 10 << 20) is None)

    part = chat_routes._model_part(pdf, {"text", "file"}, 10 << 20)
    check("pdf a un modello che legge i file",
          part and part["type"] == "file"
          and part["file"]["filename"] == "contratto.pdf"
          and part["file"]["file_data"].startswith(
              "data:application/pdf;base64,"))

    check("pdf oltre il tetto: non si allega",
          chat_routes._model_part(pdf, {"text", "file"}, 1024) is None)
    check("csv: mai come contenuto del messaggio",
          chat_routes._model_part(csvp, {"text", "image", "file"},
                                  10 << 20) is None)

    img.briefing_json = json.dumps(briefing.describe(
        str(chat_routes._att_path(img)), "foto.png", "image/png"))
    csvp.briefing_json = json.dumps(briefing.describe(
        str(chat_routes._att_path(csvp)), "dati.csv", "text/csv"))

    msg = {"role": "user", "content": "Guarda questo"}
    out = chat_routes._with_attachments(msg, [img, csvp],
                                       {"text", "image"}, 10 << 20)
    check("messaggio multimodale: testo primo, poi i file",
          isinstance(out["content"], list) and len(out["content"]) == 2
          and out["content"][0]["type"] == "text"
          and out["content"][1]["type"] == "image_url"
          and "Guarda questo" in out["content"][0]["text"],
          str([p["type"] for p in out["content"]]))
    text = out["content"][0]["text"]
    check("le schede di TUTTI gli allegati sono nel testo",
          "foto.png" in text and "dati.csv" in text
          and "/workspace/inputs" in text and "<allegati>" in text
          and "guardarlo direttamente" in text)

    out = chat_routes._with_attachments(msg, [csvp], {"text"}, 10 << 20)
    check("senza parti multimodali il contenuto resta testo",
          isinstance(out["content"], str)
          and "colonne: a | b" in out["content"]
          and "guardarlo direttamente" not in out["content"],
          out["content"][:60].replace("\n", " "))


def main():
    print("=== schede degli allegati ===")
    test_briefing()
    print("=== opzioni del modello ===")
    test_options()
    print("=== allegati verso il modello ===")
    test_model_parts()
    print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

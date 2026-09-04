"""Pool di profili LibreOffice.

Prima del pool convert._convert serializzava TUTTE le conversioni su un unico
profilo (soffice non tollera istanze concorrenti sullo stesso profilo). Ora
c'è un pool di _SLOTS profili persistenti, ognuno in uso esclusivo: due
conversioni convivono su profili diversi e l'anteprima chat converte i suoi
due lati (originale/anonimizzato) in parallelo con chat_staging._pdf_pair.

Copre: derivazione dei percorsi profilo (compatibilità coi test che
riassegnano _PROFILE_DIR), esclusività e rilascio degli slot (anche su
eccezione), conversioni reali concorrenti senza contaminazione incrociata,
equivalenza col percorso seriale, errori (bytes invalidi, timeout) che non
avvelenano il pool, _pdf_pair (ordine, propagazione errori) e build_preview
end-to-end sul percorso parallelo (docx e txt, box su entrambi i lati).

Uso:  python backend/tests/convert_pool_test.py
"""
import io
import os
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DATA_DIR = HERE / "data" / "test_convert_pool"
shutil.rmtree(DATA_DIR, ignore_errors=True)
DATA_DIR.mkdir(parents=True)
os.environ["BLOCKINGBEAR_DATA_DIR"] = str(DATA_DIR)
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")

from app import chat_staging                                      # noqa: E402
from app.engine import convert, pdf as pdf_mod                    # noqa: E402
from app.engine.convert import ConvertError, to_pdf               # noqa: E402
from app.engine.pdf import extract_text as extract_pdf            # noqa: E402

convert._PROFILE_DIR = DATA_DIR / "lo_profile"

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


def make_docx(paragraphs):
    """docx minimale (niente python-docx nel venv): basta a LibreOffice."""
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/'
           f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
          'content-types"><Default Extension="rels" ContentType="application/'
          'vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/'
          'vnd.openxmlformats-officedocument.wordprocessingml.document.main'
          '+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Id="rId1" Type="http://schemas'
            '.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def pdf_text(pdf_bytes):
    return extract_pdf(pdf_bytes, allow_empty=True)[0]


def main():
    # --- 1. derivazione dei profili -----------------------------------------
    print("\n[1] percorsi del pool")
    check("slot 0 = _PROFILE_DIR (compatibile col passato)",
          convert._profile(0) == convert._PROFILE_DIR)
    check("slot 1 = fratello con suffisso 2",
          convert._profile(1) == convert._PROFILE_DIR.with_name(
              convert._PROFILE_DIR.name + "2"),
          convert._profile(1))
    saved = convert._PROFILE_DIR
    convert._PROFILE_DIR = DATA_DIR / "altrove" / "lo_profile"
    check("riassegnare _PROFILE_DIR sposta TUTTO il pool (isolamento test)",
          convert._profile(0).parent == DATA_DIR / "altrove"
          and convert._profile(1).parent == DATA_DIR / "altrove")
    convert._PROFILE_DIR = saved
    check("pool di 2 slot, tutti liberi in quiete",
          convert._SLOTS == 2 and sorted(convert._free) == [0, 1])

    # --- 2. esclusività e rilascio degli slot -------------------------------
    print("\n[2] slot: esclusività, attesa, rilascio")
    seen, active, peak = [], [0], [0]
    gate = threading.Barrier(4)
    guard = threading.Lock()

    def worker():
        gate.wait()
        with convert._slot() as prof:
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                seen.append(prof)
            time.sleep(0.15)
            with guard:
                active[0] -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check("mai più di _SLOTS conversioni insieme", peak[0] == 2, peak[0])
    check("4 richieste servite, solo profili del pool",
          len(seen) == 4 and set(seen) <= {convert._profile(0),
                                           convert._profile(1)})
    check("tutti gli slot tornano liberi", sorted(convert._free) == [0, 1])

    try:
        with convert._slot():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    check("slot rilasciato anche su eccezione",
          sorted(convert._free) == [0, 1])

    with convert._slot() as prof:
        check("una conversione alla volta usa il profilo BASE (slot più "
              "basso libero)", prof == convert._profile(0))

    # --- 3. conversioni reali: parallelo == seriale, nessuna contaminazione --
    print("\n[3] conversioni reali concorrenti")
    if not convert.available():
        print("  SKIP: LibreOffice non installato")
        return
    doc_a = make_docx([f"Documento ALFA riga {i} marcatore UNICO-AAA."
                       for i in range(30)])
    doc_b = make_docx([f"Documento BETA riga {i} marcatore UNICO-BBB."
                       for i in range(30)])
    # riferimento seriale (e riscaldamento del profilo base)
    t0 = time.perf_counter()
    ref_a = to_pdf(doc_a, suffix=".docx")
    t_a = time.perf_counter() - t0
    t0 = time.perf_counter()
    ref_b = to_pdf(doc_b, suffix=".docx")
    t_b = time.perf_counter() - t0

    results = {}

    def convert_one(tag, data):
        results[tag] = to_pdf(data, suffix=".docx")

    t0 = time.perf_counter()
    threads = [threading.Thread(target=convert_one, args=("a", doc_a)),
               threading.Thread(target=convert_one, args=("b", doc_b))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    t_pair = time.perf_counter() - t0
    print(f"  (seriale {t_a:.1f}s + {t_b:.1f}s, parallelo {t_pair:.1f}s "
          f"— include il bootstrap del profilo 2)")
    check("entrambe le conversioni parallele riuscite",
          set(results) == {"a", "b"} and all(results.values()))
    ta, tb = pdf_text(results["a"]), pdf_text(results["b"])
    check("nessuna contaminazione: A contiene solo il suo marcatore",
          "UNICO-AAA" in ta and "UNICO-BBB" not in ta)
    check("nessuna contaminazione: B contiene solo il suo marcatore",
          "UNICO-BBB" in tb and "UNICO-AAA" not in tb)
    check("parallelo == seriale (testo estratto identico, lato A)",
          ta == pdf_text(ref_a))
    check("parallelo == seriale (testo estratto identico, lato B)",
          tb == pdf_text(ref_b))
    check("stesso numero di pagine del riferimento",
          pdf_mod.page_sizes(results["a"]) == pdf_mod.page_sizes(ref_a)
          and pdf_mod.page_sizes(results["b"]) == pdf_mod.page_sizes(ref_b))
    check("entrambi i profili del pool esistono su disco",
          convert._profile(0).is_dir() and convert._profile(1).is_dir())

    # profili ormai CALDI: il parallelo deve battere la somma dei seriali
    t0 = time.perf_counter()
    to_pdf(doc_a, suffix=".docx")
    warm_serial = time.perf_counter() - t0
    t0 = time.perf_counter()
    threads = [threading.Thread(target=convert_one, args=("a", doc_a)),
               threading.Thread(target=convert_one, args=("b", doc_b))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    t_pair_warm = time.perf_counter() - t0
    print(f"  (a caldo: 1 seriale {warm_serial:.1f}s, coppia parallela "
          f"{t_pair_warm:.1f}s)")
    check("a profili caldi la coppia costa meno di due seriali",
          t_pair_warm < 1.8 * warm_serial,
          f"{t_pair_warm:.1f}s vs 2x{warm_serial:.1f}s")

    # --- 4. stress: 8 thread, formati misti ----------------------------------
    print("\n[4] stress: 8 conversioni concorrenti, formati misti")
    jobs = []
    for i in range(8):
        if i % 2:
            jobs.append((f"txt{i}", f"testo semplice {i} marcatore-T{i}\n"
                         .encode("utf-8") * 20, ".txt"))
        else:
            jobs.append((f"docx{i}", make_docx(
                [f"stress {i} marcatore-D{i} riga {r}" for r in range(10)]),
                ".docx"))
    out, errors = {}, []

    def stress_one(tag, data, suffix):
        try:
            out[tag] = to_pdf(data, suffix=suffix)
        except Exception as exc:            # noqa: BLE001
            errors.append((tag, exc))

    threads = [threading.Thread(target=stress_one, args=j) for j in jobs]
    t0 = time.perf_counter()
    [t.start() for t in threads]
    [t.join() for t in threads]
    print(f"  (8 conversioni in {time.perf_counter() - t0:.1f}s)")
    check("nessun errore su 8 conversioni concorrenti", not errors, errors)
    marks_ok = all(
        (f"marcatore-T{i}" if i % 2 else f"marcatore-D{i}")
        in pdf_text(out[f"{'txt' if i % 2 else 'docx'}{i}"])
        for i in range(8))
    check("ogni PDF contiene il testo del SUO input", marks_ok)
    check("slot tutti liberi a fine stress", sorted(convert._free) == [0, 1])

    # --- 5. errori: non avvelenano il pool ------------------------------------
    # NB: testo spazzatura con suffisso .docx NON è un errore (LibreOffice lo
    # importa come testo); un archivio PK troncato sì.
    print("\n[5] errori e recupero")
    broken = b"PK\x03\x04" + b"\x00" * 40
    try:
        to_pdf(broken, suffix=".docx")
        check("bytes invalidi -> ConvertError", False, "nessuna eccezione")
    except ConvertError:
        check("bytes invalidi -> ConvertError", True)
    check("slot liberi dopo l'errore", sorted(convert._free) == [0, 1])
    ok = to_pdf(doc_a, suffix=".docx")
    check("il profilo (rigenerato dal retry) converte di nuovo",
          "UNICO-AAA" in pdf_text(ok))

    try:
        to_pdf(doc_a, suffix=".docx", timeout=0.05)
        check("timeout -> ConvertError", False, "nessuna eccezione")
    except ConvertError as exc:
        check("timeout -> ConvertError", "scaduta" in str(exc), exc)
    check("slot liberi dopo il timeout", sorted(convert._free) == [0, 1])
    ok = to_pdf(doc_a, suffix=".docx")
    check("conversione sana dopo il timeout", "UNICO-AAA" in pdf_text(ok))

    # --- 6. _pdf_pair ----------------------------------------------------------
    print("\n[6] chat_staging._pdf_pair")
    orig, anon = chat_staging._pdf_pair(doc_a, doc_b, ".docx")
    check("ordine dei ritorni: (originale, anonimizzato)",
          "UNICO-AAA" in pdf_text(orig) and "UNICO-BBB" in pdf_text(anon))
    try:
        chat_staging._pdf_pair(doc_a, broken, ".docx")
        check("errore su un lato -> propaga ConvertError", False,
              "nessuna eccezione")
    except ConvertError:
        check("errore su un lato -> propaga ConvertError", True)
    check("il pool sopravvive all'errore di un lato",
          sorted(convert._free) == [0, 1])
    ok = to_pdf(doc_a, suffix=".docx")
    check("conversione sana dopo l'errore della coppia",
          "UNICO-AAA" in pdf_text(ok))

    # --- 7. build_preview end-to-end sul percorso parallelo -------------------
    print("\n[7] build_preview (docx e txt) sul percorso parallelo")
    value, ph = "Mario Rossi", "[FULLNAME_1]"
    d_orig = make_docx([f"Il cliente {value} ha firmato.", "Riga neutra."])
    d_anon = make_docx([f"Il cliente {ph} ha firmato.", "Riga neutra."])
    po, pa, info = chat_staging.build_preview(
        ".docx", d_orig, d_anon, [(ph, value)], {ph: value}, ())
    ob = [b for bl in info["original_boxes"].values() for b in bl]
    ab = [b for bl in info["anonymized_boxes"].values() for b in bl]
    check("docx: il valore ha un box a sinistra",
          any(b["ph"] == ph for b in ob), ob)
    check("docx: il placeholder ha un box a destra",
          any(b["ph"] == ph for b in ab), ab)
    check("docx: label derivata sul box",
          all(b.get("label") == "FULLNAME" for b in ob + ab))
    check("docx: page_sizes per entrambi i lati",
          len(info["page_sizes"]["original"]) >= 1
          and len(info["page_sizes"]["anonymized"]) >= 1)
    check("docx: i due lati sono PDF con il testo giusto",
          value in pdf_text(po) and ph in pdf_text(pa))

    t_orig = f"Contatto: {value}\ntelefono in chiaro\n".encode("utf-8")
    t_anon = f"Contatto: {ph}\ntelefono in chiaro\n".encode("utf-8")
    po, pa, info = chat_staging.build_preview(
        ".txt", t_orig, t_anon, [(ph, value)], {ph: value}, ())
    check("txt: box su entrambi i lati",
          any(b["ph"] == ph
              for bl in info["original_boxes"].values() for b in bl)
          and any(b["ph"] == ph
                  for bl in info["anonymized_boxes"].values() for b in bl))
    check("txt: preview_truncated assente/False",
          info["preview_truncated"] is False)


if __name__ == "__main__":
    main()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)

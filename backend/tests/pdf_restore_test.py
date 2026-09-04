"""Ripristino dei segnaposto nei PDF (engine/pdf_export.restore_pdf).

Niente modello, niente rete, niente Docker: serve solo LibreOffice, che genera
i PDF di prova esattamente come fa la sandbox della chat (python-docx/HTML ->
`soffice --convert-to pdf`). Il profilo LibreOffice è ISOLATO: così il test
non litiga con il backend di sviluppo eventualmente attivo.

Cosa verifica:
  - i valori tornano nel testo, nelle annotazioni, nei campi modulo, nei
    segnalibri e nei metadati;
  - il testo ACCANTO al segnaposto non viene mangiato dalla redazione (il
    difetto che si vedrebbe subito: "mario.rossi@x.it ntro il 15/09");
  - un valore più lungo del segnaposto non invade il carattere successivo né
    scavalca il bordo della cella: viene scritto con un corpo ridotto;
  - quando il .docx sorgente c'è, il PDF viene RIGENERATO da quello e i corpi
    restano intatti (il ripristino glifo per glifo resta il ripiego);
  - i bordi di tabella e le immagini sopravvivono alla rimozione del testo;
  - un segnaposto che non è nel registro resta nel file ed è dichiarato;
  - andata e ritorno: redact_pdf -> restore_pdf rimette i valori originali.

Uso:
    python backend/tests/pdf_restore_test.py
"""

import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
_tmp = tempfile.TemporaryDirectory(prefix="blockingbear-pdf-restore-")
os.environ["BLOCKINGBEAR_DATA_DIR"] = _tmp.name
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
sys.path.insert(0, str(HERE))

import fitz                                                        # noqa: E402
from app.chat_anonymization import restore_artifact                # noqa: E402
from app.engine import convert                                     # noqa: E402
from app.engine import pdf_export as px                            # noqa: E402
from app.engine.pdf_export import (PdfError, redact_pdf, restore_pdf,  # noqa: E402
                                   sub_placeholders)

# profilo tutto nostro: due soffice sullo stesso profilo si annullano a vicenda
convert._PROFILE_DIR = Path(_tmp.name) / "lo_profile"

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


def ws(text):
    """Testo senza spaziatura: la spaziatura cambia con la ricostruzione del
    content stream, i caratteri no."""
    return re.sub(r"\s+", "", text or "")


def page_text(pdf_bytes, sort=False):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return "\n".join(p.get_text(sort=sort) for p in doc)


def rect_of(pdf_bytes, needle, page=0):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        hits = doc[page].search_for(needle)
    return hits[0] if hits else None


def n_drawings(pdf_bytes):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return sum(len(p.get_drawings()) for p in doc)


def _spans_with(pdf_bytes, needle):
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", ()):
                    for span in line.get("spans", ()):
                        if needle in span.get("text", ""):
                            yield span


def span_fonts(pdf_bytes, needle):
    """Nomi dei font degli span che contengono `needle`."""
    return [s.get("font", "") for s in _spans_with(pdf_bytes, needle)]


def span_sizes(pdf_bytes, needle):
    return [s.get("size", 0.0) for s in _spans_with(pdf_bytes, needle)]


def all_span_sizes(pdf_bytes):
    """Corpo di TUTTI gli span non vuoti: il modo più diretto per dire che nel
    documento non è rimasto niente di rimpicciolito."""
    out = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", ()):
                    for span in line.get("spans", ()):
                        if span.get("text", "").strip():
                            out.append(span.get("size", 0.0))
    return out


# --------------------------------------------------------------------------- #
# Il documento di prova: quello che scrive il modello in una chat anonimizzata
# --------------------------------------------------------------------------- #
MAPPING = {
    "[FULLNAME_1]": "Mario Rossi",
    "[ORG_1]": "Fratelli Bianchi & Figli S.r.l.",
    "[ADDRESS_1]": "Via Roma 10, 20121 Milano",
    # 4 volte più lungo del segnaposto, in mezzo a una riga: è il caso
    # scomodo, quello che decide la politica di adattamento
    "[EMAIL_1]": "mario.rossi@fratellibianchiefigli.example.com",
    "[PHONE_1]": "+39 02 5512 8890",
    "[IBAN_1]": "IT60X0542811101000000123456",
    "[FULLNAME_2]": "Ștefan Ionescu",            # fuori Latin-1: va approssimato
    "[IBAN_2]": "IT02L1234512345123456789012",
}

# I bordi di tabella vanno chiesti con gli attributi HTML: LibreOffice ignora
# `border` del CSS convertendo HTML -> PDF e il PDF uscirebbe senza filetti,
# cioè senza il limite che il ripristino deve rispettare nelle celle.
HTML = """<html><head><meta charset="utf-8"><style>
body { font-family: 'Calibri', sans-serif; font-size: 11pt; }
h1 { font-family: 'Liberation Serif', serif; font-size: 15pt; }
</style></head><body>
<h1>Sollecito per [ORG_1]</h1>
<p>Gentile [FULLNAME_1], ricapitolando quanto detto:</p>
<p>risulta insoluta la fattura 2026/114 intestata a [ORG_1], con sede in
[ADDRESS_1]. Scriva a [EMAIL_1] entro il 15/09/2026 oppure telefoni al
[PHONE_1] chiedendo dell'ufficio crediti.</p>
<table border="1" cellspacing="0" cellpadding="3" width="60%">
<tr><td>Cliente</td><td>Importo</td><td>IBAN</td></tr>
<tr><td>[ORG_1]</td><td>1.240,00</td><td>[IBAN_1]</td></tr>
<tr><td>[FULLNAME_2]</td><td>310,50</td><td>[IBAN_2]</td></tr></table>
<p>Pratica seguita da [FULLNAME_1] con il codice [MISTERO_9] non in registro.</p>
</body></html>"""


def build_fixture():
    """HTML -> PDF con LibreOffice, poi si aggiungono a mano i posti dove il
    testo NON è content stream (annotazione, segnalibro, metadati): lì il
    ripristino passa dalle stesse funzioni della redazione."""
    pdf = convert.to_pdf(HTML.encode("utf-8"), suffix=".html")
    doc = fitz.open(stream=pdf, filetype="pdf")
    page = doc[0]
    annot = page.add_text_annot((520, 60), "Verificare [IBAN_1] di [ORG_1].")
    annot.set_info(title="[FULLNAME_1]", content="Verificare [IBAN_1] di [ORG_1].")
    annot.update()
    doc.set_toc([[1, "Sollecito a [FULLNAME_1]", 1]])
    doc.set_metadata({"title": "Sollecito [ORG_1]",
                      "author": "[FULLNAME_1]",
                      "keywords": "[IBAN_1]"})
    out = doc.tobytes(deflate=True)
    doc.close()
    return out


def main():
    print("Fixture: HTML -> PDF con LibreOffice (può volerci qualche secondo)")
    src = build_fixture()
    print(f"  PDF di partenza: {len(src)} byte, "
          f"{len(px.PLACEHOLDER_RE.findall(page_text(src)))} segnaposto nel testo")

    out, rep = restore_pdf(src, MAPPING)
    text = page_text(out)

    # 1. i valori sono nel documento
    missing = [ph for ph, value in MAPPING.items()
               if ws(px._latin1(value)[0]) not in ws(text)]
    check("tutti i valori del registro sono nel PDF ripristinato", not missing,
          f"mancanti: {missing}")
    check("nessun valore perso (rimosso e non riscritto)", rep["lost"] == [],
          str(rep["lost"]))
    expected = sum(1 for ph in px.PLACEHOLDER_RE.findall(page_text(src))
                   if ph in MAPPING)
    check("ripristinate tutte le occorrenze note, e solo quelle",
          rep["restored"] == expected, f"restored={rep['restored']} "
          f"attese={expected} by_placeholder={rep['by_placeholder']}")

    # 2. il segnaposto fuori registro resta, ed è dichiarato
    check("segnaposto non in registro conservato e dichiarato",
          rep["remaining"] == ["[MISTERO_9]"] and "[MISTERO_9]" in text,
          str(rep["remaining"]))

    # 3. il testo accanto non è stato mangiato dalla redazione.
    # Si confrontano i due documenti PRIVATI di segnaposto e valori: quel che
    # resta è il contesto, e deve essere identico carattere per carattere. Un
    # rettangolo di redazione che sborda di mezzo punto sul glifo accanto si
    # vede qui e solo qui (la "e" di "entro" che sparisce).
    src_ctx = ws(px._PH_BROKEN_RE.sub("", page_text(src)))
    out_ctx = ws(text)
    for value in MAPPING.values():
        out_ctx = out_ctx.replace(ws(px._latin1(value)[0]), "")
    out_ctx = px.PLACEHOLDER_RE.sub("", out_ctx)      # [MISTERO_9], non toccato
    where = next((i for i, (a, b) in enumerate(zip(src_ctx, out_ctx)) if a != b),
                 min(len(src_ctx), len(out_ctx)))
    check("il testo intorno ai segnaposto è intatto, carattere per carattere",
          src_ctx == out_ctx,
          "" if src_ctx == out_ctx else
          f"divergenza a {where}\n        atteso:   ...{src_ctx[max(0, where - 30):where + 30]}..."
          f"\n        ottenuto: ...{out_ctx[max(0, where - 30):where + 30]}...")

    # 4. il valore lungo non invade il carattere successivo
    email = MAPPING["[EMAIL_1]"]
    r_email = rect_of(out, email)
    r_next = rect_of(out, "entro il 15/09/2026")
    check("il valore lungo non si sovrappone al testo che segue",
          r_email is not None and r_next is not None and r_email.x1 <= r_next.x0 + 0.6,
          f"email.x1={r_email.x1:.1f} next.x0={r_next.x0:.1f}" if r_email and r_next
          else f"email={r_email} next={r_next}")
    shrunk_phs = {s["ph"] for s in rep["shrunk"]}
    check("il valore lungo è stato scritto con corpo ridotto",
          "[EMAIL_1]" in shrunk_phs,
          str(rep["shrunk"]))

    # 5. in tabella il valore resta dentro la cella (bordo verticale a destra)
    with fitz.open(stream=src, filetype="pdf") as doc:
        page = doc[0]
        ph_rect = page.search_for("[IBAN_1]")[0]
        borders = [x for x, y0, y1 in px._page_vlines(page)
                   if x > ph_rect.x1 - 0.5
                   and min(ph_rect.y1, y1) - max(ph_rect.y0, y0) > 0.3 * ph_rect.height]
    r_iban = rect_of(out, MAPPING["[IBAN_1]"])
    limit = min(borders) if borders else None
    check("in tabella il valore non scavalca il bordo della cella",
          limit is not None and r_iban is not None and r_iban.x1 <= limit + 0.6,
          f"iban.x1={r_iban.x1:.1f} bordo={limit:.1f}" if r_iban and limit
          else f"iban={r_iban} bordi={borders}")

    # 6. la grafica sopravvive alla rimozione del testo
    check("bordi di tabella conservati",
          n_drawings(out) >= n_drawings(src),
          f"prima={n_drawings(src)} dopo={n_drawings(out)}")

    # 7. font e corpo seguono lo span di partenza
    fonts_h1 = span_fonts(out, "Fratelli Bianchi")
    check("il titolo serif viene riscritto con un serif",
          any("Times" in f or "Serif" in f for f in fonts_h1),
          str(fonts_h1))
    fonts_body = span_fonts(out, "Mario Rossi")
    check("il corpo del testo viene riscritto con un sans",
          any("Helvetica" in f for f in fonts_body), str(fonts_body))
    # controllo positivo: dove c'è spazio il corpo NON si tocca (il titolo ha
    # il segnaposto a fine riga, quindi tutto il margine destro a disposizione)
    sizes_h1 = span_sizes(out, "Fratelli Bianchi")
    check("dove c'è spazio il valore mantiene il corpo originale",
          any(abs(s - 15.0) < 0.6 for s in sizes_h1), str(sizes_h1))

    # 8. caratteri fuori Latin-1
    check("valore fuori Latin-1 approssimato e dichiarato",
          rep["degraded"] == ["[FULLNAME_2]"] and "Stefan Ionescu" in text,
          f"degraded={rep['degraded']}")

    # 9. testo fuori dal content stream
    with fitz.open(stream=out, filetype="pdf") as doc:
        annots = [a.info for a in doc[0].annots()]
        toc = doc.get_toc(simple=True)
        meta = doc.metadata
    check("annotazioni ripristinate",
          rep["annots"] >= 3
          and all("[" not in (a.get("content") or "") for a in annots)
          and any("Mario Rossi" in (a.get("title") or "") for a in annots),
          f"annots={rep['annots']} info={annots}")
    check("segnalibri ripristinati",
          rep["toc"] == 1 and toc and "Mario Rossi" in toc[0][1], str(toc))
    check("metadati ripristinati",
          rep["metadata"] == 3 and "Fratelli Bianchi" in (meta.get("title") or "")
          and "Mario Rossi" in (meta.get("author") or ""),
          f"metadata={rep['metadata']} title={meta.get('title')!r}")

    # 10. secondo giro: niente da fare, e nulla si rompe
    out2, rep2 = restore_pdf(out, MAPPING)
    check("ripristino idempotente",
          rep2["restored"] == 0 and rep2["remaining"] == ["[MISTERO_9]"]
          and ws(MAPPING["[ORG_1]"]) in ws(page_text(out2)),
          f"restored={rep2['restored']} remaining={rep2['remaining']}")

    # 11. errori d'uso
    try:
        restore_pdf(src, {})
        check("mappa vuota rifiutata", False)
    except PdfError:
        check("mappa vuota rifiutata", True)
    try:
        restore_pdf(b"non sono un pdf", MAPPING)
        check("file non PDF rifiutato", False)
    except PdfError:
        check("file non PDF rifiutato", True)

    # 12. andata e ritorno: la redazione e il suo inverso sulla stessa pagina.
    # Valori tutti Latin-1: qui si pretende il ritorno IDENTICO, e "Ștefan"
    # tornerebbe "Stefan" (i base-14 non hanno quel glifo, vedi punto 8).
    real = dict(MAPPING, **{"[FULLNAME_2]": "Stefano Ionesco"})
    plain = HTML
    for ph, value in real.items():
        plain = plain.replace(ph, value)
    plain = plain.replace("[MISTERO_9]", "AB-77/2026")
    original = convert.to_pdf(plain.encode("utf-8"), suffix=".html")
    with fitz.open(stream=original, filetype="pdf") as doc:
        page = doc[0]
        note = page.add_text_annot((520, 60), f"Chiamare {real['[FULLNAME_1]']}")
        note.set_info(title=real["[FULLNAME_1]"],
                      content=f"Chiamare {real['[FULLNAME_1]']}")
        note.update()
        doc.set_toc([[1, f"Sollecito a {real['[FULLNAME_1]']}", 1]])
        original = doc.tobytes(deflate=True)
    redacted, r_rep = redact_pdf(original, real)
    check("redazione: nessun valore reale residuo",
          not r_rep["residual"] and not r_rep["not_found"],
          f"residual={r_rep['residual']} not_found={r_rep['not_found']}")
    # i tre _scrub_* sono condivisi fra le due direzioni: se la redazione
    # smettesse di coprire annotazioni e segnalibri il residual non basterebbe
    # a dirlo (lì guarda solo i valori, non da dove vengono)
    check("redazione: annotazioni e segnalibri coperti anche in andata",
          r_rep["annots"] >= 2 and r_rep["toc"] == 1,
          f"annots={r_rep['annots']} toc={r_rep['toc']}")
    back, b_rep = restore_pdf(redacted, real)
    back_text = page_text(back)
    lost_values = [v for v in real.values() if ws(v) not in ws(back_text)]
    check("andata e ritorno: i valori reali tornano tutti",
          not lost_values and not b_rep["remaining"] and not b_rep["lost"],
          f"persi={lost_values} remaining={b_rep['remaining']}")
    check("andata e ritorno: il testo non PII resta intatto",
          "AB-77/2026" in back_text and "ufficio crediti" in ws(back_text).lower()
          or "ufficiocrediti" in ws(back_text).lower(),
          "")

    # 13. sub_placeholders: l'unica implementazione delle sostituzioni testuali
    check("sub_placeholders lascia stare gli sconosciuti",
          sub_placeholders("[A_1] e [B_2]", {"[A_1]": "x"}) == ("x e [B_2]", 1))
    check("sub_placeholders XML-escapa quando serve",
          sub_placeholders("[A_1]", {"[A_1]": "R&D <s>"}, escape=True)
          == ("R&amp;D &lt;s&gt;", 1))

    # 14. rigenerazione dal .docx sorgente (chat_anonymization._rerender_pdf):
    # quando il modello lascia accanto al PDF il documento da cui l'ha ricavato,
    # i valori non si rimpiccioliscono — si reimpagina.
    print("Rigenerazione dal sorgente: HTML -> docx -> PDF con LibreOffice")
    # passaggio per l'ODT: un .html LibreOffice lo apre in Writer/Web, che un
    # filtro di export docx non ce l'ha (nella chat il .docx lo scrive
    # python-docx, che qui non è installato e nel backend non serve)
    src_docx = convert.to_docx(
        convert._convert(HTML.encode("utf-8"), ".html", "odt"), suffix=".odt")
    docx_path = Path(_tmp.name) / "sollecito.docx"
    docx_path.write_bytes(src_docx)
    pdf_path = Path(_tmp.name) / "sollecito.pdf"
    pdf_path.write_bytes(convert.to_pdf(src_docx, suffix=".docx"))

    data, n_re, left_re, extra_re = restore_artifact(pdf_path, MAPPING,
                                                     source=docx_path)
    check("PDF rigenerato dal sorgente invece che ricucito",
          data is not None and extra_re.get("rerendered"), str(extra_re))
    re_text = page_text(data) if data else ""
    missing_re = [ph for ph, value in MAPPING.items()
                  if ws(value) not in ws(re_text)]
    check("tutti i valori del registro sono nel PDF rigenerato", not missing_re,
          f"mancanti: {missing_re}")
    # niente approssimazione Latin-1: qui il valore lo scrive LibreOffice, non
    # un base-14 (nell'altro ramo "Ștefan" diventa "Stefan", vedi punto 8)
    check("i caratteri fuori Latin-1 non vengono approssimati",
          "Ștefan Ionescu" in re_text, "")
    check("segnaposto fuori registro conservato e dichiarato",
          left_re == ["[MISTERO_9]"], str(left_re))
    sizes = all_span_sizes(data) if data else []
    check("nessun valore rimpicciolito: i corpi restano quelli del documento",
          bool(sizes) and min(sizes) >= 8.0,
          f"corpo minimo={min(sizes):.1f}pt" if sizes else "nessuno span")

    # controprova: senza sorgente si ricuce, e il valore lungo si rimpicciolisce
    _d, _n, _l, extra_glyph = restore_artifact(pdf_path, MAPPING)
    check("senza sorgente si torna al ripristino glifo per glifo",
          not extra_glyph.get("rerendered") and bool(extra_glyph.get("shrunk")),
          f"shrunk={extra_glyph.get('shrunk')}")

    print(f"\n{PASS} PASS, {FAIL} FAIL")
    if not FAIL:
        keep = Path(_tmp.name).parent / "blockingbear_pdf_restore_out.pdf"
        keep.write_bytes(out)
        print(f"PDF ripristinato salvato per ispezione a mano: {keep}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        _tmp.cleanup()

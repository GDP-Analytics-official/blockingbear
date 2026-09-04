"""Smoke test della funzionalità "azioni di colonna" (click su A/B/C nella
preview xlsx): parsing delle intestazioni dal PDF di anteprima, estrazione dei
valori di colonna e anonimizzazione manuale deterministica ([CUSTOM_n], senza
modello). Con --model si aggiunge l'integrazione col modello vero (riuso dei
placeholder già assegnati all'upload).

Non tocca il server: usa direttamente gli engine, come smoke_test.py.

Uso:  python backend/tests/column_smoke_test.py [--model]
"""

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app import db
from app.engine import convert, xlsx
from app.engine.pdf import spreadsheet_columns

db.init_db()          # settings letti dal pannello admin (truncate, campione)

TEST_DIR = Path(__file__).resolve().parent / "assets"
FAILS = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


# --------------------------------------------------------------------------- #
# Generatore di xlsx minimali (solo stdlib, stringhe inline): LibreOffice li
# apre senza problemi; servono per i casi limite non coperti dai file in test/
# --------------------------------------------------------------------------- #
def _col_name(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def make_xlsx(sheets, merges=None, hidden_cols=None, page_setup=None):
    """sheets = {nome: [[cella, ...], ...]} (None = cella vuota). merges,
    hidden_cols, page_setup: {nome_foglio: ...} opzionali."""
    merges = merges or {}
    hidden_cols = hidden_cols or {}
    page_setup = page_setup or {}
    names = list(sheets)
    ct_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType='
        f'"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(len(names)))
    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + ct_overrides + "</Types>",
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>" + "".join(
                f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                for i, n in enumerate(names)) + "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/'
                f'officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{i + 1}.xml"/>'
                for i in range(len(names)))
            + f'<Relationship Id="rId{len(names) + 1}" Type="http://schemas.'
              f'openxmlformats.org/officeDocument/2006/relationships/styles" '
              f'Target="styles.xml"/></Relationships>',
        "xl/styles.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellXfs>'
            "</styleSheet>",
    }
    for i, name in enumerate(names):
        rows_xml = []
        for r, row in enumerate(sheets[name], start=1):
            cells = []
            for c, val in enumerate(row):
                if val is None:
                    continue
                ref = f"{_col_name(c)}{r}"
                if isinstance(val, (int, float)):
                    cells.append(f'<c r="{ref}"><v>{val}</v></c>')
                else:
                    v = (str(val).replace("&", "&amp;").replace("<", "&lt;")
                         .replace(">", "&gt;"))
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>')
            if cells:
                rows_xml.append(f'<row r="{r}">' + "".join(cells) + "</row>")
        cols = ""
        if name in hidden_cols:
            cols = "<cols>" + "".join(
                f'<col min="{c + 1}" max="{c + 1}" hidden="1"/>'
                for c in hidden_cols[name]) + "</cols>"
        mc = ""
        if name in merges:
            mc = (f'<mergeCells count="{len(merges[name])}">'
                  + "".join(f'<mergeCell ref="{m}"/>' for m in merges[name])
                  + "</mergeCells>")
        ps = page_setup.get(name, "")
        files[f"xl/worksheets/sheet{i + 1}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + cols + "<sheetData>" + "".join(rows_xml) + "</sheetData>" + mc + ps
            + "</worksheet>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, content in files.items():
            zf.writestr(n, content)
    return buf.getvalue()


def preview_layout(xlsx_bytes, max_rows=150):
    """Come fa il backend: copia troncata+spreadsheet_look -> PDF -> layout."""
    trunc, _ = xlsx.truncate_for_preview(xlsx_bytes, max_rows=max_rows)
    pdf = convert.to_pdf(trunc, suffix=".xlsx")
    return spreadsheet_columns(pdf, xlsx.sheet_names(xlsx_bytes)), pdf


def show(layout):
    for pno in sorted(layout):
        e = layout[pno]
        letters = [c["col"] for c in e["cols"]]
        print(f"    pag {pno + 1}: {e['sheet']!r} -> {letters}")


def sane(layout):
    """Box ordinati, senza sovrapposizioni, lettere crescenti."""
    for e in layout.values():
        cols = e["cols"]
        for a, b in zip(cols, cols[1:]):
            if b["x0"] < a["x1"] - 0.5:
                return False
            if xlsx._letter_idx(a["col"]) >= xlsx._letter_idx(b["col"]):
                return False
        if any(c["x1"] - c["x0"] <= 0 for c in cols):
            return False
    return True


# --------------------------------------------------------------------------- #
# 1) file reali in test/
# --------------------------------------------------------------------------- #
print("=== File reali (test/) ===")
for fname in ("tabella_piccola.xlsx", "cartella_multifoglio.xlsx",
              "tabella_grande.xlsx"):
    p = TEST_DIR / fname
    if not p.exists():
        print(f"  (salto {fname}: non trovato)")
        continue
    data = p.read_bytes()
    names = xlsx.sheet_names(data)
    print(f"\n--- {fname} — fogli: {names}")
    layout, _pdf = preview_layout(data)
    show(layout)
    check(bool(layout), f"{fname}: almeno una pagina con colonne riconosciute")
    check(sane(layout), f"{fname}: box coerenti (ordinati, non sovrapposti)")
    found_sheets = {e["sheet"] for e in layout.values()}
    check(found_sheets <= set(names), f"{fname}: fogli del layout esistenti")
    # column_values sulla prima colonna del primo foglio riconosciuto
    if layout:
        e = layout[min(layout)]
        letter = e["cols"][0]["col"]
        vals, label = xlsx.column_values(data, e["sheet"], letter)
        print(f"    colonna {letter} di {e['sheet']!r}: {len(vals)} distinti, "
              f"header={label!r}, primi: {vals[:3]}")
        check(len(vals) > 0, f"{fname}: column_values non vuoto")

# --------------------------------------------------------------------------- #
# 2) casi limite sintetici
# --------------------------------------------------------------------------- #
print("\n=== Casi limite sintetici ===")

# a) nome foglio con spazi/accenti + 30 colonne (test lettere AA+) + colonna
#    nascosta + un foglio a colonna singola + un foglio vuoto
wide_header = [f"Campo {i + 1}" for i in range(30)]
wide_rows = [wide_header] + [[f"r{r}c{c}" for c in range(30)] for r in range(8)]
data = make_xlsx(
    {"Dati Città Estero": wide_rows,
     "Solo Una": [["Nominativo"], ["Mario Rossi"], ["Luca Bianchi"]],
     "Vuoto": [[]]},
    hidden_cols={"Dati Città Estero": [2]},        # colonna C nascosta
)
names = xlsx.sheet_names(data)
print(f"--- sintetico A — fogli: {names}")
layout, _ = preview_layout(data)
show(layout)
check(sane(layout), "sintetico A: box coerenti")
wide_pages = [e for e in layout.values() if e["sheet"] == "Dati Città Estero"]
check(bool(wide_pages), "sintetico A: foglio con spazi/accenti riconosciuto")
all_letters = {c["col"] for e in wide_pages for c in e["cols"]}
check(any(len(l) == 2 for l in all_letters),
      f"sintetico A: lettere doppie (AA+) riconosciute ({sorted(all_letters)[-3:]})")
# niente aspettative sulla colonna NASCOSTA C: truncate_for_preview rende
# visibili apposta righe e colonne nascoste ("l'anteprima MOSTRA TUTTO": la
# PII che si nasconde è proprio quella da far vedere), quindi C compare in
# anteprima per scelta. Che sia leggibile si verifica sotto, su column_values.
# il testo che SBORDA nella colonna accanto estende l'area di stampa: la
# pagina può mostrare anche B (vuota) — l'importante è che A ci sia e che
# un click su B degradi in "0 valori"
single = [e for e in layout.values() if e["sheet"] == "Solo Una"]
check(bool(single) and single[0]["cols"][0]["col"] == "A",
      "sintetico A: foglio a colonna singola -> prima lettera A")
vals_b, _ = xlsx.column_values(data, "Solo Una", "B")
check(vals_b == [], "sintetico A: colonna B (solo overflow di stampa) vuota")
check(not any(e["sheet"] == "Vuoto" for e in layout.values()),
      "sintetico A: foglio vuoto senza overlay")
# l'header si riconosce (euristica _detect_header: >=2 colonne testuali) e la
# colonna lo esclude dai valori; su fogli a colonna singola NON è
# riconoscibile per design e la prima cella resta tra i valori
vals, label = xlsx.column_values(data, "Dati Città Estero", "A")
check(vals == [f"r{r}c0" for r in range(8)] and label == "Campo 1",
      f"sintetico A: column_values esclude l'header ({len(vals)} vals, {label!r})")
vals, label = xlsx.column_values(data, "Solo Una", "A")
check(vals == ["Nominativo", "Mario Rossi", "Luca Bianchi"] and label is None,
      "sintetico A: colonna singola -> header non riconoscibile, incluso nei valori")
# colonna nascosta: i valori si estraggono comunque (lettera reale)
vals_c, _ = xlsx.column_values(data, "Dati Città Estero", "C")
check(len(vals_c) == 8, f"sintetico A: colonna nascosta C leggibile ({len(vals_c)})")

# b) senza header + duplicati + titolo con celle unite sopra l'header
data = make_xlsx(
    {"NoHeader": [[123, "alfa"], [456, "beta"], [789, "alfa"], [456, "gamma"]],
     "ConTitolo": [["Report Vendite 2026", None],
                   ["Cliente", "Importo"],
                   ["Rossi Mario", 1000],
                   ["Bianchi Luca", 2000]]},
    merges={"ConTitolo": ["A1:B1"]},
)
print("--- sintetico B")
layout, _ = preview_layout(data)
show(layout)
check(sane(layout), "sintetico B: box coerenti")
vals, label = xlsx.column_values(data, "NoHeader", "B")
check(vals == ["alfa", "beta", "gamma"] and label is None,
      f"sintetico B: no header -> tutte le righe, distinct ({vals}, {label!r})")
vals, label = xlsx.column_values(data, "ConTitolo", "A")
check("Report Vendite 2026" in vals or "Cliente" in vals or "Rossi Mario" in vals,
      f"sintetico B: colonna A con titolo unito leggibile ({vals})")
# errori attesi
try:
    xlsx.column_values(data, "NonEsiste", "A")
    check(False, "sintetico B: foglio inesistente -> XlsxError")
except xlsx.XlsxError:
    check(True, "sintetico B: foglio inesistente -> XlsxError")
try:
    xlsx.column_values(data, "NoHeader", "A1")
    check(False, "sintetico B: colonna invalida -> XlsxError")
except xlsx.XlsxError:
    check(True, "sintetico B: colonna invalida -> XlsxError")
vals, _ = xlsx.column_values(data, "NoHeader", "Z")
check(vals == [], "sintetico B: colonna vuota -> []")

# c) pageSetup proprio (portrait): il foglio largo si spezza su più pagine
#    orizzontali e ogni pagina ha il SUO intervallo di lettere
wide_rows = [[f"ColonnaMoltoLarga{c}" for c in range(12)]] + \
            [[f"valore lungo {r} colonna {c}" for c in range(12)] for r in range(6)]
data = make_xlsx({"Largo": wide_rows},
                 page_setup={"Largo": '<pageSetup orientation="portrait"/>'})
print("--- sintetico C (foglio spezzato su più pagine)")
layout, _ = preview_layout(data)
show(layout)
check(sane(layout), "sintetico C: box coerenti")
if len(layout) > 1:
    firsts = [e["cols"][0]["col"] for e in layout.values()]
    check(len(set(firsts)) > 1,
          f"sintetico C: pagine con intervalli di lettere diversi ({firsts})")
else:
    print("  (una sola pagina: LibreOffice ha compresso tutto, niente split)")

print()
# --------------------------------------------------------------------------- #
# 3) anonimizzazione manuale di colonna: DETERMINISTICA, senza modello
# --------------------------------------------------------------------------- #
print("=== Anonimizzazione di colonna (deterministica, senza modello) ===")
import re as _re
from app.engine.core import _norm

nomi = ["Mario Rossi", "Luca Bianchi", "Anna Verdi", "Paola Neri",
        "Giulio Ferrari", "Sara Russo", "Marco Gallo", "Elena Costa"]
citta = ["Roma", "Milano", "Torino", "Napoli", "Bari", "Genova", "Parma", "Lodi"]
data = make_xlsx({"Clienti": [["Cliente", "Città", "Importo"]] +
                  [[n, c, i * 100] for i, (n, c) in enumerate(zip(nomi, citta))]})

# a mappa vuota: TUTTI i distinct diventano [CUSTOM_n], sempre
mapping = {}
out = xlsx.anonymize_xlsx_column(data, "Clienti", "A", mapping)
print(f"  colonna A: tag={out['tag']} nuovi={len(out['new_placeholders'])} "
      f"esistenti={out['existing']} skipped={out['skipped']}")
check(out["n_values"] == len(nomi), "colonna A: tutti i distinct visti")
check(len(out["new_placeholders"]) == len(nomi),
      "colonna A: OGNI distinct mappato (deterministico)")
check(all(_re.fullmatch(r"\[CUSTOM_\d+\]", ph) for ph in out["new_placeholders"]),
      "colonna A: tutti i nuovi placeholder sono [CUSTOM_n]")
check(out["exact_new"] == set(out["new_placeholders"]),
      "colonna A: tutti i nuovi sono exact (cella intera)")

# seconda passata sulla stessa colonna: nulla di nuovo, tutto existing
out2 = xlsx.anonymize_xlsx_column(data, "Clienti", "A", mapping)
check(not out2["new_placeholders"] and out2["existing"] == len(nomi),
      "colonna A ripetuta: 0 nuovi, tutti già in mappa")

# colonna C (importi 0..700): "0" è troppo corto -> skipped, il resto CUSTOM
out_c = xlsx.anonymize_xlsx_column(data, "Clienti", "C", mapping)
print(f"  colonna C: nuovi={len(out_c['new_placeholders'])} skipped={out_c['skipped']}")
check(out_c["skipped"] == 1 and len(out_c["new_placeholders"]) == 7,
      "colonna C: numeri corti saltati, il resto mappato comunque")

# valore condiviso con un'altra colonna già mappata: riusa il placeholder
mapping2 = {"[FULLNAME_1]": "Mario Rossi"}
out_r = xlsx.anonymize_xlsx_column(data, "Clienti", "A", mapping2)
check(out_r["existing"] == 1 and len(out_r["new_placeholders"]) == len(nomi) - 1,
      "riuso: un valore già mappato (anche con altra label) non si duplica")
normed = [_norm(v) for v in mapping2.values()]
check(len(normed) == len(set(normed)), "mappa senza valori duplicati")
# i contatori CUSTOM proseguono quelli in mappa (stessa numerazione di
# anonymize-text): con [CUSTOM_3] preesistente si parte da _4
mapping3 = {"[CUSTOM_3]": "qualcosa di già mappato"}
out_n = xlsx.anonymize_xlsx_column(data, "Clienti", "B", mapping3)
check(min(int(_re.search(r"_(\d+)", ph).group(1))
          for ph in out_n["new_placeholders"]) == 4,
      "contatore CUSTOM prosegue dalla mappa esistente")

# ri-redazione con la mappa aggiornata + exact dei nuovi: nessun residuo
result = xlsx.rebuild_xlsx(data, mapping, exact_phs=set(mapping))
check(result["report"]["occurrences"] > 0, "rebuild: occorrenze redatte")
check(not result["report"]["residual"], "rebuild: nessun residuo")

print()
if "--model" not in sys.argv:
    print("(salto il test col modello: aggiungi --model per includerlo)")
else:
    # ----------------------------------------------------------------------- #
    # 4) integrazione col modello vero: upload classico, poi colonna a mano
    # ----------------------------------------------------------------------- #
    print("=== Colonna dopo upload col modello (riuso placeholder) ===")
    from app.config import MODEL_DIR
    from app.engine import PiiEngine

    engine = PiiEngine(MODEL_DIR)
    # upload classico (foglio piccolo: percorso non tabellare)
    res = xlsx.anonymize_xlsx(data, engine)
    mapping_m = dict(res["analysis"]["mapping"])
    n_before = len(mapping_m)
    print(f"  upload: {n_before} placeholder, by_label={res['analysis']['by_label']}")

    # colonna A: i nomi già visti dal modello riusano il loro placeholder
    # (FULLNAME o altro), SOLO i mancanti diventano CUSTOM
    out_m = xlsx.anonymize_xlsx_column(data, "Clienti", "A", mapping_m)
    print(f"  colonna A: nuovi={len(out_m['new_placeholders'])} "
          f"esistenti={out_m['existing']}")
    check(out_m["existing"] + len(out_m["new_placeholders"]) == len(nomi),
          "colonna A: copertura totale = existing + nuovi")
    normed = [_norm(v) for v in mapping_m.values()]
    check(len(normed) == len(set(normed)), "mappa modello+colonna senza duplicati")
    check(len(mapping_m) >= n_before, "la mappa non perde voci")

    result = xlsx.rebuild_xlsx(data, mapping_m, exact_phs=out_m["exact_new"])
    check(result["report"]["occurrences"] > 0, "rebuild: occorrenze redatte")
    check(not result["report"]["residual"], "rebuild: nessun residuo")

print()
print("FALLIMENTI:", len(FAILS))
for f in FAILS:
    print("  -", f)
sys.exit(1 if FAILS else 0)

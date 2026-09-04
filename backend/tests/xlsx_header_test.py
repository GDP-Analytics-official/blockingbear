"""Intestazioni di colonna dei fogli xlsx TABELLARI.

La redazione è cieca alla posizione: cerca i valori della mappa ovunque, e
non sa che una cella è un nome di colonna. Se il modello tagga un'etichetta,
l'unica cella colpita è proprio l'intestazione — la tabella diventa
illeggibile senza che nessun dato sia stato protetto. Caso reale misurato su
assets/listino_marchi.xlsx: 'Marchio' -> [FULLNAME_1], 1 occorrenza.

Due difese, verificate qui:
  1. la riga header NUDA non si manda più al modello (le etichette restano
     visibili come prefisso "Etichetta: valore" del campione, dove sono
     contesto e non candidati);
  2. se un'etichetta finisce in mappa da un'altra strada, si scarta PRIMA
     della redazione — ma solo se quel valore non compare in nessun dato
     (xlsx._header_only_phs). Scartarlo dopo, ripristinando la cella,
     sarebbe un residuo che fa dichiarare il file non protetto (422).

Il modello vero non serve: il rilevatore è un doppio deterministico. Solo la
parte 3 usa il file d'esempio.

    python backend/tests/xlsx_header_test.py
"""
import io
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
_tmp = tempfile.TemporaryDirectory(prefix="blockingbear-xlsx-header-")
os.environ["BLOCKINGBEAR_DATA_DIR"] = _tmp.name
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
sys.path.insert(0, str(HERE))

from app import db, settings_store                              # noqa: E402
from app.chat_anonymization import (ConversationEngine,          # noqa: E402
                                    ReplacementMapping,
                                    conversation_mapping)
from app.db import Conversation, ConversationEntity, init_db     # noqa: E402
from app.engine import xlsx                                      # noqa: E402
from app.engine.xlsx_table import PlaceholderAllocator           # noqa: E402

REAL = Path(__file__).resolve().parent / "assets" / "listino_marchi.xlsx"

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


# --------------------------------------------------------------------------- #
# Workbook sintetico minimo (niente openpyxl in ambiente): celle inlineStr
# --------------------------------------------------------------------------- #
_CT = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WB = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Dati" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_WB_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""


def make_xlsx(rows):
    """[[testo, ...], ...] -> bytes di un .xlsx con un solo foglio 'Dati'."""
    body = []
    for ri, cells in enumerate(rows, start=1):
        cs = []
        for ci, txt in enumerate(cells):
            ref = f"{chr(ord('A') + ci)}{ri}"
            cs.append(f'<c r="{ref}" t="inlineStr"><is><t>{txt}</t></is></c>')
        body.append(f'<row r="{ri}">{"".join(cs)}</row>')
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main"><sheetData>'
             + "".join(body) + "</sheetData></worksheet>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _WB)
        z.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def sheet_rows(xlsx_bytes):
    """Righe non vuote del foglio, come testo per riga."""
    import xml.etree.ElementTree as ET
    zf, names = xlsx._open(xlsx_bytes)
    sst = xlsx._shared_strings(zf, names)
    part, _sn = xlsx._sheet_parts(zf, names)[0]
    root = ET.fromstring(zf.read(part))
    return [" | ".join(t for _c, t, _s in cells)
            for _ri, cells in xlsx._sheet_rows_idx(root, sst)]


class StubEngine:
    """Rilevatore deterministico: le coppie (label, valore) passate al
    costruttore, cercate alla lettera nel testo. Registra i testi ricevuti."""

    def __init__(self, entries):
        self.entries = entries
        self.texts = []

    def analyze(self, text, excluded=None, custom_terms=None, ctl=None):
        self.texts.append(text)
        entities, mapping, counters, by_value = [], {}, {}, {}
        for label, value in self.entries:
            for m in re.finditer(re.escape(value), text):
                ph = by_value.get(value)
                if ph is None:
                    counters[label] = counters.get(label, 0) + 1
                    ph = f"[{label}_{counters[label]}]"
                    by_value[value] = ph
                    mapping[ph] = value
                entities.append({"label": label, "value": value, "ph": ph,
                                 "start": m.start(), "end": m.end(),
                                 "source": "test", "validated": True})
        entities.sort(key=lambda e: e["start"])
        return {"entities": entities, "anonymized_text": text,
                "mapping": mapping, "n_entities": len(entities),
                "n_unique": len(mapping), "by_label": {}}


SessionLocal = init_db()


def set_min_rows(n):
    with SessionLocal() as s:
        settings_store.set_values(s, {"xlsx_table_min_rows": n})


# --------------------------------------------------------------------------- #
# 1) La funzione pura: quando un'etichetta si scarta e quando no
# --------------------------------------------------------------------------- #
def part_pura():
    print("\n== 1) _header_only_phs ==")
    labels = {xlsx._norm_value("Marchio"), xlsx._norm_value("Rossi")}

    out = xlsx._header_only_phs({"[FULLNAME_1]": "Marchio"}, labels, [], [])
    check("etichetta che non compare tra i dati -> scartata",
          out == {"[FULLNAME_1]": "Marchio"}, str(out))

    out = xlsx._header_only_phs({"[FULLNAME_1]": "Marchio"}, labels,
                                ["Vistaclear", "Marchio"], [])
    check("etichetta che È anche un dato -> si tiene (scoprirebbe le celle)",
          out == {}, str(out))

    out = xlsx._header_only_phs({"[FULLNAME_1]": "Rossi"}, labels,
                                ["Mario Rossi"], [])
    check("collisione a SOTTOSTRINGA dentro un valore più lungo",
          out == {}, str(out))

    out = xlsx._header_only_phs({"[FULLNAME_1]": "Marchio"}, labels, [],
                                ["nota: il Marchio è registrato dal 2019"])
    check("collisione in un testo (riga-titolo, foglio non tabellare, commento)",
          out == {}, str(out))

    out = xlsx._header_only_phs({"[ORG_1]": "Vistaclear"}, labels, [], [])
    check("un valore che non è un'etichetta non si tocca mai",
          out == {}, str(out))

    # un placeholder del registro può portare più superfici: scartarlo le
    # porta via tutte, quindi basta che UNA compaia nei dati perché resti
    rm = ReplacementMapping({"[FULLNAME_1]": "Marchio"},
                            [("[FULLNAME_1]", "Marchio Rossi")])
    check("una superficie del placeholder presente nei dati lo salva",
          xlsx._header_only_phs(rm, labels, ["Marchio Rossi"], []) == {},
          str(xlsx._header_only_phs(rm, labels, ["Marchio Rossi"], [])))
    check("se nessuna superficie è nei dati si scarta lo stesso",
          xlsx._header_only_phs(rm, labels, ["Vistaclear"], [])
          == {"[FULLNAME_1]": "Marchio"})

    alloc = PlaceholderAllocator({"[FULLNAME_1]": "Marchio"})
    check("forget() restituisce il valore", alloc.forget("[FULLNAME_1]") == "Marchio")
    check("forget() lo toglie dalla mappa", alloc.mapping == {}, str(alloc.mapping))
    ph, created = alloc.get("FULLNAME", "Mario Rossi")
    check("il numero scartato NON si riusa (registro: sarebbe ambiguo)",
          (ph, created) == ("[FULLNAME_2]", True), ph)
    check("forget() di un placeholder assente è innocuo",
          alloc.forget("[NOPE_9]") is None)

    # nei progetti/chat la mappa è una ReplacementMapping: dizionario
    # canonico PIÙ le superfici alias, ed è su quelle che i redattori
    # iterano. Toglierla dal solo dizionario lascerebbe la superficie viva.
    rm = ReplacementMapping({"[FULLNAME_1]": "Marchio", "[ORG_1]": "Vistaclear"},
                            [("[FULLNAME_1]", "MARCHIO")])
    check("la mappa del registro espone anche gli alias",
          ("[FULLNAME_1]", "MARCHIO") in rm.items(), str(rm.items()))
    PlaceholderAllocator(rm).forget("[FULLNAME_1]")
    check("forget() toglie anche le SUPERFICI (o si redigerebbe lo stesso)",
          [p for p, _v in rm.items()] == ["[ORG_1]"], str(rm.items()))
    check("le altre voci restano intatte", rm.get("[ORG_1]") == "Vistaclear")


# --------------------------------------------------------------------------- #
# 2) End-to-end su workbook sintetico
# --------------------------------------------------------------------------- #
def part_sintetico():
    print("\n== 2) workbook sintetico ==")
    set_min_rows(5)

    # header 'Marchio' + una colonna con un dato vero da anonimizzare
    rows = [["Reparto", "Marchio", "Note"]]
    for i in range(12):
        rows.append([f"Rossi Spa", f"Vistaclear {i}", f"riga {i}"])
    data = make_xlsx(rows)

    eng = StubEngine([("FULLNAME", "Marchio"), ("ORG", "Rossi Spa")])
    res = xlsx.anonymize_xlsx(data, eng, make_preview=False)
    rep = res["report"]
    kept = rep["header_kept"]
    out_rows = sheet_rows(res["file"])

    check("l'etichetta finisce in header_kept",
          [k["value"] for k in kept] == ["Marchio"], str(kept))
    check("la riga header esce INTATTA",
          out_rows[0] == "Reparto | Marchio | Note", out_rows[0])
    check("il dato vero resta anonimizzato",
          out_rows[1].startswith("[ORG_1] |"), out_rows[1])
    check("nessun residuo (il valore non è più in mappa)",
          rep["residual"] == [], str(rep["residual"]))
    check("il placeholder scartato non è nella mappa consegnata",
          "[FULLNAME_1]" not in res["analysis"]["mapping"],
          str(sorted(res["analysis"]["mapping"])))
    check("né tra le entità consegnate a valle",
          all(e["value"] != "Marchio" for e in res["analysis"]["entities"]))

    # la riga header nuda non arriva al modello, le coppie arricchite sì
    first = eng.texts[0]
    check("la riga header nuda non si manda al modello",
          "Reparto | Marchio | Note" not in first)
    check("le etichette restano come contesto delle coppie del campione",
          "Marchio: Vistaclear 0" in first)

    # collisione: l'etichetta è anche un dato -> si anonimizza comunque
    rows = [["Reparto", "Milano", "Note"]]
    for i in range(12):
        rows.append([f"sede {i}", f"valore {i}", "Milano" if i == 3 else "-"])
    eng = StubEngine([("LOC", "Milano")])
    res = xlsx.anonymize_xlsx(make_xlsx(rows), eng, make_preview=False)
    out_rows = sheet_rows(res["file"])
    check("con collisione l'etichetta NON si protegge",
          res["report"]["header_kept"] == [], str(res["report"]["header_kept"]))
    check("header redatto comunque", "[LOC_1]" in out_rows[0], out_rows[0])
    check("e la cella dati che collideva è coperta",
          "[LOC_1]" in out_rows[4], out_rows[4])

    # percorso tabellare spento: comportamento invariato
    set_min_rows(0)
    rows = [["Reparto", "Marchio", "Note"]]
    for i in range(12):
        rows.append([f"sede {i}", f"Vistaclear {i}", f"riga {i}"])
    eng = StubEngine([("FULLNAME", "Marchio")])
    res = xlsx.anonymize_xlsx(make_xlsx(rows), eng, make_preview=False)
    out_rows = sheet_rows(res["file"])
    check("percorso tabellare spento: niente filtro",
          res["report"]["header_kept"] == [])
    check("percorso tabellare spento: header redatto come sempre",
          "[FULLNAME_1]" in out_rows[0], out_rows[0])
    set_min_rows(5)


# --------------------------------------------------------------------------- #
# 3) L'asset versionato (senza modello: stub con l'esito misurato)
# --------------------------------------------------------------------------- #
def part_file_vero():
    print(f"\n== 3) assets/{REAL.name} ==")
    if not REAL.is_file():
        check("file d'esempio presente", False, str(REAL))
        return
    data = REAL.read_bytes()
    # sulla riga di intestazione nuda il modello legge 'Marchio' come un
    # cognome; 'Vistaclear' è il dato che sta nella colonna sotto
    eng = StubEngine([("FULLNAME", "Marchio"), ("ORG", "Vistaclear")])
    res = xlsx.anonymize_xlsx(data, eng, make_preview=False)
    rep = res["report"]
    out_rows = sheet_rows(res["file"])
    check("header intatto",
          out_rows[0] == "Codice a barre | SKU | Marchio | Modello", out_rows[0])
    check("'Marchio' protetto",
          [k["value"] for k in rep["header_kept"]] == ["Marchio"],
          str(rep["header_kept"]))
    check("il marchio VERO nelle celle sotto è coperto",
          "[ORG_1]" in out_rows[1], out_rows[1][:80])
    check("nessun residuo", rep["residual"] == [], str(rep["residual"]))
    check("occorrenze > 0", rep["occurrences"] > 0, str(rep["occurrences"]))


# --------------------------------------------------------------------------- #
# 4) Registro di progetto/chat: l'etichetta non deve restare una superficie nota
# --------------------------------------------------------------------------- #
def part_registro():
    print("\n== 4) registro (ConversationEngine.keep_in_clear) ==")
    with SessionLocal() as session:
        conv = Conversation(owner_id=1)
        session.add(conv)
        session.commit()
        cid = conv.id

    with SessionLocal() as session:
        conv = session.get(Conversation, cid)
        eng = ConversationEngine(StubEngine([("FULLNAME", "Marchio")]),
                                 session, conv)
        eng.analyze("colonna Marchio del listino")
        ph = next(iter(eng._created))
        check("l'entità è nata da questa analisi", ph == "[FULLNAME_1]", ph)
        check("keep_in_clear ne marca una", eng.keep_in_clear([ph]) == 1)
        entity = session.query(ConversationEntity).filter_by(
            conv_id=cid, placeholder=ph).one()
        check("excluded scritto sul registro", entity.excluded == 1)
        mapping = conversation_mapping(session, cid, include_aliases=True)
        # l'esclusione morde sulle SOSTITUZIONI (ciò che i redattori
        # iterano con .items()), non sul dizionario canonico
        pairs = mapping.for_text("il Marchio resta").items()
        check("la superficie non si sostituisce più",
              all(p != ph for p, _v in pairs), str(pairs))
        check("ma resta nel dizionario canonico (decodifica del passato)",
              conversation_mapping(session, cid).get(ph) == "Marchio")
        session.commit()

    with SessionLocal() as session:
        conv = session.get(Conversation, cid)
        eng = ConversationEngine(StubEngine([("ORG", "Rossi Spa")]),
                                 session, conv)
        eng.analyze("cliente Rossi Spa")
        check("keep_in_clear ignora le entità di analisi PRECEDENTI "
              "(decisione presa altrove)", eng.keep_in_clear(["[FULLNAME_1]"]) == 0)
        session.commit()

    # end-to-end col registro vero: è il percorso di progetti e chat, dove la
    # mappa è una ReplacementMapping e il file passa dal registro
    set_min_rows(5)
    rows = [["Reparto", "Marchio", "Note"]]
    for i in range(12):
        rows.append(["Rossi Spa", f"Vistaclear {i}", f"riga {i}"])
    with SessionLocal() as session:
        conv = Conversation(owner_id=1)
        session.add(conv)
        session.commit()
        eng = ConversationEngine(
            StubEngine([("FULLNAME", "Marchio"), ("ORG", "Rossi Spa")]),
            session, conv)
        res = xlsx.anonymize_xlsx(make_xlsx(rows), eng, make_preview=False)
        session.commit()
        out_rows = sheet_rows(res["file"])
        check("e2e registro: header intatto",
              out_rows[0] == "Reparto | Marchio | Note", out_rows[0])
        check("e2e registro: il dato vero resta coperto",
              out_rows[1].startswith("[ORG_1] |"), out_rows[1])
        check("e2e registro: nessun residuo",
              res["report"]["residual"] == [], str(res["report"]["residual"]))
        rowset = {e.placeholder: e.excluded for e in
                  session.query(ConversationEntity).filter_by(conv_id=conv.id)}
        check("e2e registro: l'etichetta è marcata in chiaro",
              rowset.get("[FULLNAME_1]") == 1, str(rowset))
        check("e2e registro: il dato vero non è stato toccato",
              rowset.get("[ORG_1]") == 0, str(rowset))


def main():
    which = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if which in ("", "pura"):
        part_pura()
    if which in ("", "sintetico"):
        part_sintetico()
    if which in ("", "reale"):
        part_file_vero()
    if which in ("", "registro"):
        part_registro()
    print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
    db._engine.dispose()
    _tmp.cleanup()
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

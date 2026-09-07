"""
Redazione dei fogli di calcolo Excel (.xlsx): il fratello SpreadsheetML di
docx.py e pptx.py, con una geometria diversa: il testo delle celle NON sta
nei fogli, sta in xl/sharedStrings.xml (tabella di stringhe UNICHE condivise);
i fogli contengono numeri e indici in quella tabella. Questo aiuta coi file
enormi: si analizza e si redige il testo unico, non le 500.000 celle che lo
riusano, e una sostituzione nella stringa condivisa corregge insieme tutte le
celle che la referenziano.

Il contratto comune ai redattori (matching ancorato, chiavi del report,
verifica dei residui, convenzione dei colori) sta in engine/__init__.py. Qui
sotto solo ciò che è specifico dei fogli di calcolo.

DIFFERENZE CHIAVE RISPETTO A WORD/POWERPOINT
  - EVIDENZIAZIONE: Excel non ha l'evidenziatore sul testo; l'equivalente è lo
    SFONDO giallo della cella (patternFill solid in styles.xml). Per ogni stile
    originale coinvolto si crea una variante identica + fill giallo, così il
    resto della formattazione si conserva.
  - LINEARIZZAZIONE PER RIGA: per l'analisi le celle di una riga vengono unite
    con " | " (separatore che _value_pattern NON attraversa: un match non può
    scavalcare due celle) e, se la prima riga sembra un header (tutte stringhe,
    uniche, corte), le righe dati diventano coppie "Intestazione: valore" ->
    contesto vero per il modello NER anche su celle ambigue. L'arricchimento
    riguarda SOLO il testo passato al modello: la redazione è guidata dalla
    mappa dei valori e non dipende da questi separatori.
  - INTESTAZIONI: la redazione è cieca alla posizione (cerca i valori della
    mappa ovunque), quindi un'etichetta di colonna taggata per errore rende
    illeggibile la tabella senza proteggere nulla. Sui fogli tabellari la riga
    header non viene mandata al modello e le etichette che finiscono comunque
    in mappa si scartano se non compaiono in nessun dato (_header_only_phs).
  - NUMERI: un telefono può essere una cella NUMERICA (fuori da sharedStrings);
    il valore grezzo viene analizzato e, se mappato, la cella diventa testo
    (inlineStr) col placeholder. Limite dichiarato: le date salvate come
    seriale (27598 mostrato "23/07/1975") sono viste dal modello come numeri.
  - FORMULE: se una formula contiene un valore mappato nel testo o nel
    risultato in cache, sostituire la cache non basta (al ricalcolo la PII
    tornerebbe): la formula viene CONGELATA al valore redatto. Le formule
    condivise (t="shared") si congelano in gruppo, e se si congela qualcosa
    xl/calcChain.xml viene eliminato (è una cache ricostruibile; lasciarlo
    incoerente fa scattare il ripristino di Excel).

COPIE NASCOSTE DEI DATI, ripulite (posti in cui la PII sopravviverebbe)
  - cache dei PIVOT (pivotCacheDefinition/Records: copia INTEGRALE dei dati
    sorgente, valori negli ATTRIBUTI) -> redatte + refreshOnLoad="1";
  - cache dei GRAFICI (c:v / cx:pt) e titoli rich text (a:t);
  - commenti classici (testo + autori svuotati) e THREADED (testo +
    displayName/userId in xl/persons svuotati);
  - caselle di testo (xl/drawings: DrawingML, riuso del redattore pptx con
    evidenziazione a:highlight vera);
  - intestazioni/piè di pagina di stampa, valori dei FILTRI automatici,
    hyperlink (display/tooltip + target nei .rels), validazioni dati,
    formattazione condizionale, nomi colonna delle tabelle, connessioni;
  - docProps/core.xml e app.xml (TitlesOfParts = NOMI dei fogli, Company...).

ANTEPRIMA TRONCATA
La conversione LibreOffice di un foglio da 500k righe sarebbe un PDF da
migliaia di pagine, quindi l'anteprima si genera da una COPIA con le prime N
righe per foglio (parametro `xlsx_preview_rows` del pannello admin,
report["preview_truncated"]=True). Redazione e verifica dei residui lavorano
SEMPRE sul file completo. La stessa copia forza griglia, intestazioni
A/B/C-1/2/3 e orientamento orizzontale (printOptions/pageSetup): la conversione
è una "stampa", e senza impostazioni di stampa il PDF sembrerebbe un documento
Word invece di un foglio di calcolo.

LIMITI NOTI E DELIBERATI
Worksheet and defined names are redacted together with their references.
Dynamic INDIRECT/EVALUATE references that cannot be safely rewritten reject
the operation. Hidden defined-name templates retain only protected text for
restoration; the original names are never stored in the protected package.
Il testo dentro le immagini è
coperto solo con ocr=True (vedi anonymize_xlsx), come per gli altri formati
OOXML. I .xls binari e i .xlsm con macro si convertono in .xlsx all'ingresso
(le macro non sopravvivono: possono contenere PII e riscriverle non è
affidabile).

Solo stdlib; le trappole di riserializzazione OOXML sono quelle risolte in
docx.py (_serialize_part / _part_namespaces, vedi engine/__init__.py) e qui si
riusano, insieme al redattore DrawingML di pptx.py per caselle di testo e
titoli dei grafici.
"""

import copy
import io
import re
import zipfile
import xml.etree.ElementTree as ET

from .. import settings_store
from . import image_ocr, xlsx_table
from .core import _norm as _norm_value
from .docx import (_alt_text_surfaces, _meta_surfaces, _part_namespaces,
                   _RelsValues, _scrub_alt_text, _scrub_app_props,
                   _scrub_core_props, _scrub_rels, _serialize_part, _set_text)
from .pptx import _redact_tree as _redact_drawing_tree
from .pdf_export import _too_noisy, _value_pattern
from .text_patterns import contains_literal
from .detectors import EXACT_SPAN_LABELS
from .xlsx_matching import CellValues, ValueIndex, patterns
from .xlsx_names import redact_names, workbook_surfaces, reference_surfaces

X_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
X = "{%s}" % X_NS
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
# NIENTE ET.register_namespace("") a livello di modulo: il registro tiene UN
# solo namespace per prefisso e il default è conteso tra .rels, content types
# e le part spreadsheet -> la registrazione avviene per-part in _serialize_part
# (il default è catturato da _part_namespaces) o just-in-time qui sotto

# Guardrail sull'ANALISI dei fogli enormi: mmBERT su CPU d'ufficio fa ~0.75s
# a chunk (misurato: 306s per 2.000 righe = 380 chunk; batch_size non aiuta,
# torch satura già i core) -> 200k righe sarebbero ~8 ORE di modello. Con
# l'elaborazione in background (jobs.py) l'upload non resta più appeso, ma
# un foglio così terrebbe comunque un worker occupato per ore: il tetto è
# il parametro `xlsx_max_chunks` del pannello admin (settings_store.py),
# passato dal worker come `max_chunks` (0/None = nessun limite).
_SECONDS_PER_CHUNK = 0.75

_SHEET_RE = re.compile(r"^xl/worksheets/(?!_rels)[^/]+\.xml$")
_COMMENTS_RE = re.compile(r"^xl/comments\d*\.xml$")
_THREADED_RE = re.compile(r"^xl/threadedComments/[^/]+\.xml$")
_PERSONS_RE = re.compile(r"^xl/persons/[^/]+\.xml$")
_DRAWING_RE = re.compile(r"^xl/drawings/drawing\d+\.xml$")
_CHART_RE = re.compile(r"^xl/charts/chart(?:Ex)?\d+\.xml$")
# part in cui i dati vivono negli ATTRIBUTI (cache pivot = copia integrale dei
# dati; nomi colonna delle tabelle; stringhe di connessione)
_ATTR_PART_RE = re.compile(
    r"^xl/(tables/table\d+|pivotCache/pivotCache(?:Definition|Records)\d+"
    r"|pivotTables/pivotTable\d+|connections)\.xml$")

# attributi dei FOGLI che portano dati utente (allowlist: r/ref/s/t restano)
_SHEET_DATA_ATTRS = {"display", "tooltip", "location", "val", "text"}
# nodi-testo "liberi" dei fogli: intestazioni di stampa e formule di
# validazione/formattazione condizionale (possono contenere liste di valori)
_SHEET_TEXT_TAGS = {X + t for t in (
    "oddHeader", "oddFooter", "evenHeader", "evenFooter",
    "firstHeader", "firstFooter", "formula1", "formula2", "formula")}


class XlsxError(ValueError):
    """Errore d'uso (file non valido, niente testo, dizionario vuoto...)."""


# --------------------------------------------------------------------------- #
# Caricamento
# --------------------------------------------------------------------------- #
def _open(xlsx_bytes):
    try:
        zf = zipfile.ZipFile(io.BytesIO(xlsx_bytes))
        names = set(zf.namelist())
    except zipfile.BadZipFile:
        raise XlsxError("File .xlsx non valido o danneggiato.")
    if "xl/workbook.xml" not in names:
        raise XlsxError("Non sembra un foglio di calcolo Excel (.xlsx).")
    return zf, names


def _sheet_parts(zf, names):
    """[(nome_part, nome_foglio), ...] nell'ORDINE del workbook (la numerazione
    dei placeholder segue l'ordine in cui l'utente vede i fogli)."""
    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
    except ET.ParseError:
        raise XlsxError("XML non valido dentro l'xlsx (xl/workbook.xml).")
    id2target = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        id2target = {r.get("Id"): r.get("Target", "") for r in rels}
    out = []
    for sh in wb.iter():
        if not sh.tag.endswith("}sheet"):
            continue
        target = id2target.get(sh.get(R_ID), "")
        if "worksheets/" not in target:          # chartsheet: niente celle
            continue
        part = target.lstrip("/")
        if not part.startswith("xl/"):
            part = "xl/" + part
        if part in names:
            out.append((part, sh.get("name") or ""))
    # fallback per package anomali senza rels risolvibili
    if not out:
        out = [(n, "") for n in sorted(names) if _SHEET_RE.match(n)]
    return out


def _si_text(si):
    """Testo piano di un elemento <si>/<is> (runs concatenati, fonetica esclusa)."""
    parts = []

    def rec(el):
        if el.tag == X + "rPh":
            return
        if el.tag == X + "t":
            if el.text:
                parts.append(el.text)
            return
        for ch in el:
            rec(ch)

    rec(si)
    return "".join(parts)


def _shared_strings(zf, names):
    if "xl/sharedStrings.xml" not in names:
        return []
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except ET.ParseError:
        raise XlsxError("XML non valido dentro l'xlsx (sharedStrings).")
    return [_si_text(si) for si in root]


def _cell_text(c, sst):
    """Testo visibile di una cella: stringa condivisa, inline, risultato di
    formula o valore grezzo (i numeri restano nella loro forma salvata)."""
    t = c.get("t", "n")
    if t == "inlineStr":
        is_el = c.find(X + "is")
        return _si_text(is_el) if is_el is not None else ""
    v = c.find(X + "v")
    if v is None or not v.text:
        return ""
    if t == "s":
        try:
            return sst[int(v.text)]
        except (ValueError, IndexError):
            return ""
    if t in ("b", "e"):
        return ""
    return v.text


_COL_RE = re.compile(r"^([A-Z]+)\d+$")


def _letter_idx(letters):
    """"A" -> 0, "B" -> 1, ..., "AA" -> 26 (inverso della numerazione Excel)."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _col_idx(c, fallback):
    m = _COL_RE.match(c.get("r") or "")
    if not m:
        return fallback
    return _letter_idx(m.group(1))


# --------------------------------------------------------------------------- #
# Estrazione + arricchimento per il modello
# --------------------------------------------------------------------------- #
def _detect_header(rows):
    """{col: etichetta} se la prima riga non vuota sembra un'intestazione:
    tutte celle TESTUALI, corte, senza duplicati, con almeno 2 colonne e
    almeno 2 righe di dati sotto. Sbagliare costa poco: si ripete come
    "contesto" una riga vera del documento, già analizzata comunque."""
    if len(rows) < 3 or len(rows[0]) < 2:
        return None
    hdr = rows[0]
    vals = []
    for _col, txt, is_text in hdr:
        v = txt.strip()
        if not is_text or not v or len(v) > 60 or "\n" in v:
            return None
        vals.append(v)
    low = [v.casefold() for v in vals]
    if len(set(low)) != len(low):
        return None
    return {col: txt.strip() for col, txt, _ in hdr}


def _sheet_rows_idx(root, sst):
    """[(riga_excel_1based, [(col, testo, è_testo), ...]), ...] delle righe
    non vuote del foglio. Il numero di riga serve al percorso tabellare
    (contiguità del blocco dati, posizione delle celle unite)."""
    rows = []
    sheet_data = root.find(X + "sheetData")
    if sheet_data is None:
        return rows
    prev = 0
    for row in sheet_data:
        try:
            ri = int(row.get("r") or prev + 1)
        except ValueError:
            ri = prev + 1
        prev = ri
        cells = []
        for c in row:
            if c.tag != X + "c":
                continue
            txt = _cell_text(c, sst)
            if txt and txt.strip():
                cells.append((_col_idx(c, len(cells)),
                              txt, c.get("t") in ("s", "inlineStr", "str")))
        if cells:
            rows.append((ri, cells))
    return rows


def _sheet_rows(root, sst):
    """[[(col, testo, è_testo), ...], ...] delle righe non vuote del foglio."""
    return [cells for _ri, cells in _sheet_rows_idx(root, sst)]


_MERGE_REF_RE = re.compile(r"^([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$")


def _merged_ranges(root):
    """Celle unite del foglio: [(r1, c1, r2, c2), ...] con righe 1-based e
    colonne 0-based, estremi inclusi (per xlsx_table.detect_table)."""
    mc = root.find(X + "mergeCells")
    if mc is None:
        return []

    out = []
    for m in mc:
        ref = (m.get("ref") or "").upper()
        mt = _MERGE_REF_RE.match(ref)
        if not mt:
            continue
        c1, r1 = _letter_idx(mt.group(1)), int(mt.group(2))
        c2 = _letter_idx(mt.group(3)) if mt.group(3) else c1
        r2 = int(mt.group(4)) if mt.group(4) else r1
        out.append((min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)))
    return out


def _sheet_text(root, sst, enriched=False, full=False):
    """Linearizzazione di un foglio: una riga di testo per riga di celle,
    celle unite da " | " (che _value_pattern non attraversa). Con enriched=True
    e header riconosciuto, le righe dati diventano coppie "Etichetta: valore".
    Con full=True include anche gli attributi-dato (filtri, hyperlink, ...)."""
    rows = _sheet_rows(root, sst)
    header = _detect_header(rows) if enriched else None
    lines = []
    for i, cells in enumerate(rows):
        if header and i > 0:
            parts = []
            for col, txt, _ in cells:
                lab = header.get(col, "")
                parts.append(f"{lab}: {txt}" if lab else txt)
            lines.append(" | ".join(parts))
        else:
            lines.append(" | ".join(txt for _, txt, _ in cells))
    lines.extend(_sheet_extra_lines(root, full=full))
    return "\n".join(lines)


def _sheet_extra_lines(root, full=False):
    """Testi del foglio FUORI dalle celle: intestazioni di stampa, formule di
    validazione/formattazione condizionale e (full=True) gli attributi-dato."""
    lines, attrs = [], []
    for el in root.iter():
        if el.tag in _SHEET_TEXT_TAGS and el.text and el.text.strip():
            lines.append(el.text)
        elif el.tag == X + "f" and el.text:
            if full:
                lines.append(el.text)
            else:
                # Formula literals can be the only copy of a personal value.
                lines.extend(m[1:-1].replace('""', '"') for m in
                             re.findall(r'"(?:[^"]|"")*"', el.text))
        if full:
            attrs.extend(val for attr in _SHEET_DATA_ATTRS
                         if (val := el.get(attr)) and val.strip())
    if attrs:
        # " | " e non "\n": la tolleranza di sillabazione di _value_pattern
        # (cifra-\n-cifra) farebbe match FANTASMA attraverso valori distinti
        lines.append(" | ".join(attrs))
    return lines


def _plain_texts(root, tags_localname):
    """Testi dei nodi con local-name in `tags_localname` (namespace-agnostico:
    i threadedComments usano un namespace Microsoft 2018 non standard)."""
    out = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] in tags_localname and el.text and el.text.strip():
            out.append(el.text)
    return out


def _drawing_text(root):
    """Testo DrawingML (a:t) di drawing e grafici, un run per riga basta."""
    A_T = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
    return [el.text for el in root.iter(A_T) if el.text and el.text.strip()]


def extract_text(xlsx_bytes, full=False, enriched=False):
    """Testo del foglio di calcolo (celle per riga + commenti + caselle di
    testo + grafici + intestazioni di stampa).

    enriched=True: arricchimento per l'ANALISI del modello (coppie
    "Intestazione: valore" quando la prima riga è un header). full=True:
    include anche ciò che vive negli attributi (filtri, hyperlink, cache
    pivot, nomi colonna, autori dei commenti) — serve per cercare un valore
    OVUNQUE possa essere redatto (residual check, selezione manuale), non per
    l'analisi. XlsxError se non c'è testo."""
    zf, names = _open(xlsx_bytes)
    sst = _shared_strings(zf, names)
    texts = []
    for part, _sheet_name in _sheet_parts(zf, names):
        root = ET.fromstring(zf.read(part))
        t = _sheet_text(root, sst, enriched=enriched, full=full)
        if t.strip():
            texts.append(t)
    texts.extend(_aux_texts(zf, names, full=full))
    text = "\n".join(texts)
    if not text.strip():
        raise XlsxError("Il file Excel non contiene testo.")
    return text


def _aux_texts(zf, names, full=False):
    """Testi del workbook FUORI dai fogli: commenti (classici e threaded),
    caselle di testo, grafici e, con full=True, autori/attributi (persons,
    cache pivot, tabelle, connessioni)."""
    texts = workbook_surfaces(zf.read("xl/workbook.xml"), full=full)
    # Reference-bearing parts also include external link caches and chart
    # formulas. Unsupported references remain visible to the final check.
    if full:
        for part in sorted(names):
            if part.startswith("xl/") and part.endswith(".xml") and part != "xl/workbook.xml":
                texts.extend(reference_surfaces(ET.fromstring(zf.read(part))))
    for name in sorted(n for n in names if _COMMENTS_RE.match(n)):
        root = ET.fromstring(zf.read(name))
        tags = {"t", "author"} if full else {"t"}
        texts.extend(_plain_texts(root, tags))
    for name in sorted(n for n in names if _THREADED_RE.match(n)):
        texts.extend(_plain_texts(ET.fromstring(zf.read(name)), {"text"}))
    for name in sorted(n for n in names if _DRAWING_RE.match(n)):
        texts.extend(_drawing_text(ET.fromstring(zf.read(name))))
    for name in sorted(n for n in names if _CHART_RE.match(n)):
        root = ET.fromstring(zf.read(name))
        texts.extend(_drawing_text(root))
        vals = _plain_texts(root, {"v", "pt"})
        if vals:
            texts.append(" | ".join(vals))   # vedi nota su " | " in _sheet_text
    if full:
        for name in sorted(n for n in names if _PERSONS_RE.match(n)):
            root = ET.fromstring(zf.read(name))
            vals = [el.get(attr) for el in root.iter()
                    for attr in ("displayName", "userId") if el.get(attr)]
            if vals:
                texts.append(" | ".join(vals))
        for name in sorted(n for n in names if _ATTR_PART_RE.match(n)):
            root = ET.fromstring(zf.read(name))
            vals = [v for el in root.iter()
                    for v in el.attrib.values() if v and v.strip()]
            if vals:
                texts.append(" | ".join(vals))
    return texts


# --------------------------------------------------------------------------- #
# Redazione: item di testo, attributi, celle
# --------------------------------------------------------------------------- #
def _redact_item(elem, usable, text_tags, skip_tags=(), exact=None):
    """Redige UN item logico (un <si>, un <is>, il testo di un commento, un
    nodo <v>...): i suoi nodi-testo vengono linearizzati insieme (un valore
    può attraversare i run interni ma MAI due item diversi) e il placeholder
    va inline, senza run nuovi: in Excel l'evidenza è lo sfondo della cella.
    Ritorna {placeholder: n_match}.

    `exact` = {valore_normalizzato: placeholder} dei valori "da colonna" del
    percorso tabellare: item il cui testo COINCIDE con un valore mappato ->
    sostituzione O(1), senza passare dalle migliaia di regex che quella mappa
    comporterebbe (un unique per riga = pattern per riga: il prodotto
    pattern x sharedStrings sarebbe ingestibile)."""
    chunks, segs, pos = [], [], 0

    def rec(el):
        nonlocal pos
        if el.tag in skip_tags:
            return
        if el.tag in text_tags:
            s = el.text or ""
            if s:
                segs.append((pos, pos + len(s), el))
                chunks.append(s)
                pos += len(s)
            return
        for ch in el:
            rec(ch)

    rec(elem)
    text = "".join(chunks)
    if not text.strip():
        return {}
    if exact:
        ph = exact.get(_norm_value(text))
        if ph is not None:
            for i, (_s, _e, el) in enumerate(segs):
                _set_text(el, ph if i == 0 else "")
            return {ph: 1}
    matches, claimed = [], []
    for ph, _val, pat in patterns(usable, exact, text):
        for m in pat.finditer(text):
            ms, me = m.start(), m.end()
            if any(ms < ce and me > cs for cs, ce in claimed):
                continue                     # già coperto da un valore più lungo
            claimed.append((ms, me))
            matches.append((ms, me, ph))
    hits = {}
    # destra->sinistra: i prefissi (offset originali) dei nodi già toccati
    # restano validi per i match precedenti
    for ms, me, ph in sorted(matches, key=lambda x: -x[0]):
        hit = [s for s in segs if s[0] < me and s[1] > ms]
        if not hit:
            continue
        first = hit[0]
        t1 = first[2]
        text1 = t1.text or ""
        ls = ms - first[0]
        if me <= first[1]:
            _set_text(t1, text1[:ls] + ph + text1[me - first[0]:])
        else:
            _set_text(t1, text1[:ls] + ph)
            for seg in hit[1:]:
                t = seg[2]
                s = t.text or ""
                if me < seg[1]:
                    _set_text(t, s[me - seg[0]:])
                    break
                _set_text(t, "")
        hits[ph] = hits.get(ph, 0) + 1
    return hits


def _redact_node(el, usable, exact=None):
    """Redige il testo diretto di un singolo nodo."""
    return _redact_item(el, usable, (el.tag,), exact=exact)


def _redact_attrs(root, usable, allow=None, exact=None):
    """Redige i valori degli ATTRIBUTI (cache pivot, nomi colonna, filtri...).
    Con `allow` si limita agli attributi in allowlist (per i fogli, dove r/ref
    sono riferimenti strutturali da non toccare)."""
    hits = {}
    for el in root.iter():
        for attr, val in list(el.attrib.items()):
            if (allow is not None and attr not in allow) or not val or len(val) < 2:
                continue
            if exact:
                ph = exact.get(_norm_value(val))
                if ph is not None:
                    el.set(attr, ph)
                    hits[ph] = hits.get(ph, 0) + 1
                    continue
            new = val
            for ph, _v, pat in patterns(usable, exact, val):
                new, k = pat.subn(ph, new)
                if k:
                    hits[ph] = hits.get(ph, 0) + k
            if new != val:
                el.set(attr, new)
    return hits


def _acc(dst, hits):
    for ph, n in hits.items():
        dst[ph] = dst.get(ph, 0) + n


# attributi-VALORE delle cache pivot; gli altri (minValue, count, ref...) sono
# numeri/riferimenti strutturali: metterci un placeholder viola lo schema e
# Excel rifiuta il file
_PIVOT_VALUE_ATTRS = {"v", "name", "caption", "formula"}
_PIVOT_TYPED = {"n", "d", "b", "e"}          # item tipizzati: n=numero, d=data...
_PIVOT_STALE_HINTS = ("minValue", "maxValue", "minDate", "maxDate")


def _redact_pivot(root, usable, exact=None):
    """Cache dei pivot: redige SOLO gli attributi-valore; un item tipizzato
    (<n v="3665550107"/>) che diventa placeholder viene RITIPIZZATO a stringa
    (<s v="[TELEPHONENUM_1]"/>), e gli aggregati min/max che ripetono un valore
    mappato vengono rimossi (la cache si ricostruisce al refresh).
    Ritorna (hits, part_modificata)."""
    hits = {}
    touched = False
    parents = {c: p for p in root.iter() for c in p}
    refit = set()                            # sharedItems con item ritipizzati
    for el in root.iter():
        changed = False
        for attr, val in list(el.attrib.items()):
            if attr not in _PIVOT_VALUE_ATTRS or not val or len(val) < 2:
                continue
            if exact:
                # la cache pivot è una copia integrale dei dati: qui le
                # colonne del percorso tabellare ricompaiono per intero
                ph = exact.get(_norm_value(val))
                if ph is not None:
                    el.set(attr, ph)
                    hits[ph] = hits.get(ph, 0) + 1
                    changed = touched = True
                    continue
            new = val
            for ph, _v, pat in patterns(usable, exact, val):
                new, k = pat.subn(ph, new)
                if k:
                    hits[ph] = hits.get(ph, 0) + k
            if new != val:
                el.set(attr, new)
                changed = touched = True
        if changed and el.tag.rsplit("}", 1)[-1] in _PIVOT_TYPED:
            keep = el.get("v", "")
            el.attrib.clear()
            el.set("v", keep)
            el.tag = X + "s"
            parent = parents.get(el)
            if parent is not None and parent.tag.rsplit("}", 1)[-1] == "sharedItems":
                refit.add(parent)
    # un sharedItems toccato diventa TUTTO stringhe, senza flag di tipo: Excel
    # rifiuta varie combinazioni di item misti n/s (provate una a una col
    # bisect), mentre tutto-<s> senza flag apre sempre; al refreshOnLoad la
    # cache si ricostruisce dai fogli (redatti) coi tipi veri
    for si in refit:
        for ch in si:
            if ch.tag.rsplit("}", 1)[-1] in _PIVOT_TYPED:
                keep = ch.get("v", "")
                ch.attrib.clear()
                ch.set("v", keep)
                ch.tag = X + "s"
        for attr in ("containsString", "containsSemiMixedTypes",
                     "containsInteger", "containsNumber", "containsMixedTypes",
                     "containsDate", "containsNonDate", *_PIVOT_STALE_HINTS):
            si.attrib.pop(attr, None)
    for el in root.iter():
        if any((val := el.get(attr))
               and ((exact and _norm_value(val) in exact)
                    or any(pat.search(val) for _ph, _v, pat in patterns(usable, exact, val)))
               for attr in _PIVOT_STALE_HINTS):
            # in COPPIA: un minValue orfano del suo maxValue (o viceversa)
            # fa rifiutare il file a Excel
            for attr in _PIVOT_STALE_HINTS:
                el.attrib.pop(attr, None)
            touched = True
    return hits, touched


def _strip_phonetic(si):
    for rph in list(si):
        if rph.tag in (X + "rPh", X + "phoneticPr"):
            si.remove(rph)


def _cell_to_inline(c, text):
    """Converte una cella (numerica o risultato di formula) in stringa inline
    col testo redatto: via <f> e <v>, dentro <is><t>."""
    for tag in ("f", "v"):
        el = c.find(X + tag)
        if el is not None:
            c.remove(el)
    c.set("t", "inlineStr")
    is_el = ET.SubElement(c, X + "is")
    _set_text(ET.SubElement(is_el, X + "t"), text)


def _redact_sheet(root, sst_hits, usable, exact=None):
    """Redige un foglio. Ritorna (fills, frozen, changed):
    fills = [(cella, stile_originale), ...] da colorare di giallo (l'assegnazione
    dello stile avviene dopo, quando styles.xml ha le varianti); frozen = numero
    di formule congelate; changed = la part va riserializzata."""
    by_ph = {}
    fills, frozen, changed = [], 0, False
    frozen_shared = set()
    for c in root.iter(X + "c"):
        f = c.find(X + "f")
        freeze = False
        if f is not None:
            ftxt = f.text or ""
            if ftxt and any(pat.search(ftxt) for _ph, _v, pat in patterns(usable, exact, ftxt)):
                freeze = True                # PII letterale DENTRO la formula
        t = c.get("t", "n")
        hits = {}
        if t == "s":
            v = c.find(X + "v")
            try:
                idx = int(v.text) if v is not None and v.text else -1
            except ValueError:
                idx = -1
            if idx in sst_hits:
                hits = sst_hits[idx]
        elif t == "inlineStr":
            is_el = c.find(X + "is")
            if is_el is not None:
                hits = _redact_item(is_el, usable, (X + "t",), (X + "rPh",),
                                    exact=exact)
                if hits:
                    _strip_phonetic(is_el)
        else:                                # n / str / d / b / e: testo in <v>
            v = c.find(X + "v")
            if v is not None and v.text:
                hits = _redact_node(v, usable, exact=exact)
                if hits:
                    if f is not None:        # la cache era PII: formula congelata
                        if f.get("t") == "shared" and f.get("si") is not None:
                            frozen_shared.add(f.get("si"))
                        frozen += 1
                    _cell_to_inline(c, v.text)
                    f, freeze = None, False  # già rimossa da _cell_to_inline
        if hits:
            _acc(by_ph, hits)
            fills.append((c, c.get("s") or "0"))
            changed = True
            if f is not None:
                freeze = True                # la cache era PII: si congela
        if freeze and f is not None:
            if f.get("t") == "shared" and f.get("si") is not None:
                frozen_shared.add(f.get("si"))
            c.remove(f)
            frozen += 1
            changed = True
    # le formule condivise si congelano in GRUPPO: gli slave citano il master
    # per indice (si=...), lasciarli orfani fa scattare il ripristino di Excel
    if frozen_shared:
        for c in root.iter(X + "c"):
            f = c.find(X + "f")
            if f is not None and f.get("t") == "shared" and f.get("si") in frozen_shared:
                c.remove(f)
                frozen += 1
    # intestazioni di stampa, validazioni, formattazione condizionale, filtri,
    # hyperlink: testo e attributi-dato fuori dalle celle
    for el in root.iter():
        if el.tag in _SHEET_TEXT_TAGS and el.text:
            h = _redact_node(el, usable, exact=exact)
            if h:
                _acc(by_ph, h)
                changed = True
    h = _redact_attrs(root, usable, allow=_SHEET_DATA_ATTRS, exact=exact)
    if h:
        _acc(by_ph, h)
        changed = True
    return by_ph, fills, frozen, changed


# --------------------------------------------------------------------------- #
# Stili: varianti gialle
# --------------------------------------------------------------------------- #
def _yellow_style_map(styles_root, needed):
    """Per ogni stile originale coinvolto crea una variante identica con lo
    sfondo giallo. Ritorna {stile_originale: stile_giallo}.

    Il fill giallo è AUTO-DESCRITTIVO: nei pattern solid il renderer mostra
    solo il fgColor, quindi il bgColor (invisibile) trasporta il colore del
    fill solid ORIGINALE della cella. Il ripristino degli artifact
    (`restore_cell_styles`) lo rilegge da lì e ricostruisce lo stile di
    partenza senza stato fuori dal file — regge anche il round-trip di
    openpyxl nella sandbox, che rinumera stili e fill. bgColor indexed="64"
    (il default dei solid) = l'originale non aveva un fill solid (nessuno,
    gradiente, pattern): si ripristina a "nessun riempimento"."""
    if not needed:
        return {}
    fills = styles_root.find(X + "fills")
    cell_xfs = styles_root.find(X + "cellXfs")
    if fills is None or cell_xfs is None:
        return {}
    all_fills = list(fills)
    xfs = list(cell_xfs)

    def solid_fg(sid):
        """Attributi del fgColor del fill solid dello stile, o None."""
        try:
            base = xfs[int(sid)]
            f = all_fills[int(base.get("fillId") or 0)]
        except (ValueError, IndexError):
            return None
        pf = f.find(X + "patternFill")
        if pf is None or pf.get("patternType") != "solid":
            return None
        fg = pf.find(X + "fgColor")
        return dict(fg.attrib) if fg is not None and fg.attrib else None

    yellow_ids = {}                 # encoding del bgColor -> fillId giallo

    def yellow_fill(bg_attrs):
        key = tuple(sorted((bg_attrs or {"indexed": "64"}).items()))
        if key not in yellow_ids:
            fill = ET.SubElement(fills, X + "fill")
            pf = ET.SubElement(fill, X + "patternFill")
            pf.set("patternType", "solid")
            ET.SubElement(pf, X + "fgColor").set("rgb", "FFFFFF00")
            bg = ET.SubElement(pf, X + "bgColor")
            for k, v in key:
                bg.set(k, v)
            yellow_ids[key] = len(list(fills)) - 1
        return yellow_ids[key]

    mapping = {}
    for sid in sorted(needed, key=int):
        base = xfs[int(sid)] if 0 <= int(sid) < len(xfs) else None
        xf = copy.deepcopy(base) if base is not None else ET.Element(X + "xf")
        for attr in ("numFmtId", "fontId", "fillId", "borderId", "xfId"):
            if xf.get(attr) is None:
                xf.set(attr, "0")
        xf.set("fillId", str(yellow_fill(solid_fg(sid))))
        xf.set("applyFill", "1")
        cell_xfs.append(xf)
        mapping[sid] = str(len(list(cell_xfs)) - 1)
    fills.set("count", str(len(list(fills))))
    cell_xfs.set("count", str(len(list(cell_xfs))))
    return mapping


# --------------------------------------------------------------------------- #
# Ripristino degli artifact: via il giallo dalle celle coi placeholder
# --------------------------------------------------------------------------- #
def _norm_rgb(value):
    """FFFF00 e FFFFFF00 sono lo stesso colore (l'alfa FF è il default)."""
    v = (value or "").upper()
    return v[2:] if len(v) == 8 and v.startswith("FF") else v


def _is_app_yellow(pf):
    fg = pf.find(X + "fgColor")
    return (pf.get("patternType") == "solid" and fg is not None
            and _norm_rgb(fg.get("rgb")) == "FFFF00")


def _canon_xf(xf):
    """Firma confrontabile di un xf (attributi + figli, ordine normalizzato):
    per riusare un elemento identico già presente invece di duplicarlo."""
    return ET.tostring(xf, encoding="unicode")


def restore_cell_styles(parts, placeholders):
    """Rimette lo stile originale alle celle il cui testo contiene uno dei
    `placeholders` in via di ripristino e il cui stile porta il giallo della
    redazione (fill solid FFFF00). Il fill di partenza si rilegge dal bgColor
    auto-descrittivo del giallo (vedi _yellow_style_map).

    `parts` è {nome: bytes} dell'intero xlsx artifact. Ritorna {nome: bytes}
    delle sole part riscritte. Difensiva fino in fondo: a ogni intoppo
    ritorna {} e l'artifact resta com'è (un giallo di troppo è un difetto
    cosmetico, un file rotto no)."""
    try:
        return _restore_cell_styles(parts, placeholders)
    except Exception:
        return {}


def _restore_cell_styles(parts, placeholders):
    styles_data = parts.get("xl/styles.xml")
    if not styles_data or not placeholders:
        return {}
    styles_root = ET.fromstring(styles_data)
    fills = styles_root.find(X + "fills")
    cell_xfs = styles_root.find(X + "cellXfs")
    if fills is None or cell_xfs is None:
        return {}
    all_fills = list(fills)

    def none_fill_id():
        for i, f in enumerate(all_fills):
            pf = f.find(X + "patternFill")
            if pf is not None and pf.get("patternType") in (None, "none") \
                    and len(pf) == 0:
                return i
        return _add_fill(None)

    def _add_fill(fg_attrs):
        fill = ET.SubElement(fills, X + "fill")
        pf = ET.SubElement(fill, X + "patternFill")
        if fg_attrs:
            pf.set("patternType", "solid")
            fg = ET.SubElement(pf, X + "fgColor")
            for k, v in fg_attrs.items():
                fg.set(k, v)
            bg = ET.SubElement(pf, X + "bgColor")
            if _norm_rgb(fg_attrs.get("rgb", "")) == "FFFF00":
                # giallo dell'UTENTE ripristinato: bgColor = fgColor, così un
                # eventuale secondo passaggio lo decodifica di nuovo in "era
                # giallo" (punto fisso) invece di scambiarlo per il marcatore
                for k, v in fg_attrs.items():
                    bg.set(k, v)
            else:
                bg.set("indexed", "64")
        else:
            pf.set("patternType", "none")
        all_fills.append(fill)
        fills.set("count", str(len(all_fills)))
        return len(all_fills) - 1

    def solid_fill_id(fg_attrs):
        for i, f in enumerate(all_fills):
            pf = f.find(X + "patternFill")
            if pf is None or pf.get("patternType") != "solid":
                continue
            fg = pf.find(X + "fgColor")
            if fg is None:
                continue
            a, b = dict(fg.attrib), dict(fg_attrs)
            for d in (a, b):
                if "rgb" in d:
                    d["rgb"] = _norm_rgb(d["rgb"])
            if a == b and not _is_app_yellow(pf):
                return i
        return _add_fill(fg_attrs)

    # 1) gli xf gialli e il loro sostituto (riusato se già presente)
    xfs = list(cell_xfs)
    restored = {}                   # indice giallo (str) -> indice nuovo (str)
    canon = {_canon_xf(xf): i for i, xf in enumerate(xfs)}
    for i, xf in enumerate(xfs):
        try:
            pf = all_fills[int(xf.get("fillId") or 0)].find(X + "patternFill")
        except (ValueError, IndexError):
            continue
        if pf is None or not _is_app_yellow(pf):
            continue
        bg = pf.find(X + "bgColor")
        bg_attrs = dict(bg.attrib) if bg is not None else {}
        if not bg_attrs or bg_attrs == {"indexed": "64"}:
            target_fill = none_fill_id()
        else:
            target_fill = solid_fill_id(bg_attrs)
        new = copy.deepcopy(xf)
        new.set("fillId", str(target_fill))
        key = _canon_xf(new)
        if key in canon:
            restored[str(i)] = str(canon[key])
        else:
            cell_xfs.append(new)
            idx = len(list(cell_xfs)) - 1
            canon[key] = idx
            restored[str(i)] = str(idx)
    if not restored:
        return {}
    cell_xfs.set("count", str(len(list(cell_xfs))))

    # 2) gli indici delle shared strings che contengono un placeholder
    sst_hit = set()
    sst_data = parts.get("xl/sharedStrings.xml")
    if sst_data:
        sst_root = ET.fromstring(sst_data)
        for i, si in enumerate(sst_root.findall(X + "si")):
            text = "".join(si.itertext())
            if any(ph in text for ph in placeholders):
                sst_hit.add(str(i))

    def cell_hit(c):
        t = c.get("t", "n")
        if t == "s":
            v = c.find(X + "v")
            return v is not None and (v.text or "").strip() in sst_hit
        if t == "inlineStr":
            is_el = c.find(X + "is")
            text = "".join(is_el.itertext()) if is_el is not None else ""
        else:
            v = c.find(X + "v")
            text = v.text or "" if v is not None else ""
        return any(ph in text for ph in placeholders)

    # 3) i fogli: via il giallo SOLO dalle celle col placeholder
    replaced = {}
    touched_styles = False
    for name, data in parts.items():
        if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
            continue
        root = ET.fromstring(data)
        changed = False
        for c in root.iter(X + "c"):
            s = c.get("s")
            if s in restored and cell_hit(c):
                c.set("s", restored[s])
                changed = True
        if changed:
            replaced[name] = _serialize_part(root, _part_namespaces(data))
            touched_styles = True
    if touched_styles:
        replaced["xl/styles.xml"] = _serialize_part(
            styles_root, _part_namespaces(styles_data))
    return replaced


# --------------------------------------------------------------------------- #
# Scrub e pulizie
# --------------------------------------------------------------------------- #
def _scrub_comment_authors(root):
    """Commenti classici: <authors><author>Nome</author> -> svuotati."""
    n = 0
    for el in root.iter(X + "author"):
        if (el.text or "").strip():
            el.text = ""
            n += 1
    return n


def _scrub_persons(xml_bytes):
    """Autori dei commenti THREADED (xl/persons): nome e account sono attributi."""
    root = ET.fromstring(xml_bytes)
    n = 0
    for el in root.iter():
        for attr in ("displayName", "userId"):
            if el.get(attr):
                el.set(attr, "")
                n += 1
    if n == 0:
        return xml_bytes
    return _serialize_part(root, _part_namespaces(xml_bytes))


def _drop_calc_chain_ct(ct_bytes):
    """Toglie l'Override di calcChain da [Content_Types].xml."""
    root = ET.fromstring(ct_bytes)
    for el in list(root):
        if el.get("PartName") == "/xl/calcChain.xml":
            root.remove(el)
    ET.register_namespace("", "http://schemas.openxmlformats.org/package/2006/content-types")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _drop_calc_chain_rel(rels_bytes):
    """Toglie la Relationship verso calcChain dai rels del workbook."""
    root = ET.fromstring(rels_bytes)
    for el in list(root):
        if (el.get("Target") or "").endswith("calcChain.xml"):
            root.remove(el)
    ET.register_namespace("", "http://schemas.openxmlformats.org/package/2006/relationships")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# --------------------------------------------------------------------------- #
# API pubblica
# --------------------------------------------------------------------------- #
def redact_xlsx(xlsx_bytes, mapping, exact_phs=None, ctl=None, ocr_cache=None):
    """Xlsx originale -> xlsx con placeholder e celle gialle + report.
    Stesse chiavi di report di redact_docx/redact_pptx (+ frozen_formulas).
    ocr_cache: le part xl/media/* pianificate vengono riscritte coi box gialli
    nei pixel (vedi image_ocr); senza cache le part media passano intatte.

    `ctl` (progress.JobControl, opzionale): sui workbook enormi la redazione
    stessa dura minuti — avanzamento per part (sharedStrings, poi ogni foglio,
    poi il resto) e checkpoint di annullamento dentro i loop lunghi.

    `exact_phs` = placeholder del percorso tabellare (xlsx_table): i loro
    valori si cercano prima per uguaglianza dell'intera cella (dizionario
    O(1)). Un indice seleziona i candidati per le occorrenze dentro testo più
    lungo e metadati: anche queste vengono redatte, con gli stessi pattern
    del controllo di uscita, senza provare 40k regex su ogni cella."""
    from .progress import NULL as _NULL_CTL
    ctl = ctl or _NULL_CTL
    if not isinstance(mapping, dict) or not mapping:
        raise XlsxError("Dizionario vuoto: anonimizza prima il documento.")
    items = sorted(((ph, v) for ph, v in mapping.items()
                    if isinstance(ph, str) and isinstance(v, str) and v.strip()),
                   key=lambda kv: -len(kv[1]))
    if not items:
        raise XlsxError("Dizionario non valido.")
    exact_set = set(exact_phs or ())

    skipped, usable = [], []
    exact = CellValues()                     # whole-cell lookup + embedded index
    for ph, val in items:
        label = ph.strip("[]").rsplit("_", 1)[0]
        if ph in exact_set and label not in EXACT_SPAN_LABELS:
            nv = _norm_value(val)
            if nv:
                # il primo (valore più LUNGO, per l'ordinamento sopra) vince
                exact.setdefault(nv, ph)
            else:
                skipped.append(ph)
            continue
        if _too_noisy(val, ph):
            skipped.append(ph)
            continue
        pat = _value_pattern(val, ph)
        if pat:
            usable.append((ph, val, pat))
        else:
            skipped.append(ph)

    exact.index = ValueIndex((ph, val) for ph, val in items
                             if ph in exact_set and not _too_noisy(val, ph))
    zf, names = _open(xlsx_bytes)
    parts = {info.filename: zf.read(info.filename) for info in zf.infolist()}
    name_parts, name_hits = redact_names(parts, ValueIndex(
        (ph, val) for ph, val in items if ph not in skipped))
    if name_parts:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zf.infolist():
                zout.writestr(info, name_parts.get(info.filename, parts[info.filename]))
        xlsx_bytes = buf.getvalue()
        zf.close()
        zf, names = _open(xlsx_bytes)
    by_ph = dict(name_hits)
    replaced = {}                            # nome part -> bytes riserializzati
    frozen_total = 0
    sheets = _sheet_parts(zf, names)
    # avanzamento per part: sharedStrings + un tick a foglio + "il resto"
    # (commenti/grafici/pivot/scrittura zip, di solito trascurabili)
    n_units = 1 + len(sheets) + 1
    ctl.tick(0, n_units)

    # 1) sharedStrings: si redige il testo UNICO; le celle che lo referenziano
    #    verranno colorate (e contate) foglio per foglio
    sst_hits = {}
    if "xl/sharedStrings.xml" in names:
        data = zf.read("xl/sharedStrings.xml")
        sst_root = ET.fromstring(data)
        sst_changed = False
        for i, si in enumerate(sst_root):
            if i % 1024 == 0:
                ctl.check()          # tabella enorme: annullabile a metà strada
            hits = _redact_item(si, usable, (X + "t",), (X + "rPh",), exact=exact)
            if hits:
                sst_hits[i] = hits
                _strip_phonetic(si)
                sst_changed = True
        if sst_changed:
            replaced["xl/sharedStrings.xml"] = _serialize_part(
                sst_root, _part_namespaces(data))
    ctl.tick(1, n_units)

    # 2) fogli: celle, formule, filtri, hyperlink, intestazioni di stampa
    all_fills = []                           # (cella, stile_orig) su tutti i fogli
    sheet_roots = {}                         # part -> (root, decls) da colorare
    for si_idx, (part, _sn) in enumerate(sheets):
        data = zf.read(part)
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            raise XlsxError(f"XML non valido dentro l'xlsx ({part}).")
        hits, fills, frozen, changed = _redact_sheet(root, sst_hits, usable,
                                                     exact=exact)
        _acc(by_ph, hits)
        frozen_total += frozen
        all_fills.extend(fills)
        if changed or fills:
            sheet_roots[part] = (root, _part_namespaces(data))
        ctl.tick(2 + si_idx, n_units)

    # 3) styles: varianti gialle degli stili coinvolti, poi assegnazione
    if all_fills and "xl/styles.xml" in names:
        data = zf.read("xl/styles.xml")
        styles_root = ET.fromstring(data)
        style_map = _yellow_style_map(styles_root, {sid for _c, sid in all_fills})
        if style_map:
            for c, sid in all_fills:
                c.set("s", style_map[sid])
            replaced["xl/styles.xml"] = _serialize_part(
                styles_root, _part_namespaces(data))
    for part, (root, decls) in sheet_roots.items():
        replaced[part] = _serialize_part(root, decls)

    # 4) commenti (testo + autori), threaded comments, caselle di testo,
    #    grafici, part con dati negli attributi
    for name in names:
        ctl.check()
        if _COMMENTS_RE.match(name):
            data = zf.read(name)
            root = ET.fromstring(data)
            hits = {}
            for txt in root.iter(X + "text"):
                _acc(hits, _redact_item(txt, usable, (X + "t",), (X + "rPh",),
                                        exact=exact))
            n_auth = _scrub_comment_authors(root)
            if hits or n_auth:
                _acc(by_ph, hits)
                replaced[name] = _serialize_part(root, _part_namespaces(data))
        elif _THREADED_RE.match(name):
            data = zf.read(name)
            root = ET.fromstring(data)
            hits = {}
            for el in root.iter():
                if el.tag.rsplit("}", 1)[-1] == "text" and el.text:
                    _acc(hits, _redact_node(el, usable, exact=exact))
            if hits:
                _acc(by_ph, hits)
                replaced[name] = _serialize_part(root, _part_namespaces(data))
        elif _DRAWING_RE.match(name) or _CHART_RE.match(name):
            data = zf.read(name)
            root = ET.fromstring(data)
            hits = {}
            _redact_drawing_tree(root, usable, hits)     # a:t con evidenziazione
            for el in root.iter():
                if el.tag.rsplit("}", 1)[-1] in ("v", "pt", "t") and el.text:
                    # exact anche qui: le cache dei grafici ripetono le colonne
                    _acc(hits, _redact_node(el, usable, exact=exact))
            # testo alternativo/titolo di immagini e forme: svuotati, non
            # sostituiti (vedi docx._scrub_alt_text)
            n_alt = _scrub_alt_text(root)
            if hits or n_alt:
                _acc(by_ph, hits)
                replaced[name] = _serialize_part(root, _part_namespaces(data))
        elif _ATTR_PART_RE.match(name):
            data = zf.read(name)
            root = ET.fromstring(data)
            if "pivotCache" in name:
                hits, touched = _redact_pivot(root, usable, exact=exact)
                if touched and "pivotCacheDefinition" in name:
                    # al primo aperto Excel ricostruisce la cache dai fogli
                    # (già redatti), sanando i tipi ritipizzati
                    root.set("refreshOnLoad", "1")
            elif "pivotTables" in name:
                hits = _redact_attrs(root, usable, allow={
                    "name", "caption", "n", "dataCaption", "rowHeaderCaption",
                    "colHeaderCaption", "errorCaption", "missingCaption",
                    "subtotalCaption"}, exact=exact)
                touched = bool(hits)
            elif "tables/" in name:
                hits = _redact_attrs(root, usable, allow={"name", "displayName"},
                                     exact=exact)
                touched = bool(hits)
            else:                            # connections: stringhe libere
                hits = _redact_attrs(root, usable, exact=exact)
                touched = bool(hits)
            if hits or touched:
                _acc(by_ph, hits)
                replaced[name] = _serialize_part(root, _part_namespaces(data))

    tracked = set(skipped)
    by_ph = {ph: by_ph.get(ph, 0) for ph, _v in items if ph not in tracked}
    # preparati una volta sola: le part .rels sono molte e i valori, con le
    # colonne anonimizzate per intero, possono essere decine di migliaia
    values = _RelsValues(v for ph, v in items if ph not in tracked)
    drop_calc = frozen_total > 0 and "xl/calcChain.xml" in names

    img_by_ph = {}
    media_redactor = image_ocr.ooxml_media_redactor(ocr_cache, mapping, img_by_ph)
    src = zipfile.ZipFile(io.BytesIO(xlsx_bytes))
    out_buf = io.BytesIO()
    n_rels = 0
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in src.infolist():
            name = info.filename
            if drop_calc and name == "xl/calcChain.xml":
                continue
            data = src.read(name)
            if name in replaced:
                data = replaced[name]
            elif _PERSONS_RE.match(name):
                data = _scrub_persons(data)
            elif name == "docProps/core.xml":
                data = _scrub_core_props(data)
            elif name == "docProps/app.xml":
                data = _scrub_app_props(data)     # TitlesOfParts = nomi dei fogli
            elif name == "[Content_Types].xml" and drop_calc:
                data = _drop_calc_chain_ct(data)
            elif name.endswith(".rels"):
                data, n = _scrub_rels(data, values)
                n_rels += n
                if drop_calc and name == "xl/_rels/workbook.xml.rels":
                    data = _drop_calc_chain_rel(data)
            elif media_redactor is not None:
                data = media_redactor(name, data)
            zout.writestr(name, data)
    out = out_buf.getvalue()
    ctl.tick(n_units, n_units)

    report = {
        "occurrences": sum(by_ph.values()),
        "by_placeholder": by_ph,
        "not_found": [ph for ph, n in by_ph.items() if n == 0],
        "skipped": skipped,
        "residual": _verify_residuals(out, [(ph, v) for ph, v, _p in usable],
                                      exact=exact),
        "rels": n_rels,
        "frozen_formulas": frozen_total,
    }
    if ocr_cache:
        image_ocr.merge_image_report(report, img_by_ph)
    return out, report


# figli di CT_Worksheet che nello schema (sequenza obbligata) vengono DOPO
# printOptions / DOPO pageSetup: i nodi nuovi si inseriscono prima del primo
# di questi, mai appesi in coda (stessa trappola d'ordine di w:highlight)
_AFTER_PAGE_SETUP = {X + t for t in (
    "headerFooter", "rowBreaks", "colBreaks", "customProperties", "cellWatches",
    "ignoredErrors", "smartTags", "drawing", "drawingHF", "picture",
    "oleObjects", "controls", "webPublishItems", "tableParts", "extLst")}
_AFTER_PRINT_OPTIONS = _AFTER_PAGE_SETUP | {X + "pageMargins", X + "pageSetup"}
_AFTER_HEADER_FOOTER = _AFTER_PAGE_SETUP - {X + "headerFooter"}


def _insert_ordered(root, el, after_set):
    idx = next((i for i, ch in enumerate(root) if ch.tag in after_set), len(root))
    root.insert(idx, el)


def _spreadsheet_look(root):
    """Resa "da foglio di calcolo" per la SOLA copia di anteprima: la
    conversione PDF è una stampa, e senza impostazioni di stampa LibreOffice
    produce testo nudo su pagine bianche (sembra un Word). Griglia e
    intestazioni A/B/C-1/2/3 si forzano sempre; orientamento orizzontale e
    adattamento alla larghezza pagina solo se il foglio non ha già un
    pageSetup suo (una scelta di stampa esplicita si rispetta)."""
    po = root.find(X + "printOptions")
    if po is None:
        po = ET.Element(X + "printOptions")
        _insert_ordered(root, po, _AFTER_PRINT_OPTIONS)
    po.set("gridLines", "1")
    po.set("headings", "1")
    # nome del foglio nell'intestazione di pagina (&A): è il modo in cui il
    # backend riconduce ogni pagina del PDF al suo foglio (vedi
    # pdf.spreadsheet_columns) — solo copia di anteprima, come tutto qui
    hf = root.find(X + "headerFooter")
    if hf is None:
        hf = ET.Element(X + "headerFooter")
        _insert_ordered(root, hf, _AFTER_HEADER_FOOTER)
    for attr in ("differentOddEven", "differentFirst"):
        hf.attrib.pop(attr, None)
    for tag in ("evenHeader", "firstHeader"):
        el = hf.find(X + tag)
        if el is not None:
            hf.remove(el)
    oh = hf.find(X + "oddHeader")
    if oh is None:
        oh = ET.Element(X + "oddHeader")
        hf.insert(0, oh)             # oddHeader è il primo figlio dello schema
    oh.text = "&C&A"
    if root.find(X + "pageSetup") is None:
        ps = ET.Element(X + "pageSetup")
        ps.set("orientation", "landscape")
        ps.set("fitToWidth", "1")
        ps.set("fitToHeight", "0")      # largo 1 pagina, alto quante servono
        _insert_ordered(root, ps, _AFTER_PAGE_SETUP)
        # fitToWidth agisce solo col flag fitToPage in sheetPr/pageSetUpPr
        pr = root.find(X + "sheetPr")
        if pr is None:
            pr = ET.Element(X + "sheetPr")
            root.insert(0, pr)          # sheetPr è il PRIMO figlio dello schema
        pspr = pr.find(X + "pageSetUpPr")
        if pspr is None:
            pspr = ET.SubElement(pr, X + "pageSetUpPr")
        pspr.set("fitToPage", "1")


# larghezza massima (in caratteri Excel) a cui una colonna viene portata per
# mostrare i placeholder per intero: oltre, con fitToWidth, il foglio si
# rimpicciolirebbe troppo per essere leggibile
_MAX_PREVIEW_COL_W = 40.0


def _widen_placeholder_columns(root, sst):
    """Allarga (nella SOLA copia di anteprima) le colonne le cui celle
    contengono un placeholder [TAG_n]: nel render di stampa una cella più
    stretta del suo testo viene TRONCATA se la vicina è occupata, il token
    "[TELEPHONENUM_1]" diventa "[TELEPHO" e la ricerca dei placeholder
    nell'anteprima (pdf._placeholder_boxes) non può più trovarlo: box
    giallo muto, tooltip e deanonimizzazione impossibili. La redazione NON
    passa da qui: è solo resa."""
    sheet_data = root.find(X + "sheetData")
    if sheet_data is None:
        return
    need = {}                                   # col 0-based -> larghezza char
    for row in sheet_data:
        idx = 0
        for c in row:
            if c.tag != X + "c":
                continue
            ci = _col_idx(c, idx)
            idx = ci + 1
            txt = _cell_text(c, sst)
            if not txt or "[" not in txt or "]" not in txt:
                continue
            w = min(_MAX_PREVIEW_COL_W, max(len(ln) for ln in txt.split("\n")) + 2.0)
            if w > need.get(ci, 0):
                need[ci] = w
    if not need:
        return

    cols_el = root.find(X + "cols")
    if cols_el is not None:
        # def esistenti: le colonne già abbastanza larghe si lasciano stare,
        # le altre si RITAGLIANO fuori dai range (i range di <col> non devono
        # sovrapporsi) per ridefinirle sotto
        for col in list(cols_el):
            try:
                mn, mx = int(col.get("min")), int(col.get("max"))
            except (TypeError, ValueError):
                continue
            try:
                cur_w = float(col.get("width") or 0)
            except ValueError:
                cur_w = 0
            hit = [i for i in need if mn <= i + 1 <= mx]
            for i in hit:
                if cur_w >= need[i]:
                    del need[i]
            hit = sorted(i + 1 for i in need if mn <= i + 1 <= mx)
            if not hit:
                continue
            # spezza [mn, mx] togliendo le colonne da ridefinire
            cols_el.remove(col)
            start = mn
            for h in hit + [mx + 1]:
                if start <= h - 1:
                    seg = ET.Element(X + "col", dict(col.attrib))
                    seg.set("min", str(start))
                    seg.set("max", str(h - 1))
                    cols_el.append(seg)
                start = h + 1
    if not need:
        return
    if cols_el is None:
        cols_el = ET.Element(X + "cols")
        root.insert(list(root).index(sheet_data), cols_el)
    for i, w in sorted(need.items()):
        ET.SubElement(cols_el, X + "col",
                      {"min": str(i + 1), "max": str(i + 1),
                       "width": str(w), "customWidth": "1"})


def truncate_for_preview(xlsx_bytes, max_rows=None):
    """(bytes, troncato?) — copia usata SOLO per la conversione PDF
    dell'anteprima: al massimo `max_rows` righe per foglio + resa da foglio di
    calcolo (griglia, intestazioni, orizzontale). La redazione lavora sempre
    sul file completo. max_rows assente = parametro del pannello admin.

    L'anteprima MOSTRA TUTTO: fogli e righe/colonne nascosti vengono resi
    visibili (solo qui). È un tool di verifica dell'anonimizzazione: la PII
    che si nasconde in un foglio hidden è proprio quella da far vedere, e
    senza render i suoi placeholder non avrebbero box interattivi."""
    if max_rows is None:
        max_rows = settings_store.current("xlsx_preview_rows")
    zf, names = _open(xlsx_bytes)
    sst = _shared_strings(zf, names)
    changed = {}
    truncated = False
    for name in names:
        if not _SHEET_RE.match(name):
            continue
        data = zf.read(name)
        root = ET.fromstring(data)
        sheet_data = root.find(X + "sheetData")
        rows = list(sheet_data) if sheet_data is not None else []
        if len(rows) > max_rows:
            for r in rows[max_rows:]:
                sheet_data.remove(r)
            truncated = True
        for row in (sheet_data if sheet_data is not None else ()):
            row.attrib.pop("hidden", None)     # righe nascoste/filtrate
        cols_el = root.find(X + "cols")
        if cols_el is not None:
            for col in cols_el:
                col.attrib.pop("hidden", None)  # colonne nascoste
        _widen_placeholder_columns(root, sst)
        _spreadsheet_look(root)
        changed[name] = _serialize_part(root, _part_namespaces(data))
    # fogli nascosti: LibreOffice non li stampa affatto — visibili in anteprima
    wb_data = zf.read("xl/workbook.xml")
    wb_root = ET.fromstring(wb_data)
    wb_hit = False
    sheets_el = wb_root.find(X + "sheets")
    for sh in (sheets_el if sheets_el is not None else ()):
        if sh.get("state") in ("hidden", "veryHidden"):
            del sh.attrib["state"]
            wb_hit = True
    # pivot: al caricamento LibreOffice RICALCOLA le tabelle pivot dalla
    # cache e sovrascrive le celle dell'area pivot (dove stanno i placeholder
    # redatti) con la propria resa. Senza la macchineria pivot le celle
    # salvate si stampano così come sono: è quel che l'anteprima deve fare.
    pc = wb_root.find(X + "pivotCaches")
    if pc is not None:
        wb_root.remove(pc)
        wb_hit = True
    if wb_hit:
        changed["xl/workbook.xml"] = _serialize_part(wb_root,
                                                     _part_namespaces(wb_data))
    src = zipfile.ZipFile(io.BytesIO(xlsx_bytes))
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in src.infolist():
            if _PIVOT_PART_RE.match(info.filename):
                continue                       # via anche i part pivot
            blob = changed.get(info.filename) or src.read(info.filename)
            if info.filename == "[Content_Types].xml" \
                    or info.filename.endswith(".rels"):
                blob = _strip_pivot_refs(info.filename, blob)
            zout.writestr(info.filename, blob)
    return out_buf.getvalue(), truncated


_PIVOT_PART_RE = re.compile(r"^xl/(pivotTables|pivotCache)/")
_RELS_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"


def _strip_pivot_refs(name, data):
    """Toglie da .rels e [Content_Types].xml i riferimenti ai part pivot
    rimossi dall'anteprima: un riferimento pendente può far rifiutare il
    file a LibreOffice."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return data
    hit = False
    if name == "[Content_Types].xml":
        for el in list(root):
            part = (el.get("PartName") or "").lstrip("/")
            if el.tag == _CT_NS + "Override" and _PIVOT_PART_RE.match(part):
                root.remove(el)
                hit = True
    else:
        for el in list(root):
            tgt = (el.get("Target") or "").replace("../", "xl/").lstrip("/")
            if el.tag == _RELS_NS + "Relationship" \
                    and ("pivotTable" in tgt or "pivotCache" in tgt):
                root.remove(el)
                hit = True
    if not hit:
        return data
    return _serialize_part(root, _part_namespaces(data))


def rebuild_xlsx(xlsx_bytes, mapping, preview_pdf_original=None, exact_phs=None,
                 ctl=None, make_preview=True, ocr_cache=None):
    """Redazione dell'xlsx con una mappa GIÀ DATA (niente ri-analisi) + PDF di
    anteprima (via LibreOffice, sulla copia TRONCATA) + box. Stesso contratto
    di rebuild_docx/rebuild_pptx. `exact_phs`: vedi redact_xlsx."""
    from . import convert, pdf
    from .progress import NULL as _NULL_CTL
    ctl = ctl or _NULL_CTL

    if not mapping:
        raise XlsxError("La mappa è vuota: niente da redigere.")
    ctl.phase("redaction")
    out, report = redact_xlsx(xlsx_bytes, mapping, exact_phs=exact_phs, ctl=ctl,
                              ocr_cache=ocr_cache)
    if report["occurrences"] == 0:
        raise XlsxError("Nessuna occorrenza trovata nel file Excel.")
    if not make_preview:
        report["preview_truncated"] = False
        return {"file": out, "report": report,
                "original_boxes": {}, "anonymized_boxes": {}}
    ctl.phase("preview")

    # la redazione non aggiunge né toglie righe: il flag di troncamento del
    # redatto vale anche per l'originale, che va troncato e convertito SOLO
    # se il chiamante non ha già la sua anteprima (modifiche alla mappa:
    # niente rewrite dello zip originale a ogni ritocco)
    trunc_anon, truncated = truncate_for_preview(out)
    report["preview_truncated"] = truncated

    trunc_orig = None
    if preview_pdf_original is None:
        trunc_orig, _ = truncate_for_preview(xlsx_bytes)
        preview_pdf_original = convert.to_pdf(trunc_orig, suffix=".xlsx")
    preview_orig = preview_pdf_original
    preview_anon = convert.to_pdf(trunc_anon, suffix=".xlsx")

    # box di anteprima: con le mappe enormi del percorso tabellare la ricerca
    # dei valori nel PDF non può provare 40k pattern; ai placeholder "exact"
    # si chiede prima di comparire nelle righe TRONCATE dell'anteprima
    preview_mapping = mapping
    exact_set = set(exact_phs or ()) & mapping.keys()
    if exact_set:
        try:
            trunc_orig_bytes, _ = truncate_for_preview(xlsx_bytes)
            segments = {_norm_value(seg)
                        for line in extract_text(trunc_orig_bytes, full=True).split("\n")
                        for seg in line.split(" | ")}
        except XlsxError:
            segments = set()
        preview_mapping = {ph: v for ph, v in mapping.items()
                           if ph not in exact_set or _norm_value(v) in segments}

    original_boxes = pdf._value_boxes(preview_orig, preview_mapping)
    anonymized_boxes = pdf._placeholder_boxes(preview_anon, preview_mapping)
    # le entità lette nelle immagini non compaiono nel testo dei PDF
    # convertiti (stanno nei pixel): i loro box si ritrovano a parte, sulle
    # copie TRONCATE che sono quelle davvero convertite
    if ocr_cache:
        if trunc_orig is None:      # anteprima originale già pronta dal chiamante
            trunc_orig, _ = truncate_for_preview(xlsx_bytes)
        image_ocr.merge_ooxml_overlays(
            ocr_cache, mapping,
            [(original_boxes, preview_orig, trunc_orig),
             (anonymized_boxes, preview_anon, trunc_anon)])
    for pages in (original_boxes, anonymized_boxes):
        for blist in pages.values():
            for b in blist:
                b["label"] = pdf._ph_label(b["ph"])

    return {
        "file": out,
        "report": report,
        "original_boxes": original_boxes,
        "anonymized_boxes": anonymized_boxes,
        "preview_pdf_original": preview_orig,
        "preview_pdf_anonymized": preview_anon,
        "page_sizes": {"original": pdf.page_sizes(preview_orig),
                       "anonymized": pdf.page_sizes(preview_anon)},
    }


def _header_only_phs(mapping, labels, hay_values, hay_texts):
    """Placeholder da NON applicare: il loro valore è l'intestazione di una
    colonna di un foglio tabellare e non compare da nessun'altra parte.

    Serve perché la redazione è cieca alla posizione: cerca i valori della
    mappa ovunque, e non sa che una cella è un nome di colonna. Se il modello
    tagga un'etichetta (caso tipico: una colonna 'Marchio' letta come
    FULLNAME), l'unica cella colpita è proprio l'intestazione — la tabella
    diventa illeggibile senza che nessun dato sia stato protetto.

    Il filtro toglie il valore dalla mappa PRIMA della redazione, invece di
    ripristinare la cella dopo: un ripristino sarebbe un valore in chiaro che
    `_verify_residuals` ritrova, dichiarando il file non protetto (422), e
    lascerebbe l'intestazione disallineata dalle sue copie-etichetta (nomi
    colonna di xl/tables, cacheField dei pivot, nomi serie dei grafici), che
    Excel "ripara" all'apertura.

    Condizione dura: si scarta solo se il valore non si trova NEMMENO UNA
    VOLTA fuori dalle righe header. Se un'etichetta coincide con un dato vero
    (colonna 'Milano' con celle 'Milano' sotto) scartarla scoprirebbe le celle,
    quindi resta anonimizzata. La ricerca è a sottostringa, con lo
    stesso `_value_pattern` della redazione: header 'Rossi' e cella 'Mario
    Rossi' contano come collisione.

    `hay_values` = valori-cella (unici di colonna, celle fuori colonna);
    `hay_texts` = testi più grossi (righe-titolo sopra l'header, fogli non
    tabellari, commenti/grafici/caselle di testo). Restano fuori dal pagliaio
    le superfici che copiano l'ETICHETTA e non il dato (nomi colonna delle
    tabelle Excel, campi dei pivot): lì il valore DEVE restare in chiaro
    insieme all'intestazione, o le due copie si disallineano.

    Nei progetti/chat la mappa è una ReplacementMapping: un placeholder può
    portare PIÙ superfici (alias del registro, varianti dedotte). Scartarlo
    le porta via tutte, quindi il candidato si sceglie sul valore canonico ma
    il controllo si fa su OGNI sua superficie: basta che una compaia nei dati
    perché il placeholder resti."""
    surfaces = {}
    for ph, val in mapping.items():          # ReplacementMapping: anche gli alias
        surfaces.setdefault(ph, []).append(val)
    out = {}
    for ph, val in dict.items(mapping):      # il valore CANONICO
        if _norm_value(val) not in labels:
            continue
        vals = surfaces.get(ph) or []
        if val not in vals:
            vals = [val, *vals]
        # le superfici senza pattern non si redigono comunque: già innocue
        pats = [p for p in (_value_pattern(v, ph) for v in vals) if p is not None]
        if not pats:
            continue
        if any(p.search(t) for p in pats for t in hay_texts):
            continue
        if any(p.search(v) for p in pats for v in hay_values):
            continue
        out[ph] = val
    return out


def _chunk_cap_error(n_chunks, max_chunks):
    est = max(1, round(n_chunks * _SECONDS_PER_CHUNK / 60))
    return XlsxError(
        f"Questo foglio supera il limite di analisi impostato "
        f"dall'amministratore ({n_chunks} blocchi di testo, il massimo è "
        f"{max_chunks}: servirebbero circa {est} minuti di modello). "
        f"Dividi il file, rimuovi le colonne non necessarie o chiedi "
        f"all'amministratore di alzare il limite nelle impostazioni.")


def anonymize_xlsx(xlsx_bytes, engine, excluded=None, custom_terms=None,
                   max_chunks=None, ctl=None, make_preview=True,
                   allow_empty=False, ocr=False):
    """Xlsx -> analisi PII + redazione gialla.

    Fogli TABELLARI (xlsx_table.detect_table, soglie del pannello admin): il
    modello analizza solo un CAMPIONE di righe; le colonne che nel campione
    risultano PII "atomiche" (la cella È il valore) si anonimizzano in modo
    deterministico — unique della colonna -> placeholder, con gli stessi tag
    e contatori del modello —; le colonne con PII dentro testo libero tornano
    al modello ma sui soli valori UNICI; le altre passano dalla rete
    regex/checksum come rete di sicurezza. Tutto il resto del workbook
    (fogli non tabellari, commenti, grafici, righe-titolo) segue il percorso
    classico. Un'unica redazione finale con la mappa combinata copre anche le
    copie nascoste (pivot, filtri, docProps...).

    INTESTAZIONI DI COLONNA: la riga header non si manda al modello (una fila
    di etichette senza contesto invita a leggerci nomi propri) e, se
    un'etichetta finisce comunque in mappa da un'altra strada, si scarta —
    ma solo se quel valore non compare in nessun DATO del workbook
    (_header_only_phs). Le etichette restano visibili al modello come
    prefisso "Etichetta: valore" del campione, dove sono contesto utile.

    max_chunks: tetto sui chunk di analisi (0/None = nessun limite), deciso
    dall'amministratore nelle impostazioni e passato dal worker.

    ctl (progress.JobControl, opzionale): le fasi dipendono dal percorso e si
    dichiarano appena la partizione dei fogli è nota — coi fogli tabellari
    c'è un secondo pass del modello ("Analisi colonne"), senza no."""
    from .core import chunk_text
    from .detectors import detect_regex
    from .progress import NULL as _NULL_CTL
    ctl = ctl or _NULL_CTL

    excluded_set = set(excluded or ())
    zf, names = _open(xlsx_bytes)
    sst = _shared_strings(zf, names)
    min_rows = settings_store.current("xlsx_table_min_rows")
    k_sample = settings_store.current("xlsx_table_sample_rows")
    coverage = min(100, settings_store.current("xlsx_table_coverage_pct"))

    # 1) partizione dei fogli e testo per il modello (pass 1)
    texts, plans = [], []
    # pagliaio del filtro sulle intestazioni (_header_only_phs): tutto il
    # workbook TRANNE le righe header dei fogli tabellari
    hay_texts = []
    for part, sheet_name in _sheet_parts(zf, names):
        ctl.check()
        root = ET.fromstring(zf.read(part))
        rows = _sheet_rows_idx(root, sst)
        det = xlsx_table.detect_table(rows, _merged_ranges(root), min_rows)
        if not det["tabular"]:
            t = _sheet_text(root, sst, enriched=True)
            if t.strip():
                texts.append(t)
                hay_texts.append(t)
            continue
        header, data = det["header"], det["data"]
        sampled = [data[i]
                   for i in xlsx_table.sample_indices(len(data), k_sample)]
        # righe-titolo come righe semplici; campione arricchito "Etichetta:
        # valore" (stessa convenzione di _sheet_text). La riga HEADER NUDA
        # ("Codice a barre | SKU | Marchio | Modello") NON si manda al
        # modello: è una fila di etichette senza contesto e ci si leggono
        # nomi propri (misurato: 'Marchio' -> FULLNAME). Le etichette il
        # modello le vede dove servono davvero, cioè come prefisso delle
        # coppie del campione, dove sono contesto e non candidati.
        lines = [" | ".join(t for _c, t, _s in cells)
                 for _ri, cells in rows[:det["header_pos"]]]
        hay_texts.extend(lines)
        for _ri, cells in sampled:
            lines.append(" | ".join(
                f"{header[c]}: {t}" if c in header else t for c, t, _s in cells))
        stray = [t for _ri, cells in data
                 for c, t, _s in cells if c not in header]
        if stray:               # celle fuori dalle colonne (<=5%): al modello
            lines.append(" | ".join(stray))
            hay_texts.extend(stray)
        lines.extend(_sheet_extra_lines(root))
        texts.append("\n".join(lines))
        plans.append({"sheet": sheet_name or part, "header": header,
                      "data": data, "sampled": sampled})
    aux = _aux_texts(zf, names)
    texts.extend(aux)
    hay_texts.extend(aux)
    text = "\n".join(t for t in texts if t.strip())
    images = image_ocr.ooxml_images(xlsx_bytes) if ocr else []
    if not text.strip() and not images:
        raise XlsxError("Il file Excel non contiene testo.")

    # fasi note solo ORA: il percorso tabellare ha un secondo pass del modello
    # sui valori unici delle colonne a testo libero, quello classico no
    ocr_phase = ["image_ocr"] if images else []
    if plans:
        ctl.phases(ocr_phase + ["analysis_sample", "analysis_columns",
                                "redaction", "preview"])
    else:
        ctl.phases(ocr_phase + ["analysis", "redaction", "preview"])
    cache = None
    if images:
        ctl.phase("image_ocr")
        cache = image_ocr.build_cache(images, ctl=ctl)
    if not text.strip() and cache is None:
        raise XlsxError("Il file Excel non contiene testo e l'OCR non ha "
                        "letto testo nelle immagini.")
    ctl.phase("analysis_sample" if plans else "analysis")

    n_chunks = len(chunk_text(text))
    if max_chunks and n_chunks > max_chunks:
        raise _chunk_cap_error(n_chunks, max_chunks)
    res = image_ocr.analyze_with_corpus(engine, text, cache, excluded=excluded,
                                        custom_terms=custom_terms, ctl=ctl)

    # 2) mappa combinata: modello + colonne dei fogli tabellari
    mapping = res["mapping"].copy()
    # la mappa combinata È quella consegnata a valle (stesso oggetto): senza
    # questo, un'uscita anticipata (allow_empty) restituirebbe l'analisi con
    # dentro ancora le intestazioni scartate qui sotto
    res["mapping"] = mapping
    # l'allocatore si costruisce PRIMA di scartare le intestazioni: i suoi
    # contatori devono tener conto anche dei placeholder scartati (vedi
    # PlaceholderAllocator.forget)
    alloc = xlsx_table.PlaceholderAllocator(mapping)
    for plan in plans:                  # unici di colonna: al filtro e al punto 2
        ctl.check()
        plan["uniques"] = xlsx_table.column_uniques(plan["data"], plan["header"])

    # 2a) intestazioni di colonna taggate per errore: fuori dalla mappa, non
    #     si redigono affatto (vedi _header_only_phs)
    header_kept = []
    if plans:
        labels = {_norm_value(lab) for plan in plans
                  for lab in plan["header"].values()}
        hay_values = [v for plan in plans
                      for vals in plan["uniques"].values() for v in vals]
        for ph, val in sorted(_header_only_phs(mapping, labels, hay_values,
                                               hay_texts).items()):
            alloc.forget(ph)
            header_kept.append({"placeholder": ph, "value": val})
        if header_kept:
            dropped = {_norm_value(k["value"]) for k in header_kept}
            res["entities"] = [e for e in res["entities"]
                               if _norm_value(e["value"]) not in dropped]
            # nei progetti/chat il modello è avvolto dal registro, che ha già
            # scritto l'entità: senza questo, l'etichetta resterebbe una
            # superficie nota e verrebbe sostituita in OGNI file e turno
            # successivo dello stesso scope (parola comune, danno permanente)
            keep_in_clear = getattr(engine, "keep_in_clear", None)
            if keep_in_clear is not None:
                keep_in_clear([k["placeholder"] for k in header_kept])

    exact_phs = set()
    table_columns = []
    model_uniques = []                  # colonne "model": tutti i valori unici
    entities = [(e["label"], e["value"]) for e in res["entities"]]

    def add(label, value, whole_values):
        """Valore nella mappa globale. Diventa "exact" (sostituzione per
        uguaglianza di cella intera, vedi redact_xlsx) solo se NUOVO e
        coincidente con un intero valore-cella: ciò che il modello aveva
        già trovato mantiene il matching a sottostringa."""
        ph, created = alloc.get(label, value)
        if created and _norm_value(value) in whole_values:
            exact_phs.add(ph)

    for plan in plans:
        ctl.check()
        cls = xlsx_table.classify_columns(plan["header"], plan["sampled"],
                                          entities, coverage)
        uniques = plan["uniques"]
        regex_vals = []
        for col, (mode, tag) in sorted(cls.items()):
            vals = uniques.get(col, [])
            if not vals:
                continue
            if mode == "column":
                whole = {_norm_value(v) for v in vals}
                n = 0
                for v in vals:
                    if xlsx_table.usable_value(v):
                        add(tag, v, whole)
                        n += 1
                table_columns.append({"sheet": plan["sheet"],
                                      "column": plan["header"][col],
                                      "tag": tag, "values": n})
            elif mode == "model":
                model_uniques.extend(vals)
            else:                       # nessun segnale: rete di sicurezza
                regex_vals.extend(vals)
        if regex_vals:
            joined = "\n".join(regex_vals)
            whole = {_norm_value(v) for v in regex_vals}
            for e in detect_regex(joined):
                val = joined[e["start"]:e["end"]]
                if "\n" in val or e["label"] in excluded_set:
                    continue
                add(e["label"], val, whole)

    # 3) colonne a testo libero: il modello gira sui soli valori UNICI
    if plans:
        # la fase si dichiara anche a model_uniques vuoto ("fase 2 di 4"
        # completata all'istante): la numerazione promessa all'utente regge
        ctl.phase("analysis_columns")
    if model_uniques:
        joined = "\n".join(model_uniques)
        n2 = len(chunk_text(joined))
        if max_chunks and n_chunks + n2 > max_chunks:
            raise _chunk_cap_error(n_chunks + n2, max_chunks)
        res2 = engine.analyze(joined, excluded=excluded, custom_terms=custom_terms,
                              ctl=ctl)
        whole = {_norm_value(v) for v in model_uniques}
        for e in res2["entities"]:
            if "\n" not in e["value"]:      # a cavallo di due valori: scarto
                add(e["label"], e["value"], whole)

    if not mapping:
        if allow_empty:
            result = {
                "file": xlsx_bytes,
                "report": {"occurrences": 0, "by_placeholder": {},
                           "not_found": [], "skipped": [], "residual": [],
                           "preview_truncated": False, "table_columns": [],
                           "table_phs": [], "header_kept": header_kept},
                "original_boxes": {}, "anonymized_boxes": {},
                "analysis": res,
                "ocr_cache": cache,
            }
            return result
        raise XlsxError("Nessuna PII trovata: niente da anonimizzare in questo file.")

    result = rebuild_xlsx(xlsx_bytes, mapping, exact_phs=exact_phs, ctl=ctl,
                          make_preview=make_preview, ocr_cache=cache)

    # 4) l'analisi consegnata a valle riflette la mappa COMPLETA (è lo stesso
    #    oggetto, vedi punto 2): by_label ricalcolato dalle occorrenze redatte
    #    (come fa la ri-redazione)
    by_label = {}
    for ph, n in result["report"]["by_placeholder"].items():
        if n:
            m = xlsx_table._PH_RE.match(ph)
            lab = m.group(1) if m else ph
            by_label[lab] = by_label.get(lab, 0) + n
    res["by_label"] = dict(sorted(by_label.items(), key=lambda x: -x[1]))
    res["n_unique"] = len(mapping)
    result["report"]["table_columns"] = table_columns
    result["report"]["table_phs"] = sorted(exact_phs)
    result["report"]["header_kept"] = header_kept
    result["analysis"] = res
    result["ocr_cache"] = cache
    return result


# --------------------------------------------------------------------------- #
# Azione manuale su UNA colonna (click sull'intestazione A/B/C nella preview)
# --------------------------------------------------------------------------- #
def sheet_names(xlsx_bytes):
    """Nomi dei fogli nell'ordine del workbook (per la mappatura pagina->foglio
    della preview: il nome è stampato nell'intestazione di pagina, vedi
    _spreadsheet_look)."""
    zf, names = _open(xlsx_bytes)
    return [sn for _part, sn in _sheet_parts(zf, names) if sn]


def sheet_name_aliases(original, protected):
    """Map displayed protected sheet names to originals for local column actions."""
    oz, on = _open(original)
    pz, pn = _open(protected)
    try:
        originals = dict(_sheet_parts(oz, on))
        return {name: originals[part] for part, name in _sheet_parts(pz, pn)
                if part in originals}
    finally:
        oz.close()
        pz.close()


def column_values(xlsx_bytes, sheet_name, col_letter):
    """Valori DISTINTI (per _norm, primo testo che compare) di una colonna di
    un foglio, riga di intestazione esclusa se riconosciuta come tale.
    Ritorna (valori, etichetta_header_o_None). XlsxError se il foglio non c'è.

    A differenza del percorso tabellare automatico NON pretende una tabella
    pulita (detect_table): il click dell'utente è esplicito e vale su
    qualsiasi foglio."""
    m = re.fullmatch(r"[A-Z]{1,3}", (col_letter or "").upper())
    if not m:
        raise XlsxError(f"Colonna non valida: {col_letter!r}.")
    col = _letter_idx(m.group(0))
    zf, names = _open(xlsx_bytes)
    sst = _shared_strings(zf, names)
    for part, sn in _sheet_parts(zf, names):
        if sn == sheet_name:
            root = ET.fromstring(zf.read(part))
            break
    else:
        raise XlsxError(f"Foglio {sheet_name!r} non trovato nel file.")
    rows = _sheet_rows_idx(root, sst)
    header = _detect_header([cells for _ri, cells in rows])
    label = (header or {}).get(col)
    data = rows[1:] if header else rows
    uniques = xlsx_table.column_uniques(data, {col: label or col_letter})
    return uniques.get(col, []), label


def anonymize_xlsx_column(xlsx_bytes, sheet_name, col_letter, mapping,
                          ctl=None):
    """Anonimizzazione manuale di UNA colonna: DETERMINISTICA, senza modello.
    L'utente ha scelto la colonna a mano, quindi il trattamento non passa da
    nessuna classificazione: OGNI valore distinto usabile finisce in mappa.

      - un valore già mappato (ovunque nel documento) riusa il suo
        placeholder, qualunque sia la label — mai duplicati;
      - i valori nuovi ricevono il tag CUSTOM ([CUSTOM_n], stessi contatori
        dell'anonimizzazione manuale da selezione), con sostituzione exact
        per uguaglianza di cella intera;
      - i frammenti non trattabili in sicurezza (usable_value) si saltano e
        si contano in `skipped`.

    Ritorna {tag, new_placeholders, existing, exact_new, table_column,
    n_values, skipped}: la ri-redazione con la mappa aggiornata è compito
    del chiamante (project_files.process_column)."""
    from .progress import NULL as _NULL_CTL
    ctl = ctl or _NULL_CTL

    vals, label = column_values(xlsx_bytes, sheet_name, col_letter)
    if not vals:
        raise XlsxError(f"La colonna {col_letter} di «{sheet_name}» "
                        f"non contiene valori.")

    # Redazione/Anteprima sono le fasi di rebuild_xlsx, dichiarate qui perché
    # il chiamante le veda come un unico avanzamento del job
    ctl.phases(["analysis_values", "redaction", "preview"])
    ctl.phase("analysis_values")

    alloc = xlsx_table.PlaceholderAllocator(mapping)
    new_phs, exact_new = [], set()
    existing = 0                        # valori della colonna GIÀ in mappa
    skipped, n = 0, 0
    for v in vals:
        if not xlsx_table.usable_value(v):
            skipped += 1
            continue
        n += 1
        ph, created = alloc.get("CUSTOM", v)
        if created:
            new_phs.append(ph)
            # exact = sostituzione per uguaglianza di cella intera (vedi
            # redact_xlsx): questi valori SONO celle intere per costruzione
            exact_new.add(ph)
        else:
            existing += 1

    table_column = {"sheet": sheet_name,
                    "column": label or f"colonna {col_letter}",
                    "tag": "CUSTOM", "values": n}
    return {"tag": "CUSTOM", "new_placeholders": new_phs,
            "existing": existing, "exact_new": exact_new,
            "table_column": table_column, "n_values": len(vals),
            "skipped": skipped}


def residual_text(xlsx_bytes):
    """All supported file surfaces, including package metadata and relationships."""
    try:
        text = extract_text(xlsx_bytes, full=True)
    except XlsxError:
        text = ""
    zf = zipfile.ZipFile(io.BytesIO(xlsx_bytes))
    extra = []
    for name in zf.namelist():
        if name in ("docProps/core.xml", "docProps/app.xml") or name.endswith(".rels"):
            extra.append(_meta_surfaces(zf.read(name)))
        elif _DRAWING_RE.match(name) or _CHART_RE.match(name):
            try:
                extra.extend(_alt_text_surfaces(ET.fromstring(zf.read(name))))
            except ET.ParseError:
                continue
    zf.close()
    return text + "\n" + "\n".join(extra)


def _verify_residuals(xlsx_bytes, items, exact=None):
    """Placeholder il cui valore è ANCORA leggibile nell'output: testo
    completo (full=True copre anche attributi, cache pivot, filtri, autori) +
    metadati + rels. Deve essere [].

    Whole-cell table values also use an indexed search for embedded matches.
    This preserves the same privacy semantics as the final chat guard without
    testing every column value against every cell. Short values retain their
    explicit whole-cell checks."""
    haystack = residual_text(xlsx_bytes)
    all_items = list(items)
    if getattr(exact, "index", None) is not None:
        all_items.extend(exact.index.items)
    residual = []
    for ph, val, pat in ValueIndex(all_items).candidates(haystack):
        if pat.search(haystack) or contains_literal(haystack, val, ph):
            residual.append(ph)
    if exact:
        segments = {_norm_value(seg) for line in haystack.split("\n")
                    for seg in line.split(" | ")}
        for nv, ph in exact.items():
            if nv in segments:
                residual.append(ph)
    return residual


def known_xlsx_leaks(data, mapping):
    """Known protected surfaces in the complete workbook, without preview limits."""
    text = residual_text(data)
    selected = mapping.for_text(text) if hasattr(mapping, "for_text") else mapping
    return sorted(set(ValueIndex(selected.items()).matches(text)))

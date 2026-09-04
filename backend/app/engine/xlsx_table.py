"""
Percorso VELOCE per i fogli Excel in forma tabellare pulita.

Il collo di bottiglia degli xlsx enormi è l'ANALISI del modello (~0.75s a
chunk: 200k righe = ore), non la redazione. Se un foglio è una tabella pulita
(header + colonne omogenee) il modello gira solo su un CAMPIONE di righe; le
colonne in cui il campione risulta PII vengono poi anonimizzate in modo
deterministico: unique dei valori della colonna -> un placeholder per valore,
con lo STESSO schema di tag e contatori del modello ([FULLNAME_7], mai
categorie nuove). Le colonne non marcate passano comunque dalla rete
regex/checksum (rete di sicurezza), quelle con PII "dentro" testo libero
tornano al modello ma sui soli valori unici.

Questo modulo contiene la LOGICA PURA (niente XML, niente zip): lavora su
righe già estratte da xlsx.py, così è testabile in isolamento e non crea
import circolari. Strutture dati condivise con xlsx.py:

  riga    = (numero_riga_excel_1based, [(col_0based, testo, è_testo), ...])
            con sole celle NON vuote (come _sheet_rows);
  merges  = [(r1, c1, r2, c2), ...] delle celle unite (righe 1-based,
            colonne 0-based, estremi inclusi).

La rilevazione "tabellare" è una checklist di condizioni DURE, non uno score:
un falso positivo qui costa PII in chiaro, un falso negativo costa solo
lentezza (si ricade sul percorso classico). Sbagliare in modo conservativo.
"""

import re

from .core import _norm
from .pdf_export import _too_noisy, _value_pattern

# quante righe iniziali possono precedere l'header (titoli, loghi, note:
# tipicamente celle unite o singole). Oltre, il foglio non è "pulito".
_MAX_TITLE_ROWS = 5
# una riga-titolo sopra l'header può avere al massimo queste celle
_TITLE_MAX_CELLS = 2
# buco massimo tra due righe dati consecutive (2 = al più UNA riga vuota)
_MAX_ROW_GAP = 2
# quota minima di righe dati che usano SOLO le colonne dell'header
_MIN_RECT_RATIO = 0.95
# quota minima del tipo dominante (testo vs numero) in una colonna
_MIN_TYPE_RATIO = 0.80
# sotto questo numero di valori DISTINTI campionati una colonna non può
# essere dichiarata "atomica": troppo pochi per fidarsi ("SI/NO/N/A" non è
# una colonna PII), si ripiega sul modello (che su pochi unici costa nulla)
_MIN_DISTINCT_FOR_COLUMN = 5
# quota minima di valori distinti con un hit perché una colonna vada al
# percorso "modello sugli unici": sotto, 2-3 hit su 50 sono indistinguibili
# dal rumore del modello e manderebbero al modello colonne enormi di testo
# libero non-PII: su una colonna di descrizioni prodotto con 40k valori unici
# bastano 4 hit spuri su 50 campionati per farne ore di modello. Sotto la
# soglia la colonna esce come "regex" e resta comunque coperta dalla rete
# regex/checksum, che non costa nulla.
_MIN_MODEL_SHARE = 0.20
# lunghezza minima (alfanumerica) di un valore-entità per la ricerca di
# match PARZIALI dentro celle più lunghe (sotto, troppi falsi positivi)
_MIN_SUBSTR_LEN = 4


# --------------------------------------------------------------------------- #
# Rilevazione: il foglio è una tabella pulita?
# --------------------------------------------------------------------------- #
def _is_header_row(cells):
    """Header severo (a differenza di xlsx._detect_header, qui sbagliare costa
    caro): >=2 celle, tutte TESTUALI, corte, senza a-capo né duplicati."""
    if len(cells) < 2:
        return False
    vals = []
    for _col, txt, is_text in cells:
        v = txt.strip()
        if not is_text or not v or len(v) > 60 or "\n" in v:
            return False
        vals.append(v.casefold())
    return len(set(vals)) == len(vals)


def detect_table(rows, merges, min_rows):
    """Checklist dura. Ritorna un dict con:
       tabular    True/False
       reason     motivo (per log/debug, mai mostrato all'utente)
       header     {col: etichetta}          (solo se tabular)
       header_pos indice della riga header dentro `rows`
       data       righe dati (rows dopo l'header)
    """
    no = lambda why: {"tabular": False, "reason": why}
    if not min_rows or min_rows <= 0:
        return no("percorso disattivato (min_rows=0)")
    if len(rows) < min_rows:
        return no("foglio piccolo: il modello è comunque veloce")

    # 1) header: prima riga che lo sembra, entro le prime _MAX_TITLE_ROWS;
    #    le righe sopra devono essere "da titolo" (poche celle)
    header_pos = None
    for i, (_ri, cells) in enumerate(rows[:_MAX_TITLE_ROWS]):
        if _is_header_row(cells):
            header_pos = i
            break
        if len(cells) > _TITLE_MAX_CELLS:
            return no("righe iniziali non riconducibili a titolo+header")
    if header_pos is None:
        return no("nessuna riga header riconosciuta")
    header_rownum = rows[header_pos][0]
    header = {col: txt.strip() for col, txt, _ in rows[header_pos][1]}
    data = rows[header_pos + 1:]
    if len(data) < min_rows:
        return no("poche righe dati sotto l'header")

    # 2) celle unite: ammesse SOLO sopra l'header (titolo del foglio);
    #    una merge nell'header o nei dati è il marcatore dei layout "report"
    for _r1, _c1, r2, _c2 in merges:
        if r2 >= header_rownum:
            return no("celle unite nell'area dati")

    # 3) blocco unico: niente buchi (due tabelle impilate non sono "pulite")
    for (ra, _), (rb, _) in zip(data, data[1:]):
        if rb - ra > _MAX_ROW_GAP:
            return no("righe dati non contigue (più blocchi?)")

    # 4) rettangolarità: le righe dati usano (quasi) solo le colonne header
    hcols = set(header)
    inside = sum(1 for _ri, cells in data
                 if all(c in hcols for c, _t, _s in cells))
    if inside / len(data) < _MIN_RECT_RATIO:
        return no("troppe celle fuori dalle colonne dell'header")

    # 5) coerenza di tipo per colonna (testo vs numero); colonne quasi vuote
    #    esentate: troppo poco materiale per giudicare
    per_col = {}
    for _ri, cells in data:
        for c, _t, is_text in cells:
            if c in hcols:
                n_txt, n_tot = per_col.get(c, (0, 0))
                per_col[c] = (n_txt + (1 if is_text else 0), n_tot + 1)
    for c, (n_txt, n_tot) in per_col.items():
        if n_tot >= 10 and max(n_txt, n_tot - n_txt) / n_tot < _MIN_TYPE_RATIO:
            return no(f"colonna {header.get(c, c)!r} con tipi misti")

    return {"tabular": True, "reason": "ok", "header": header,
            "header_pos": header_pos, "data": data}


# --------------------------------------------------------------------------- #
# Campionamento
# --------------------------------------------------------------------------- #
def sample_indices(n, k):
    """k indici su n righe: metà in testa (le prime righe spesso hanno i casi
    "storici"), il resto a passo regolare fino all'ULTIMA riga inclusa. Le
    prime righe da sole non bastano: una colonna può cambiare natura a metà
    foglio (ordinamenti, import concatenati)."""
    if n <= k:
        return list(range(n))
    head = k // 2
    idxs = list(range(head))
    rest = k - head
    span = n - head - 1
    idxs.extend(head + round(i * span / (rest - 1)) for i in range(rest))
    # il round può produrre duplicati adiacenti: dedup mantenendo l'ordine
    seen, out = set(), []
    for i in idxs:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


# --------------------------------------------------------------------------- #
# Classificazione delle colonne dal campione analizzato
# --------------------------------------------------------------------------- #
def classify_columns(header, sampled, entities, coverage_pct):
    """Per ogni colonna dell'header decide il trattamento:

      ("column", TAG)  colonna ATOMICA: >= coverage_pct% dei valori campionati
                       coincide per intero con un'entità del modello ->
                       l'intera colonna si anonimizza deterministicamente col
                       TAG più frequente (stessi tag del modello);
      ("model", None)  PII presente ma dentro testo più lungo, o troppo
                       sparsa: al modello vanno i soli valori UNICI;
      ("regex", None)  segnale assente o sotto la soglia di rumore: rete
                       regex/checksum sugli unici come rete di sicurezza.

    Le quote si misurano sui valori DISTINTI del campione, non sulle celle:
    il matching è per valore, e in una colonna a bassa cardinalità ("Pack"
    = 5/30/90) un solo numero taggato altrove renderebbe hit TUTTE le celle
    uguali, gonfiando la copertura in cascata.

    `entities` = [(label, valore)] dall'analisi del campione."""
    full_map = {}                     # valore normalizzato -> label
    for label, value in entities:
        full_map.setdefault(_norm(value), label)
    subs = [(nv, label) for nv, label in full_map.items()
            if len(re.sub(r"[\W_]+", "", nv)) >= _MIN_SUBSTR_LEN]

    distinct = {c: set() for c in header}
    for _ri, cells in sampled:
        for c, txt, _is_text in cells:
            if c in distinct:
                distinct[c].add(_norm(txt))

    out = {}
    for c, vals in distinct.items():
        full, partial = {}, 0
        for nv in vals:
            lab = full_map.get(nv)
            if lab is not None:
                full[lab] = full.get(lab, 0) + 1
            elif any(s in nv for s, _l in subs if s != nv):
                partial += 1
        n, hits = len(vals), sum(full.values())
        if (n >= _MIN_DISTINCT_FOR_COLUMN
                and hits * 100 >= n * coverage_pct
                and hits >= partial):
            tag = max(full, key=full.get)         # il count più alto vince
            out[c] = ("column", tag)
        elif hits + partial >= max(2, n * _MIN_MODEL_SHARE):
            out[c] = ("model", None)
        else:
            out[c] = ("regex", None)
    return out


def column_uniques(data, header):
    """{col: [valori unici (per _norm), nell'ordine di prima comparsa]}."""
    out = {c: [] for c in header}
    seen = {c: set() for c in header}
    for _ri, cells in data:
        for c, txt, _is_text in cells:
            if c in out:
                nv = _norm(txt)
                if nv and nv not in seen[c]:
                    seen[c].add(nv)
                    out[c].append(txt)
    return out


def usable_value(v):
    """Un valore unico entra in mappa solo se la redazione saprà trattarlo:
    i frammenti <2 caratteri alfanumerici ("X", "-", "05") si saltano, come
    fa già il percorso classico (_too_noisy)."""
    return bool(v and v.strip()) and not _too_noisy(v) and _value_pattern(v) is not None


# --------------------------------------------------------------------------- #
# Allocatore di placeholder condiviso col modello
# --------------------------------------------------------------------------- #
_PH_RE = re.compile(r"^\[(.+)_(\d+)\]$")


class PlaceholderAllocator:
    """Prosegue i contatori della mappa del modello ([FULLNAME_3] -> il
    prossimo FULLNAME è _4) e deduplica per VALORE normalizzato: lo stesso
    valore, ovunque compaia (colonna, modello, altra colonna), ha lo stesso
    placeholder — la mappa resta reversibile. La mappa passata viene
    aggiornata in place."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.counters = {}
        self.by_value = {}
        for ph, val in mapping.items():
            m = _PH_RE.match(ph)
            if m:
                label, n = m.group(1), int(m.group(2))
                self.counters[label] = max(self.counters.get(label, 0), n)
            self.by_value.setdefault(_norm(val), ph)

    def forget(self, ph):
        """Toglie un placeholder dalla mappa e ritorna il suo valore (None se
        non c'era). Il CONTATORE della label non torna indietro: il numero non
        va riusato, perché nel registro di un progetto quel placeholder può
        continuare a esistere (marcato "in chiaro") e due entità con lo stesso
        [TAG_n] renderebbero ambigua la deanonimizzazione."""
        value = self.mapping.pop(ph, None)
        if value is not None:
            nv = _norm(value)
            if self.by_value.get(nv) == ph:
                del self.by_value[nv]
        return value

    def get(self, label, value):
        """(placeholder, creato_adesso). Riusa quello esistente se il valore
        è già in mappa (anche sotto un'altra label: una stringa è una)."""
        nv = _norm(value)
        ph = self.by_value.get(nv)
        if ph is not None:
            return ph, False
        self.counters[label] = self.counters.get(label, 0) + 1
        ph = f"[{label}_{self.counters[label]}]"
        self.mapping[ph] = value
        self.by_value[nv] = ph
        return ph, True

"""
Redazione dei documenti Word (.docx): l'equivalente di pdf_export per l'XML.

Un .docx è uno ZIP di XML; il testo sta nei nodi <w:t> dentro i run <w:r>, e
Word SPEZZA i valori su più run anche senza motivo apparente. La strategia è
la stessa dell'indice char-preciso del PDF: si linearizza il testo di ogni part
tenendo, per ogni carattere, il nodo <w:t> di provenienza; i valori della mappa
si cercano con le stesse regex ancorate di pdf_export (confini di parola, spazi
flessibili) e la sostituzione attraversa i run: il placeholder diventa un run
NUOVO con evidenziazione GIALLA nativa (w:highlight), il resto del match viene
rimosso dai run successivi. Essendo testo fluido, il layout si riadatta da solo.

Cosa viene ripulito oltre al corpo del documento (posti in cui la PII
sopravviverebbe):
  - intestazioni e piè di pagina, note a piè di pagina e di chiusura;
  - COMMENTI (stesso giro di redazione del corpo);
  - REVISIONI (track changes): le cancellazioni <w:del> vengono rimosse e le
    inserzioni <w:ins> mantenute -> equivalente ad "accetta tutte le revisioni",
    così il testo analizzato è quello che il lettore vede davvero;
  - metadati (docProps/core.xml): autore, ultimo autore, titolo, oggetto, ...;
  - docProps/app.xml: TitlesOfParts (il titolo del documento) più Company e
    Manager, che l'installazione aziendale di Office riempie da sola;
  - TARGET degli hyperlink esterni (stanno nei .rels, non nel testo): se
    contengono un valore mappato vengono sostituiti con about:blank.

Rete di sicurezza finale (contratto comune, vedi engine/__init__.py):
report["residual"] rilegge l'output — testo, metadati e rels — e deve essere
vuota.

Testo negli ATTRIBUTI (svuotato, non sostituito — vedi _scrub_alt_text e
_scrub_authors): il testo alternativo e il titolo di immagini/forme
(wp:docPr e cNvPr @descr/@title), l'autore e le iniziali di commenti e
revisioni (w:*/@w:author, @w:initials) e word/people.xml. Non passano per
w:t, quindi la redazione per valore non li vede; e un nome che sta SOLO lì
(chi ha commentato) non è nella mappa. Azzerarli sempre copre entrambi i
casi, non richiede ripristino e rende il controllo residui banale. Il testo
DENTRO le immagini è coperto
solo con ocr=True (vedi anonymize_docx). Questo modulo tratta i soli .docx: i
.doc binari si convertono in .docx all'ingresso (engine/convert.py).

Solo stdlib (zipfile + xml.etree): niente dipendenze native.
"""

import copy
import io
import re
import zipfile
import xml.etree.ElementTree as ET

from . import image_ocr
from .pdf_export import _too_noisy, _value_pattern

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = "{%s}" % W_NS

# prefissi standard OOXML: registrarli evita che ElementTree riscriva tutto
# con ns0:/ns1: (valido ma inutilmente diverso dall'originale)
_NAMESPACES = {
    "w": W_NS,
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
for _p, _u in _NAMESPACES.items():
    ET.register_namespace(_p, _u)
# i .rels usano il namespace SENZA prefisso: serializzarli con ns0: è XML valido
# ma i parser OOXML (Word, LibreOffice) rifiutano il package
ET.register_namespace("", "http://schemas.openxmlformats.org/package/2006/relationships")

_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# part XML in cui il testo va analizzato E redatto
_TEXT_PART_RE = re.compile(
    r"^word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$")

# NB: le proprietà di docProps/core.xml da svuotare NON sono un elenco a sé
# (lo erano, ed è stata una falla: vedi _scrub_core_props). Si ricavano per
# differenza da _META_BOILERPLATE, l'elenco che usa il verificatore dei
# residui.


class DocxError(ValueError):
    """Errore d'uso (file non valido, niente testo, dizionario vuoto...)."""


# --------------------------------------------------------------------------- #
# Caricamento + linearizzazione char-precisa
# --------------------------------------------------------------------------- #
def _scrub_revisions(root):
    """Accetta le revisioni: via i <w:del> (testo cancellato), restano i <w:ins>.
    Fatto PRIMA di estrarre il testo, così analisi e redazione vedono lo stesso
    documento che vede il lettore."""
    parents = {c: p for p in root.iter() for c in p}
    for d in list(root.iter(W + "del")):
        p = parents.get(d)
        if p is not None:
            p.remove(d)


# Attributi che portano testo libero fuori da w:t. Condivisi con pptx/xlsx:
# il markup DrawingML (cNvPr) è lo stesso nei tre formati.
_ALT_TEXT_TAGS = frozenset(("docPr", "cNvPr"))
_ALT_TEXT_ATTRS = ("descr", "title")
_AUTHOR_ATTRS = (W + "author", W + "initials")
_PEOPLE_PART = "word/people.xml"


def _scrub_alt_text(root):
    """Svuota testo alternativo e titolo di immagini e forme. Ritorna il
    numero di attributi azzerati."""
    n = 0
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] not in _ALT_TEXT_TAGS:
            continue
        for attr in _ALT_TEXT_ATTRS:
            if el.get(attr):
                el.set(attr, "")
                n += 1
    return n


def _alt_text_surfaces(root):
    """I valori che _scrub_alt_text azzera: per la verifica dei residui."""
    return [el.get(attr) for el in root.iter()
            if el.tag.rsplit("}", 1)[-1] in _ALT_TEXT_TAGS
            for attr in _ALT_TEXT_ATTRS if el.get(attr)]


def _scrub_authors(root):
    """Svuota autore e iniziali di commenti e revisioni (w:comment, w:ins,
    w:rPrChange...). Ritorna il numero di attributi azzerati."""
    n = 0
    for el in root.iter():
        for attr in _AUTHOR_ATTRS:
            if el.get(attr):
                el.set(attr, "")
                n += 1
    return n


def _author_surfaces(root):
    return [el.get(attr) for el in root.iter()
            for attr in _AUTHOR_ATTRS if el.get(attr)]


def _scrub_people(xml_bytes):
    """word/people.xml: l'elenco degli autori di commenti e revisioni
    (w15:person/@w15:author, presenceInfo/@userId). Tutti attributi."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return xml_bytes
    n = 0
    for el in root.iter():
        for attr in list(el.attrib):
            if el.attrib[attr] and \
                    attr.rsplit("}", 1)[-1] in ("author", "userId"):
                el.set(attr, "")
                n += 1
    if n == 0:
        return xml_bytes
    return _serialize_part(root, _part_namespaces(xml_bytes))


def _walk_part(root):
    """(testo linearizzato, [[start, end, nodo_w:t], ...]) di una part.

    I separatori strutturali (fine paragrafo, tab, break) diventano \\n/\\t
    "virtuali": occupano una posizione nel testo ma non appartengono a nessun
    nodo, quindi non possono essere redatti per sbaglio."""
    chunks, segs = [], []
    pos = 0

    def rec(el):
        nonlocal pos
        tag = el.tag
        if tag == W + "t":
            s = el.text or ""
            if s:
                segs.append([pos, pos + len(s), el])
                chunks.append(s)
                pos += len(s)
            return
        if tag == W + "tab":
            chunks.append("\t"); pos += 1
            return
        if tag in (W + "br", W + "cr"):
            chunks.append("\n"); pos += 1
            return
        if tag in (W + "delText", W + "instrText"):
            return                      # cancellato / codice di campo
        for ch in el:
            rec(ch)
        if tag == W + "p":
            chunks.append("\n"); pos += 1

    rec(root)
    return "".join(chunks), segs


_XMLNS_RE = re.compile(rb'xmlns:([\w.-]+)="([^"]+)"')
_XMLNS_DEFAULT_RE = re.compile(rb'xmlns="([^"]+)"')


def _part_namespaces(xml_bytes):
    """{prefisso: uri} dichiarati nella part (chiave "" = namespace di
    DEFAULT, usato dai package spreadsheet). Word dichiara sulla radice DECINE
    di namespace (w15, w16*, wp14, ...) e li cita in mc:Ignorable anche quando
    nel documento non compaiono mai: se la riserializzazione perde quelle
    dichiarazioni, mc:Ignorable riferisce prefissi inesistenti e Word rifiuta
    il file ("unreadable content"). I prefissi si leggono dal file stesso:
    nessuna lista mantenuta a mano."""
    decls = {p.decode(): u.decode() for p, u in _XMLNS_RE.findall(xml_bytes)}
    m = _XMLNS_DEFAULT_RE.search(xml_bytes)
    if m:
        decls[""] = m.group(1).decode()
    return decls


def _serialize_part(root, decls):
    """ET.tostring che PRESERVA le dichiarazioni di namespace originali:
    - per i namespace usati nel tree si registra il prefisso originale
      (niente ns0:), ci pensa ElementTree a dichiararli;
    - quelli NON usati vengono rimessi sulla radice come attributi letterali
      (ET dichiarerebbe solo i primi, ma mc:Ignorable cita anche gli altri).
    La registrazione avviene a OGNI chiamata: il registro di ElementTree tiene
    un solo namespace per prefisso, e il prefisso vuoto (default) è conteso
    tra part di tipo diverso (.rels, content types, spreadsheet...)."""
    used = set()
    for el in root.iter():
        if el.tag.startswith("{"):
            used.add(el.tag[1:].split("}", 1)[0])
        for k in el.attrib:
            if k.startswith("{"):
                used.add(k[1:].split("}", 1)[0])
    for p, u in decls.items():
        if u in used:
            ET.register_namespace(p, u)
        elif p and f"xmlns:{p}" not in root.attrib:
            root.set(f"xmlns:{p}", u)
        elif not p and "xmlns" not in root.attrib:
            root.set("xmlns", u)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _load_parts(docx_bytes):
    """{nome_part: (root, namespaces)} delle part testuali, revisioni accettate."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(docx_bytes))
        names = zf.namelist()
    except zipfile.BadZipFile:
        raise DocxError("File .docx non valido o danneggiato.")
    if "word/document.xml" not in names:
        raise DocxError("Non sembra un documento Word (.docx).")
    parts = {}
    for name in names:
        if _TEXT_PART_RE.match(name):
            data = zf.read(name)
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                raise DocxError(f"XML non valido dentro il docx ({name}).")
            _scrub_revisions(root)
            parts[name] = (root, _part_namespaces(data))
    return parts


def extract_text(docx_bytes, allow_empty=False):
    """Testo completo del docx (corpo + intestazioni/piè pagina + note + commenti).
    DocxError se non c'è testo (con allow_empty=True il vuoto è ammesso:
    percorso OCR, documento fatto di sole immagini)."""
    parts = _load_parts(docx_bytes)
    texts = []
    for name in sorted(parts):                    # document.xml prima, ordine stabile
        t, _ = _walk_part(parts[name][0])
        if t.strip():
            texts.append(t)
    text = "\n".join(texts)
    if not text.strip() and not allow_empty:
        raise DocxError("Il documento Word non contiene testo.")
    return text


# --------------------------------------------------------------------------- #
# Sostituzione attraverso i run
# --------------------------------------------------------------------------- #
def _set_text(t_el, s):
    t_el.text = s
    if s != s.strip():
        t_el.set(_XML_SPACE, "preserve")


# figli di w:rPr che nello schema OOXML (CT_RPr, sequenza obbligata) vengono
# DOPO w:highlight: l'evidenziazione va inserita prima di questi, appenderla
# in fondo produce un file che Word considera danneggiato
_RPR_AFTER_HIGHLIGHT = {W + t for t in (
    "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em",
    "lang", "eastAsianLayout", "specVanish", "oMath", "rPrChange")}


def _make_ph_run(model_run, ph):
    """Run nuovo per il placeholder: eredita la formattazione del run colpito
    (deepcopy del suo rPr) + evidenziazione GIALLA nativa di Word.

    Un highlight PREESISTENTE del documento non viene sostituito: è un segno
    dell'utente e deve sopravvivere al ciclo redazione -> ripristino. Il
    ripristino (chat_anonymization) toglie SOLO il giallo dai run che
    contengono un placeholder: quel giallo può essere solo nostro, ogni
    altro colore resta com'era. L'unico caso perso è un highlight giallo
    dell'utente esattamente sopra una PII (indistinguibile dal nostro)."""
    run = ET.Element(W + "r")
    rpr = model_run.find(W + "rPr")
    rpr = copy.deepcopy(rpr) if rpr is not None else ET.Element(W + "rPr")
    if rpr.find(W + "highlight") is None:
        hl = ET.Element(W + "highlight")
        hl.set(W + "val", "yellow")
        idx = next((i for i, ch in enumerate(rpr)
                    if ch.tag in _RPR_AFTER_HIGHLIGHT), len(rpr))
        rpr.insert(idx, hl)
    run.append(rpr)
    t = ET.SubElement(run, W + "t")
    _set_text(t, ph)
    return run


def _replace_match(segs, parents, ms, me, ph):
    """Applica UNA sostituzione [ms, me) -> ph sui nodi w:t coinvolti.

    Le sostituzioni vengono applicate in ordine DECRESCENTE di offset, così i
    prefissi dei nodi già toccati restano validi per i match precedenti."""
    hit = [s for s in segs if s[0] < me and s[1] > ms]
    if not hit:
        return False
    first = hit[0]
    t1 = first[2]
    run1 = parents[t1]                       # il parent di w:t è sempre w:r
    holder = parents[run1]                   # w:p / w:ins / w:hyperlink / ...
    text1 = t1.text or ""
    ls = ms - first[0]                       # offset locali nel primo nodo
    le_first = min(me, first[1]) - first[0]

    # coda del match dentro il primo nodo (match tutto in un t) o nell'ultimo
    after = ""
    if me <= first[1]:
        after = text1[le_first:]
    else:
        for seg in hit[1:]:
            t = seg[2]
            s = t.text or ""
            cut_end = min(me, seg[1]) - seg[0]
            _set_text(t, s[cut_end:] if me < seg[1] else "")
            if me < seg[1]:
                break

    _set_text(t1, text1[:ls])
    idx = list(holder).index(run1)
    ph_run = _make_ph_run(run1, ph)
    holder.insert(idx + 1, ph_run)
    if after:
        tail_run = copy.deepcopy(run1)       # stessa formattazione, senza highlight
        for t in tail_run.findall(W + "t"):
            tail_run.remove(t)
        t = ET.SubElement(tail_run, W + "t")
        _set_text(t, after)
        holder.insert(idx + 2, tail_run)
    return True


def _redact_tree(root, usable, claimed_report):
    """Redige una part: match sul testo linearizzato, sostituzioni destra->sinistra."""
    text, segs = _walk_part(root)
    if not text.strip():
        return
    parents = {c: p for p in root.iter() for c in p}
    matches = []                             # (start, end, ph)
    claimed = []
    for ph, _val, pat in usable:
        for m in pat.finditer(text):
            ms, me = m.start(), m.end()
            if any(ms < ce and me > cs for cs, ce in claimed):
                continue                     # già coperto da un valore più lungo
            claimed.append((ms, me))
            matches.append((ms, me, ph))
    for ms, me, ph in sorted(matches, key=lambda x: -x[0]):
        if _replace_match(segs, parents, ms, me, ph):
            claimed_report[ph] = claimed_report.get(ph, 0) + 1


# --------------------------------------------------------------------------- #
# Metadati + hyperlink
# --------------------------------------------------------------------------- #
def _scrub_core_props(xml_bytes):
    """docProps/core.xml: si svuota TUTTO tranne ciò che l'applicazione
    rigenera da sé (date del salvataggio, numero di revisione).

    L'elenco è NEGATIVO di proposito. Con l'elenco positivo che c'era prima —
    creator, title, subject, description, lastModifiedBy, keywords — bastava
    una proprietà in più nel file per aprire una falla muta: `cp:category`,
    che Office riempie dal campo «Categorie», non veniva svuotata ma
    `_meta_surfaces` la LEGGE (non è in _META_BOILERPLATE). Risultato: la
    verifica dei residui trovava il valore, dichiarava il file non protetto
    e nessuna redazione poteva più farlo passare — il file era irrecuperabile.

    Legare i due metri allo stesso insieme rende la deriva impossibile per
    costruzione: tutto ciò che il verificatore controlla, qui si svuota.
    Condivisa dai tre formati OOXML (docx, xlsx, pptx): stessa part, stesso
    schema."""
    root = ET.fromstring(xml_bytes)
    for el in root.iter():
        if el is root or el.tag.rsplit("}", 1)[-1] in _META_BOILERPLATE:
            continue
        if (el.text or "").strip():
            el.text = ""
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _scrub_app_props(xml_bytes):
    """docProps/app.xml: TitlesOfParts elenca i titoli delle parti — titolo del
    documento nel docx, TITOLI DELLE SLIDE nel pptx, nomi dei FOGLI nell'xlsx —
    dove la PII sopravviverebbe anche a corpo redatto; più Company e Manager,
    che l'installazione aziendale di Office riempie da sola.

    Sono valori DERIVATI, che l'applicazione ricalcola al salvataggio: si
    svuotano senza toccare la struttura (il size dei vt:vector resta quello che
    era). Condivisa dai tre formati OOXML: è la stessa part con lo stesso
    schema."""
    root = ET.fromstring(xml_bytes)
    n = 0
    for el in root.iter():
        local = el.tag.rsplit("}", 1)[-1]
        if local in ("lpstr", "Company", "Manager") and (el.text or "").strip():
            el.text = ""
            n += 1
    if n == 0:
        return xml_bytes
    return _serialize_part(root, _part_namespaces(xml_bytes))


class _RelsValues:
    """I valori mappati nella forma che serve a `_scrub_rels`, preparati UNA
    volta per redazione: casefold e versione senza spazi costano poco a valore
    ma le part .rels di un pacchetto sono decine, e i valori possono essere
    decine di migliaia (colonne xlsx)."""

    __slots__ = ("literal", "squeezed")

    def __init__(self, values):
        self.literal = [v.casefold() for v in values if len(v.strip()) >= 4]
        # solo i valori con spazi: per gli altri la forma compressa è il
        # valore stesso, già coperto dal confronto letterale
        self.squeezed = {re.sub(r"\s+", "", v) for v in self.literal
                         if any(c.isspace() for c in v)}
        self.squeezed.discard("")

    def hits(self, target):
        t = target.casefold()
        if any(v in t for v in self.literal):
            return True
        if not self.squeezed:
            return False
        tight = re.sub(r"\s+", "", t)
        return any(v in tight for v in self.squeezed)


def _scrub_rels(xml_bytes, values):
    """Target esterni (hyperlink) che contengono un valore mappato -> about:blank.
    Se non c'è niente da cambiare i bytes restano gli ORIGINALI: meno si
    riserializza, meno si rischia di scontentare un parser OOXML.

    Il confronto si fa DUE volte, sul target così com'è e sul target senza
    spazi, contro il valore senza spazi. La seconda passata non è un di più:
    la verifica dei residui usa `_value_pattern`, che unisce i token con
    whitespace flessibile e quindi riconosce un valore con spazi anche scritto
    tutto attaccato — "Acme Analytics" dentro `instagram.com/acmeanalytics/`.
    Se il verificatore sa vedere una superficie che lo scrubber non sa
    togliere, il file viene RIFIUTATO e l'utente non ha modo di farlo passare:
    i due metri devono coincidere.

    Sottostringa e non regex: qui i valori possono essere decine di migliaia
    (colonne xlsx anonimizzate per intero) e questa funzione gira su OGNI part
    .rels — compilare un pattern a valore per part costerebbe più di tutta la
    redazione. Sul target di un hyperlink la sottostringa senza spazi copre gli
    stessi casi: la sillabazione a fine riga, l'altra tolleranza del pattern,
    in un URL non esiste.

    `values` può essere già un _RelsValues: i redattori lo preparano una
    volta e lo riusano per tutte le part (vedi la classe)."""
    prepared = values if isinstance(values, _RelsValues) else _RelsValues(values)
    root = ET.fromstring(xml_bytes)
    n = 0
    for rel in root.iter():
        if rel.get("TargetMode") != "External":
            continue
        if prepared.hits(rel.get("Target", "")):
            rel.set("Target", "about:blank")
            n += 1
    if n == 0:
        return xml_bytes, 0
    # ri-registrato a ogni chiamata: il prefisso vuoto è conteso (vedi
    # _serialize_part) e un .rels con ns0: viene RIFIUTATO dai parser OOXML
    ET.register_namespace("", "http://schemas.openxmlformats.org/package/2006/relationships")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), n


# --------------------------------------------------------------------------- #
# Superfici delle part di metadati (per la verifica dei residui)
# --------------------------------------------------------------------------- #
# Campi che scrive l'APPLICAZIONE, non l'utente: nome del programma, versione,
# contatori, timestamp del salvataggio. Nessuno scrubber li tocca (non sono
# PII e riscriverli scontenterebbe Word/PowerPoint), quindi escono dalla
# redazione identici a come sono entrati. Vanno esclusi dalla verifica dei
# residui: un valore mappato che vi compare per caso — "Microsoft" etichettato
# ORG, un anno etichettato CAP — condannerebbe il file intero senza che nessuna
# redazione possa mai farlo passare.
_META_BOILERPLATE = frozenset((
    # docProps/app.xml
    "Application", "AppVersion", "PresentationFormat", "Template",
    "DocSecurity", "ScaleCrop", "LinksUpToDate", "SharedDoc",
    "HyperlinksChanged", "TotalTime", "Pages", "Words", "Characters",
    "CharactersWithSpaces", "Lines", "Paragraphs", "Slides", "Notes",
    "HiddenSlides", "MMClips",
    # docProps/core.xml: date e numero di revisione del salvataggio
    "revision", "created", "modified", "lastPrinted",
))

# URI di schema (attributi Type dei .rels, xsi:type): contengono nomi propri
# — "schemas.microsoft.com" — che collidono con valori mappati legittimi
_SCHEMA_URI_RE = re.compile(r"^https?://(schemas\.|purl\.org/dc|www\.w3\.org)",
                            re.I)


def _meta_surfaces(xml_bytes):
    """Le stringhe di una part di metadati (core.xml, app.xml, autori dei
    commenti, .rels) che possono DAVVERO contenere PII: testo degli elementi e
    valori degli attributi, meno la scaffolding del formato.

    La verifica dei residui non può leggere l'XML grezzo di queste part: le
    dichiarazioni xmlns e gli URI di schema sono pieni di nomi propri, e un
    falso positivo lì fa scartare l'intero file senza rimedio possibile
    (nessuna redazione può togliere un namespace). Parsare l'XML elimina da
    solo le dichiarazioni xmlns e le chiavi degli attributi qualificati, che
    ElementTree non espone; qui si tolgono in più gli URI di schema e i campi
    generati dall'applicazione (_META_BOILERPLATE). Tutto il resto — autore,
    azienda, titoli delle part, nomi degli autori dei commenti, target degli
    hyperlink — resta sotto controllo."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        # part illeggibile (non dovrebbe accadere: gli scrubber la parsano
        # prima): meglio il controllo grezzo che nessun controllo
        return xml_bytes.decode("utf-8", errors="replace")
    out = []
    for el in root.iter():
        if (el.text or "").strip() and \
                el.tag.rsplit("}", 1)[-1] not in _META_BOILERPLATE:
            out.append(el.text)
        for value in el.attrib.values():
            if value.strip() and not _SCHEMA_URI_RE.match(value):
                out.append(value)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# API pubblica
# --------------------------------------------------------------------------- #
def redact_docx(docx_bytes, mapping, ocr_cache=None):
    """Docx originale -> docx con placeholder evidenziati in giallo + report.

    mapping: {"[FULLNAME_1]": "Mario Rossi", ...} (il dizionario di analyze()).
    Report con le stesse chiavi di pdf_export.redact_pdf: occurrences,
    by_placeholder, not_found, skipped, residual (+ rels riscritti).

    ocr_cache (opzionale, vedi image_ocr): le part word/media/* pianificate
    vengono riscritte con i box gialli nei PIXEL; senza cache le part media
    passano intatte.
    """
    if not isinstance(mapping, dict) or not mapping:
        raise DocxError("Dizionario vuoto: anonimizza prima il documento.")
    items = sorted(((ph, v) for ph, v in mapping.items()
                    if isinstance(ph, str) and isinstance(v, str) and v.strip()),
                   key=lambda kv: -len(kv[1]))
    if not items:
        raise DocxError("Dizionario non valido.")

    skipped, usable = [], []
    for ph, val in items:
        if _too_noisy(val):
            skipped.append(ph)
            continue
        pat = _value_pattern(val)
        if pat:
            usable.append((ph, val, pat))
        else:
            skipped.append(ph)

    parts = _load_parts(docx_bytes)
    by_ph = {}
    n_attrs = 0
    for name in sorted(parts):
        root = parts[name][0]
        _redact_tree(root, usable, by_ph)
        n_attrs += _scrub_alt_text(root) + _scrub_authors(root)
    by_ph = {ph: by_ph.get(ph, 0) for ph, _, _ in usable}

    values = _RelsValues(v for _, v, _ in usable)   # preparati una volta sola
    img_by_ph = {}
    media_redactor = image_ocr.ooxml_media_redactor(ocr_cache, mapping, img_by_ph)
    src = zipfile.ZipFile(io.BytesIO(docx_bytes))
    out_buf = io.BytesIO()
    n_rels = 0
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename in parts:
                data = _serialize_part(*parts[info.filename])
            elif info.filename == "docProps/core.xml":
                data = _scrub_core_props(data)
            elif info.filename == "docProps/app.xml":
                data = _scrub_app_props(data)
            elif info.filename == _PEOPLE_PART:
                data = _scrub_people(data)
            elif info.filename.endswith(".rels"):
                data, n = _scrub_rels(data, values)
                n_rels += n
            elif media_redactor is not None:
                data = media_redactor(info.filename, data)
            zout.writestr(info.filename, data)
    out = out_buf.getvalue()

    report = {
        "occurrences": sum(by_ph.values()),
        "by_placeholder": by_ph,
        "not_found": [ph for ph, n in by_ph.items() if n == 0],
        "skipped": skipped,
        "residual": _verify_residuals(out, [(ph, v) for ph, v, _ in usable]),
        "rels": n_rels,
        "attributes": n_attrs,
    }
    if ocr_cache:
        image_ocr.merge_image_report(report, img_by_ph)
    return out, report


def rebuild_docx(docx_bytes, mapping, preview_pdf_original=None, ctl=None,
                 ocr_cache=None):
    """Redazione del docx con una mappa GIÀ DATA (niente ri-analisi del modello)
    + PDF di ANTEPRIMA (via LibreOffice) + box.

    Cuore condiviso tra la prima anonimizzazione e le MODIFICHE alla mappa
    (de-anonimizzazione / tag custom): si riparte sempre dal docx originale.
    `preview_pdf_original` evita di riconvertire l'originale se il chiamante
    ce l'ha già (è immutabile: la mappa non lo tocca).

    Ritorna la stessa struttura di pdf.rebuild_pdf, più:
      - preview_pdf_original / preview_pdf_anonymized: conversioni per la
        preview a video (il file scaricabile resta il .docx redatto);
      - page_sizes con i DUE lati separati: il testo rifluisce, l'anonimizzato
        può avere un numero di pagine diverso dall'originale.
    I box si calcolano sui PDF convertiti riusando le stesse funzioni del
    processor PDF: ciò che è evidenziato a sinistra è ciò che a destra
    diventa placeholder.
    """
    from . import convert, pdf
    from .progress import NULL as _NULL_CTL
    ctl = ctl or _NULL_CTL

    if not mapping:
        raise DocxError("La mappa è vuota: niente da redigere.")
    ctl.phase("redaction")
    out, report = redact_docx(docx_bytes, mapping, ocr_cache=ocr_cache)
    if report["occurrences"] == 0:
        raise DocxError("Nessuna occorrenza trovata nel documento Word.")

    ctl.phase("preview")
    preview_orig = preview_pdf_original or convert.to_pdf(docx_bytes)
    preview_anon = convert.to_pdf(out)

    original_boxes = pdf._value_boxes(preview_orig, mapping)
    anonymized_boxes = pdf._placeholder_boxes(preview_anon, mapping)
    # le entità lette nelle immagini non compaiono nel testo dei PDF
    # convertiti (stanno nei pixel): i loro box si ritrovano a parte
    image_ocr.merge_ooxml_overlays(
        ocr_cache, mapping,
        [(original_boxes, preview_orig, docx_bytes),
         (anonymized_boxes, preview_anon, out)])
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


def anonymize_docx(docx_bytes, engine, excluded=None, custom_terms=None, ctl=None,
                   ocr=False):
    """Docx -> analisi PII + redazione gialla (vedi rebuild_docx per l'output).
    Con ocr=True le immagini in word/media/* vengono lette con RapidOCR e il
    testo letto passa allo stesso engine.analyze del corpo (vedi image_ocr);
    il percorso del testo resta identico, con o senza OCR."""
    from .progress import NULL as _NULL_CTL
    ctl = ctl or _NULL_CTL
    text = extract_text(docx_bytes, allow_empty=ocr)

    cache = None
    images = image_ocr.ooxml_images(docx_bytes) if ocr else []
    ctl.phases((["image_ocr"] if images else [])
               + ["analysis", "redaction", "preview"])
    if images:
        ctl.phase("image_ocr")
        cache = image_ocr.build_cache(images, ctl=ctl)
    if not text.strip() and cache is None:
        raise DocxError("Il documento Word non contiene testo e l'OCR non ha "
                        "letto testo nelle immagini.")

    ctl.phase("analysis")
    res = image_ocr.analyze_with_corpus(engine, text, cache, excluded=excluded,
                                        custom_terms=custom_terms, ctl=ctl)
    if not res["mapping"]:
        raise DocxError("Nessuna PII trovata: niente da anonimizzare in questo documento.")
    result = rebuild_docx(docx_bytes, res["mapping"], ctl=ctl, ocr_cache=cache)
    result["analysis"] = res
    result["ocr_cache"] = cache
    return result


def _verify_residuals(docx_bytes, items):
    """Placeholder il cui valore è ANCORA leggibile nell'output (testo +
    attributi con testo libero + metadati + target dei rels). Deve essere []."""
    try:
        text = extract_text(docx_bytes)
    except DocxError:
        text = ""
    zf = zipfile.ZipFile(io.BytesIO(docx_bytes))
    extra = []
    for name in zf.namelist():
        if name in ("docProps/core.xml", "docProps/app.xml", _PEOPLE_PART) \
                or name.endswith(".rels"):
            extra.append(_meta_surfaces(zf.read(name)))
        elif _TEXT_PART_RE.match(name):
            try:
                root = ET.fromstring(zf.read(name))
            except ET.ParseError:
                continue
            extra.extend(_alt_text_surfaces(root) + _author_surfaces(root))
    haystack = text + "\n" + "\n".join(extra)
    residual = []
    for ph, val in items:
        pat = _value_pattern(val)
        if pat and pat.search(haystack):
            residual.append(ph)
    return residual

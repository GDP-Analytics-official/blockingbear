"""Marcatura machine-readable dei file generati dall'AI (AI Act, art. 50).

Gli artifact prodotti dal modello nella sandbox vengono marcati nei METADATI
del file — mai nel contenuto visibile — con una dicitura in inglese più il
valore standard IPTC `trainedAlgorithmicMedia` (lo stesso vocabolario di
C2PA), che essendo un URI è indipendente dalla lingua. Il punto di aggancio
è unico: chat_routes._register_artifacts, DOPO l'eventuale ripristino dei
segnaposto (che riscrive o rigenera i byte e cancellerebbe il tag).

Tutto è best-effort: `mark_file` non solleva MAI — un tag fallito non deve
fermare la consegna dell'artifact — e ogni formato scrive prima su un file
temporaneo nella stessa cartella e poi os.replace, così un errore a metà
lascia il file com'era. Formati coperti: docx/xlsx/pptx (docProps/core.xml),
pdf (Info dict, salvataggio INCREMENTALE: appende senza riscrivere nulla),
png (chunk tEXt inserito a livello di byte, i pixel non si toccano), svg
(commento in testa). csv/json/txt restano senza tag in-file: un commento ne
romperebbe il parsing; per quelli la trasparenza resta a livello di
interfaccia (Attachment.source == "sandbox").

L'interruttore è il parametro admin `chat_ai_marking` (settings_store),
acceso di default; la lettura sta nel chiamante, qui solo la meccanica.
"""

import os
import re
import shutil
import struct
import uuid
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

from ..logging_setup import get_logger
from .mupdf_lock import mupdf_serialized

log = get_logger("blockingbear.ai_mark")

# Vocabolario IPTC per i contenuti generati da un modello (usato anche da
# C2PA): un URI, quindi agnostico rispetto alla lingua.
IPTC_URI = ("http://cv.iptc.org/newscodes/digitalsourcetype/"
            "trainedAlgorithmicMedia")

# La dicitura human-readable (inglese) + il valore machine-readable.
MARK_TEXT = ("AI-generated content (EU AI Act, Article 50). "
             "digitalsourcetype: " + IPTC_URI)

# Per i campi "brevi" (cp:category, Keywords del PDF).
MARK_SHORT = "AI-generated"
_PDF_KEYWORDS = "AI-generated, trainedAlgorithmicMedia"

_CORE_XML = "docProps/core.xml"

# I namespace standard di core.xml: registrarli fissa i prefissi in
# serializzazione. Indispensabile per dcterms e xsi: il file di Word contiene
# xsi:type="dcterms:W3CDTF", un QName DENTRO un valore — se ElementTree
# rinominasse i prefissi, quel riferimento resterebbe orfano.
_CORE_NS = {
    "cp": ("http://schemas.openxmlformats.org/package/2006/metadata/"
           "core-properties"),
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "dcmitype": "http://purl.org/dc/dcmitype/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
for _p, _u in _CORE_NS.items():
    ET.register_namespace(_p, _u)


def mark_file(path):
    """Marca `path` secondo il suo formato. True se il file risulta marcato
    (anche se lo era già), False se il formato non è marcabile o qualcosa
    è andato storto. Non solleva mai: il file, nel dubbio, resta intatto."""
    path = Path(path)
    handler = _HANDLERS.get(path.suffix.lower())
    if handler is None or not path.is_file():
        return False
    try:
        return bool(handler(path))
    except Exception as e:                          # noqa: BLE001
        log.warning(f"marcatura di {path.name} fallita: {e!r}")
        return False


def _replace_atomic(path, data):
    """Scrive `data` accanto a `path` e poi lo sostituisce: un errore a metà
    scrittura non può lasciare un file troncato."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.aimark")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# --- OOXML (docx/xlsx/pptx): docProps/core.xml -------------------------------

def _mark_ooxml(path):
    """Scrive cp:category e dc:description in docProps/core.xml. Il resto
    dello zip si ricopia part per part, byte per byte (stessi ZipInfo):
    contenuto e coordinate della preview non possono spostarsi."""
    with zipfile.ZipFile(path) as zin:
        names = zin.namelist()
        if _CORE_XML not in names:
            return False                # produttore esotico: meglio non toccare
        xml = zin.read(_CORE_XML)
        if MARK_TEXT.encode("utf-8") in xml:
            return True                 # già marcato: non riscrivere lo zip
        marked = _marked_core_xml(xml)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.aimark")
        try:
            with zipfile.ZipFile(tmp, "w") as zout:
                for item in zin.infolist():
                    data = (marked if item.filename == _CORE_XML
                            else zin.read(item.filename))
                    zout.writestr(item, data)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, path)
    return True


def _marked_core_xml(xml):
    """core.xml con la marcatura. Word stesso non rispetta l'ordine dello
    schema in questo part (visto: dc:title prima di cp:lastModifiedBy), i
    consumer sono lenienti: gli elementi mancanti si appendono in coda."""
    root = ET.fromstring(xml)
    for tag, text in ((f"{{{_CORE_NS['cp']}}}category", MARK_SHORT),
                      (f"{{{_CORE_NS['dc']}}}description", MARK_TEXT)):
        el = root.find(tag)
        if el is None:
            el = ET.SubElement(root, tag)
        el.text = text
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


# --- PDF: Info dict, salvataggio incrementale --------------------------------

@mupdf_serialized
def _mark_pdf(path):
    """Appende la marcatura ai metadati (Subject e Keywords) con saveIncr():
    i byte esistenti del PDF non vengono riscritti, quindi il lavoro del
    ripristino (pdf_export) non si può rovinare. Se l'incrementale non è
    possibile (file riparato all'apertura) si riscrive su un temporaneo."""
    import fitz

    doc = fitz.open(str(path))
    tmp = None
    try:
        md = doc.metadata or {}
        if MARK_TEXT in (md.get("subject") or ""):
            return True
        new = {k: (md.get(k) or "") for k in
               ("title", "author", "subject", "keywords", "creator",
                "producer", "creationDate", "modDate")}
        new["subject"] = ((new["subject"] + " | ") if new["subject"]
                          else "") + MARK_TEXT
        new["keywords"] = ((new["keywords"] + ", ") if new["keywords"]
                           else "") + _PDF_KEYWORDS
        doc.set_metadata(new)
        try:
            doc.saveIncr()
            return True
        except (RuntimeError, ValueError):
            tmp = path.with_name(
                f".{path.name}.{uuid.uuid4().hex[:8]}.aimark.pdf")
            doc.save(str(tmp))
    finally:
        doc.close()                     # su Windows os.replace vuole il file libero
    os.replace(tmp, path)
    return True


# --- PNG: chunk tEXt inserito a mano ------------------------------------------

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_KEYWORD = b"AI-Generated"


def _mark_png(path):
    """Inserisce un chunk tEXt prima di IEND lavorando sui byte: nessuna
    ri-codifica dell'immagine, i pixel restano identici al bit."""
    data = path.read_bytes()
    if not data.startswith(_PNG_SIG):
        return False
    if _PNG_KEYWORD + b"\x00" in data:
        return True
    iend = data.rfind(b"IEND")
    if iend < 12:                       # PNG senza IEND: rotto, non si tocca
        return False
    payload = _PNG_KEYWORD + b"\x00" + MARK_TEXT.encode("latin-1")
    chunk = (struct.pack(">I", len(payload)) + b"tEXt" + payload
             + struct.pack(">I", zlib.crc32(b"tEXt" + payload)))
    _replace_atomic(path, data[:iend - 4] + chunk + data[iend - 4:])
    return True


# --- SVG: commento nel prologo -------------------------------------------------

def _mark_svg(path):
    svg = path.read_text("utf-8")
    if MARK_TEXT in svg:
        return True
    comment = f"<!-- {MARK_TEXT} -->\n"
    m = re.match(r"\s*<\?xml[^>]*\?>\s*", svg)
    cut = m.end() if m else 0
    _replace_atomic(path, (svg[:cut] + comment + svg[cut:]).encode("utf-8"))
    return True


_HANDLERS = {
    ".docx": _mark_ooxml,
    ".xlsx": _mark_ooxml,
    ".pptx": _mark_ooxml,
    ".pdf": _mark_pdf,
    ".png": _mark_png,
    ".svg": _mark_svg,
}

# Estensioni marcabili, per chi deve dirlo senza provare (UI, test).
MARKABLE_EXTS = frozenset(_HANDLERS)

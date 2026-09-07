"""
Redazione dei file di testo semplice (.txt, .md, .csv, .tsv, .json, .xml): il
caso banale delle pipeline.

Niente XML né layout: il file È il testo, quindi la redazione è una
sostituzione diretta sulla stringa con le stesse regex ancorate di pdf_export
(confini di parola, spazi flessibili), applicata destra->sinistra per non
invalidare gli offset. Il placeholder [TAG_n] non può essere evidenziato
(il formato non ha formattazione): è lui stesso il marcatore.

Il .md viene trattato come testo puro: si redige il SORGENTE markdown, non la
resa. Stessa cosa per csv/json/xml: si redige il sorgente, senza parsarlo — il
segnaposto prende il posto del valore e la struttura del file resta quella che
era. Per il JSON questo regge finché un numero non viene trovato dentro un
altro numero (vedi la guardia sui bordi numerici in _value_pattern): un
segnaposto in mezzo a un literal numerico renderebbe il file non più leggibile.
Anche l'anteprima a video (via LibreOffice) importa il sorgente come testo
semplice, così ciò che si vede coincide con ciò che si scarica.

Encoding: il BOM si riconosce dai byte, poi si prova utf-8 e a seguire
cp1252/latin-1; l'output è SEMPRE utf-8 (BOM preservato se c'era, MAI
aggiunto): il download passa da
Starlette, che sui text/* dichiara charset=utf-8 comunque — normalizzare rende
l'header veritiero anche per gli ingressi legacy. Per l'anteprima si passa a
LibreOffice una copia utf-8 con BOM, che ne rende deterministico il rilevamento.

Rete di sicurezza finale come le altre pipeline (contratto comune, vedi
engine/__init__.py): report["residual"] rilegge l'output e deve essere vuota.
"""

from .pdf_export import _too_noisy, _value_pattern
from .text_patterns import contains_literal


class TxtError(ValueError):
    """Errore d'uso (file non valido, niente testo, dizionario vuoto...)."""


# I formati che passano da QUI: il file È il testo, non c'è layout da
# preservare, quindi un .csv o un .json costano quanto un .txt. Elenco unico,
# importato da chi instrada (routes/projects.py, chat_anonymization.py):
# quando erano tuple separate bastava aggiornarne una sola per far finire un
# file sul processor sbagliato.
# L'anteprima resta il sorgente importato come testo semplice (vedi
# rebuild_txt): così ciò che si vede a schermo e ciò che si scarica
# coincidono, anche per un csv (che LibreOffice aprirebbe come foglio).
TEXT_EXTS = (".txt", ".md", ".csv", ".tsv", ".json", ".xml")

_BOM = b"\xef\xbb\xbf"
_ENCODINGS = ("utf-8", "cp1252", "latin-1")


def _decode(data):
    """(testo, encoding) del file. TxtError se sembra binario.

    Il BOM si riconosce dai BYTE, non provando il codec: `decode("utf-8-sig")`
    riesce anche su un file che il BOM non l'ha, quindi usarlo come sonda
    dichiarava "utf-8-sig" per QUALSIASI utf-8 valido e la redazione finiva per
    aggiungere un BOM che nell'originale non c'era. Su un JSON quel BOM lo fa
    rifiutare da `json.load(..., encoding="utf-8")` — cioè il file
    anonimizzato non si apriva più come si apriva quello di partenza."""
    if b"\x00" in data[:4096]:
        raise TxtError("Il file non sembra testo (contiene byte nulli).")
    had_bom = data.startswith(_BOM)
    body = data[len(_BOM):] if had_bom else data
    for enc in _ENCODINGS:
        try:
            text = body.decode(enc)
        except UnicodeDecodeError:
            continue
        # il BOM c'era: si dichiara utf-8-sig perché l'output lo rimetta
        return text, ("utf-8-sig" if had_bom else enc)
    raise TxtError("Encoding del file di testo non riconosciuto.")  # irraggiungibile: latin-1 non fallisce


def extract_text(txt_bytes):
    """Testo del file. TxtError se vuoto o binario."""
    text, _ = _decode(txt_bytes)
    if not text.strip():
        raise TxtError("Il file di testo è vuoto.")
    return text


def redact_txt(txt_bytes, mapping):
    """Testo originale -> testo coi placeholder + report.

    mapping: {"[FULLNAME_1]": "Mario Rossi", ...} (il dizionario di analyze()).
    Report con le stesse chiavi delle altre pipeline: occurrences,
    by_placeholder, not_found, skipped, residual.
    """
    if not isinstance(mapping, dict) or not mapping:
        raise TxtError("Dizionario vuoto: anonimizza prima il documento.")
    items = sorted(((ph, v) for ph, v in mapping.items()
                    if isinstance(ph, str) and isinstance(v, str) and v.strip()),
                   key=lambda kv: -len(kv[1]))
    if not items:
        raise TxtError("Dizionario non valido.")

    skipped, usable = [], []
    for ph, val in items:
        if _too_noisy(val, ph):
            skipped.append(ph)
            continue
        pat = _value_pattern(val, ph)
        if pat:
            usable.append((ph, val, pat))
        else:
            skipped.append(ph)

    text, enc = _decode(txt_bytes)
    matches, claimed = [], []                 # (start, end, ph)
    for ph, _val, pat in usable:
        for m in pat.finditer(text):
            ms, me = m.start(), m.end()
            if any(ms < ce and me > cs for cs, ce in claimed):
                continue                      # già coperto da un valore più lungo
            claimed.append((ms, me))
            matches.append((ms, me, ph))

    by_ph = {ph: 0 for ph, _, _ in usable}
    for ms, me, ph in sorted(matches, key=lambda x: -x[0]):
        text = text[:ms] + ph + text[me:]
        by_ph[ph] += 1

    residual = [ph for ph, val, pat in usable
                if pat.search(text) or contains_literal(text, val, ph)]
    out_enc = "utf-8-sig" if enc == "utf-8-sig" else "utf-8"
    return text.encode(out_enc), {
        "occurrences": sum(by_ph.values()),
        "by_placeholder": by_ph,
        "not_found": [ph for ph, n in by_ph.items() if n == 0],
        "skipped": skipped,
        "residual": residual,
    }


def _preview_bytes(txt_bytes):
    """Copia utf-8 con BOM per LibreOffice: senza BOM il filtro txt tira a
    indovinare l'encoding e i caratteri accentati possono uscire storpiati."""
    text, _ = _decode(txt_bytes)
    return text.encode("utf-8-sig")


def rebuild_txt(txt_bytes, mapping, preview_pdf_original=None, ctl=None):
    """Redazione del testo con una mappa GIÀ DATA (niente ri-analisi del
    modello) + PDF di ANTEPRIMA (via LibreOffice) + box.

    Stessa struttura di rebuild_docx: il file scaricabile resta un .txt/.md
    coi placeholder, l'anteprima e i box si calcolano sui PDF convertiti con
    le stesse funzioni del processor PDF.
    """
    from . import convert, pdf
    from .progress import NULL as _NULL_CTL
    ctl = ctl or _NULL_CTL

    if not mapping:
        raise TxtError("La mappa è vuota: niente da redigere.")
    ctl.phase("redaction")
    out, report = redact_txt(txt_bytes, mapping)
    if report["occurrences"] == 0:
        raise TxtError("Nessuna occorrenza trovata nel file di testo.")

    ctl.phase("preview")
    preview_orig = preview_pdf_original or convert.to_pdf(_preview_bytes(txt_bytes),
                                                          suffix=".txt")
    preview_anon = convert.to_pdf(_preview_bytes(out), suffix=".txt")

    original_boxes = pdf._value_boxes(preview_orig, mapping)
    anonymized_boxes = pdf._placeholder_boxes(preview_anon, mapping)
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


def anonymize_txt(txt_bytes, engine, excluded=None, custom_terms=None, ctl=None):
    """Testo -> analisi PII + redazione (vedi rebuild_txt per l'output)."""
    from .progress import NULL as _NULL_CTL
    ctl = ctl or _NULL_CTL
    text = extract_text(txt_bytes)
    ctl.phases(["analysis", "redaction", "preview"])
    ctl.phase("analysis")
    res = engine.analyze(text, excluded=excluded, custom_terms=custom_terms, ctl=ctl)
    if not res["mapping"]:
        raise TxtError("Nessuna PII trovata: niente da anonimizzare in questo documento.")
    result = rebuild_txt(txt_bytes, res["mapping"], ctl=ctl)
    result["analysis"] = res
    return result

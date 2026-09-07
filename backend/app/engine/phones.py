"""
TELEPHONENUM: numeri di telefono nei piani di numerazione dei paesi delle
lingue supportate (IT, FR, DE, ES, NL, GB) e dei vicini (BE, CH, AT), più gli
Stati Uniti per i documenti in inglese.

Il riconoscimento lo fa la libreria `phonenumbers` (porting Python di
libphonenumber di Google): conosce prefissi, lunghezze e intervalli assegnati
di ogni piano nazionale, e distingue "06 12 34 56 78" (cellulare francese) da
"12 34 56 78 90" (dieci cifre qualunque). Una regex per paese non lo saprebbe
fare senza diventare essa stessa una copia del piano di numerazione.

Ma un numero VALIDO per un piano non è ancora un telefono: nove cifre a gruppi
di tre sono un SIREN tanto quanto un fisso francese, e undici cifre sono una
Steuer-ID o una partita IVA. Perciò la forma sola basta solo quando la
scrittura è quella di un telefono:
  - prefisso internazionale ("+33 6 12 34 56 78", "0033 ...");
  - zero iniziale del piano nazionale ("06 12 34 56 78", "030 123456",
    "010-1234567", "07400 123456"), che ES e US non usano;
  - cellulare italiano nella forma di sempre ("312 3456789"), che è quella
    che la rete regex copriva già.
Altrimenti ("612 34 56 78", "(201) 555-0123", "303 265 045") serve l'etichetta
davanti: tel., téléphone, Telefon, teléfono, telefoon, phone, mobile, Handy,
móvil, portable, fax, WhatsApp... (lexicon.PHONE_CUE).

`detectors.detect_regex` chiama `detect_phones` per ULTIMO e gli passa le span
delle altre entità (IBAN, carte, identificativi, date, importi): un numero
dentro uno di quelli non è un telefono, anche quando il piano lo ammetterebbe.

Senza la libreria (immagine vecchia, ambiente di test minimo) si ripiega sulla
regex italiana storica e si avvisa nel log: il tag resta coperto per l'Italia,
gli altri paesi restano in chiaro. Come detectors.py il modulo lavora su
stringhe, senza modello.
"""

import logging
import re

from . import lexicon as _lx

log = logging.getLogger(__name__)

try:
    from phonenumbers import Leniency, PhoneNumberMatcher
except ImportError:  # pragma: no cover
    PhoneNumberMatcher = Leniency = None
    log.warning("phonenumbers is not installed: TELEPHONENUM covers Italian numbers only")

REGIONS = ("IT", "FR", "DE", "ES", "NL", "GB", "BE", "CH", "AT", "US")
# piani nazionali SENZA lo zero iniziale: lì uno zero davanti non prova niente
NO_TRUNK_ZERO = frozenset({"ES", "US"})

# Candidato grezzo: 7-18 cifre con al più due separatori fra una cifra e l'altra
# (spazio anche non separabile, punto, trattino, barra, parentesi), eventuale
# "+" o "00" davanti. NBSP e NNBSP sono frequenti nei PDF tipografici francesi.
# Dove finisce il numero lo decide poi la libreria sul candidato: la regex
# serve solo a non farle scandire tutto il testo per ogni regione.
_HSP = r"[ \t\u00a0\u202f]"
_CAND = re.compile(r"(?<![\w+])(?:\+" + _HSP + r"?|00)?\(?\d(?:[ \t\u00a0\u202f.\-/()]{0,2}\d){6,17}\)?(?![\w])")
_EXTENSION = re.compile(
    r"[ \t\u00a0\u202f]*(?:ext(?:ension)?|x|interno|poste|durchwahl|anexo|toestel)"
    r"[ \t\u00a0\u202f.:]*[0-9]{1,10}(?![\w])", re.I)
_CUE_RX = re.compile(r"(?<![\w])(?:" + _lx.alt(_lx.PHONE_CUE) + r")\.?(?:" + _HSP
                     + r"+[^\W\d_]{1,3}(?![^\W\d_])){0,2}"
                     r"[ \t\u00a0\u202f:.\-]*(?:n(?:o|r|um|umero|°|º)?\.?)?[ \t\u00a0\u202f:.\-]*$",
                     re.IGNORECASE)
CUE_BACK = 30                          # caratteri di contesto a sinistra

# regex storica (rizzo-pii): cellulare e fisso italiani, con i separatori di stampa
_LEGACY_RX = re.compile(r"(?<![\w.])(?:\+39[\s.\-]?)?(?:3\d{2}[\s.\-]?\d{3}[\s.\-]?\d{3,4}"
                        r"|0\d{1,3}[\s.\-]?\d{5,8})(?![\w])")


def has_phone_cue(text, i):
    """C'è un'etichetta di telefono subito prima della posizione `i`?"""
    return bool(_CUE_RX.search(text[max(0, i - CUE_BACK):i]))


def written_as_phone(raw, region):
    """La scrittura è già quella di un telefono (prefisso internazionale, zero
    del piano nazionale, cellulare italiano)? Se no serve l'etichetta."""
    digits = re.sub(r"\D", "", raw)
    head = raw.lstrip("( \t")
    if head.startswith("+") or head.startswith("00"):
        return True
    if digits.startswith("0") and region not in NO_TRUNK_ZERO:
        return True
    return region == "IT" and digits.startswith("3") and len(digits) in (9, 10)


def _blocked(s, e, blocked):
    return any(s < be and e > bs for bs, be in blocked)


def _ent(s, e):
    # nessun checksum: come la voce storica, regex non validata
    return {"label": "TELEPHONENUM", "start": s, "end": e, "score": 0.9,
            "validated": False, "source": "regex"}


def detect_phones(text, blocked=()):
    """Entità TELEPHONENUM nella forma di `detect_regex`. `blocked` sono le
    span (start, end) che un telefono non può sovrapporre."""
    if not text:
        return []
    if PhoneNumberMatcher is None:                         # pragma: no cover
        return [_ent(m.start(), m.end()) for m in _LEGACY_RX.finditer(text)
                if not _blocked(m.start(), m.end(), blocked)]
    spans = set()
    for m in _CAND.finditer(text):
        cand, base = m.group(), m.start()
        for region in REGIONS:
            for pm in PhoneNumberMatcher(cand, region, leniency=Leniency.VALID):
                s, e = base + pm.start, base + pm.end
                if _blocked(s, e, blocked):
                    continue
                if not (written_as_phone(text[s:e], region) or has_phone_cue(text, s)):
                    continue
                extension = _EXTENSION.match(text, e)
                if extension and not _blocked(e, extension.end(), blocked):
                    e = extension.end()
                spans.add((s, e))
    # lo stesso numero letto da più regioni, o con confini diversi: la più lunga
    out = []
    for s, e in sorted(spans, key=lambda x: (-(x[1] - x[0]), x[0])):
        if not _blocked(s, e, out):
            out.append((s, e))
    return [_ent(s, e) for s, e in sorted(out)]

# Derivato da rizzo-pii `src/app/detectors.py` — MIT, (c) 2026 Simone Rizzo,
# Rizzo AI Academy (https://github.com/Rizzo-AI-Academy/rizzo-pii) — con
# modifiche locali. La licenza MIT vuole che il suo avviso viaggi insieme a
# queste porzioni: il testo integrale è in fondo al file NOTICE, alla radice
# del repository.
"""
Rete REGEX + CHECKSUM che affianca il modello (EMAIL, TELEFONO, IBAN, CF, PIVA,
carta di credito, importo, targa, URL). I moduli fratelli coprono il resto:
credentials.py, cyber.py e devices.py il gruppo «Cybersecurity», national_ids.py
gli identificativi di persona e d'impresa fuori dall'Italia (con le stesse label
CF e PIVA), phones.py i telefoni nei piani di numerazione dei paesi delle
lingue supportate; lexicon.py le parole chiave in francese, tedesco, spagnolo e
olandese che tutti questi condividono.

Come `pdf_export`, questo modulo lavora solo su stringhe: nessun import di
torch/transformers/fitz, quindi è testabile in isolamento e senza il modello.
`core.py` ne consuma i nomi pubblici (`DETECTORS`, `detect_regex`, i validatori
di checksum) e fonde i risultati con quelli del modello.

Le regex accettano i separatori con cui gli identificativi sono STAMPATI nei
documenti, non solo la forma compatta: un IBAN su una fattura o su una carta
intestata è raggruppato a quattro, una carta di credito è separata da spazi,
trattini o punti, un telefono da spazi, punti o trattini. I validatori
normalizzano già i separatori: se la regex non gliene passa mai uno il
checksum non viene nemmeno interrogato e, dove `strict=True`, il valore resta
in chiaro senza alcun fallback.
"""

import re

from . import lexicon as _lx
from .credentials import CREDENTIAL_LABELS, detect_credentials  # noqa: F401
from .cyber import CYBER_LABELS, detect_cyber  # noqa: F401
from .devices import DEVICE_LABELS, detect_devices, is_device_number  # noqa: F401
from .national_ids import detect_national_ids, es_id_ok  # noqa: F401
from .phones import detect_phones  # noqa: F401


def iban_ok(s):
    # Si normalizzano gli stessi separatori che la regex ammette: spazio (anche
    # non-breaking, e gli a-capo del testo estratto da un PDF), punto, trattino.
    s = re.sub(r"[\s.\-]", "", s).upper()
    if not (15 <= len(s) <= 34):
        return False
    r = s[4:] + s[:4]
    try:
        n = int("".join(str(ord(c) - 55) if c.isalpha() else c for c in r))
    except ValueError:
        return False
    return n % 97 == 1


def piva_ok(p):
    p = re.sub(r"\D", "", p)
    if len(p) != 11:
        return False
    t = 0
    for i, c in enumerate(map(int, p[:10])):
        if i % 2 == 0:
            t += c
        else:
            x = c * 2
            t += x - 9 if x > 9 else x
    return (10 - t % 10) % 10 == int(p[10])


_CF_ODD = {"0": 1, "1": 0, "2": 5, "3": 7, "4": 9, "5": 13, "6": 15, "7": 17, "8": 19,
           "9": 21, "A": 1, "B": 0, "C": 5, "D": 7, "E": 9, "F": 13, "G": 15, "H": 17,
           "I": 19, "J": 21, "K": 2, "L": 4, "M": 18, "N": 20, "O": 11, "P": 3, "Q": 6,
           "R": 8, "S": 12, "T": 14, "U": 16, "V": 10, "W": 22, "X": 25, "Y": 24, "Z": 23}


def cf_ok(c):
    c = c.strip().upper()
    if len(c) != 16 or not c.isalnum():
        return False
    b = c[:15]
    try:
        t = sum((_CF_ODD[ch] if i % 2 == 0
                 else (int(ch) if ch.isdigit() else ord(ch) - 65))
                for i, ch in enumerate(b))
    except KeyError:
        return False
    return chr(65 + t % 26) == c[15]


def _luhn(d):
    """Luhn nudo, senza vincoli di lunghezza: lo usano sia le carte (13-19 cifre)
    sia il SIREN francese (9), quindi il controllo di lunghezza sta nei chiamanti."""
    tot, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        tot += n
        alt = not alt
    return tot % 10 == 0


def luhn_ok(s):
    d = re.sub(r"\D", "", s)
    return 13 <= len(d) <= 19 and _luhn(d)


def card_ok(s):
    """Carta di credito: Luhn E una forma che qualche circuito emette davvero. Un
    IMEI (15 cifre, prefisso 00/01/35/86/99) e un ICCID (18-22 cifre, prefisso 89,
    che ISO/IEC 7812 riserva alle telecomunicazioni) superano il Luhn come una
    carta ma sono DEVICE_ID (devices.py): qui si scartano, altrimenti la rete
    regex li taggherebbe carta validata e il post-filtro terrebbe per buono lo
    stesso errore del modello. Nessuna carta vera ha quei prefissi a quelle
    lunghezze (Amex e Diners a 15 cifre iniziano per 3[0146-9])."""
    return luhn_ok(s) and not is_device_number(re.sub(r"\D", "", s))


# Forma di una carta: 13-19 cifre con al più UN separatore (spazio, punto,
# trattino) tra una cifra e l'altra, che inizia e finisce su una cifra.
# Condivisa tra la rete regex (voce CREDITCARDNUMBER in DETECTORS) e il
# post-filtro sull'output del modello (core._clean_card).
_CARD_RE = re.compile(r"(?<!\d)\d(?:[ .\-]?\d){12,18}(?!\d)")


def scan_card(text):
    """Candidati carta nel testo: [(start, end, card_ok)].

    Stessa forma che pretende la rete regex, ma senza scartare chi fallisce il
    Luhn: serve al post-filtro sull'output del modello, che deve sapere se una
    span marcata CREDITCARDNUMBER copre un numero di carta plausibile e se il
    checksum regge. Fuori dalla forma (meno di 13 o più di 19 cifre, doppi
    separatori, cifre attaccate ad altre cifre) non esce alcun candidato."""
    return [(m.start(), m.end(), card_ok(m.group())) for m in _CARD_RE.finditer(text)]


# --------------------------------------------------------------------------- #
# Partita IVA europea                                                          #
# --------------------------------------------------------------------------- #
# Il modello è addestrato SOLO su partite IVA italiane (rizzo-pii
# `generate_synthetic_pii.partita_iva`: 11 cifre + Luhn), quindi su una VAT
# estera sbaglia anche la FORMA della span - tipicamente prende il numero e
# lascia fuori il prefisso. Qui sotto la forma nazionale di ogni paese, così
# che sia la rete regex a trovarla per intero e il post-filtro sul modello
# (core._clean_piva) a rimetterla in squadra invece di scartarla.
#
# Fuori dall'Italia il PREFISSO DEL PAESE È OBBLIGATORIO, e non è una
# preferenza: "811569869" nudo non si distingue da un numero di protocollo, ed
# è esattamente la classe di falsi positivi che questo modulo esiste per
# togliere. Solo l'Italia può starne senza, perché lì il carico lo regge il
# Luhn su 11 cifre (voce PIVA in DETECTORS).


def vat_at(b):
    """AT: U + 8 cifre, somma pesata delle cifre dei prodotti."""
    d = b[1:]
    s = 0
    for i, c in enumerate(d[:7]):
        v = int(c) * (1, 2)[i % 2]
        s += v // 10 + v % 10
    return (96 - s) % 10 == int(d[7])


def vat_be(b):
    """BE: 10 cifre, le ultime due sono 97 - (prime otto mod 97)."""
    return 97 - int(b[:8]) % 97 == int(b[8:])


def vat_de(b):
    """DE: 9 cifre, ISO 7064 MOD 11,10."""
    p = 10
    for c in b[:8]:
        s = (int(c) + p) % 10 or 10
        p = (2 * s) % 11
    return (11 - p) % 10 == int(b[8])


def vat_el(b):
    """EL (Grecia): 9 cifre, pesi 256..2, resto mod 11 troncato alla cifra."""
    s = sum(int(c) * (2 ** (8 - i)) for i, c in enumerate(b[:8]))
    return s % 11 % 10 == int(b[8])


# ES: NIF, NIE e CIF hanno lettera di controllo con tabelle diverse; lo
# stesso controllo serve agli identificativi spagnoli senza prefisso
# (national_ids.py), quindi vive lì.
vat_es = es_id_ok


def vat_fr(b):
    """FR: chiave di 2 caratteri + SIREN di 9 cifre (Luhn). Quando la chiave è
    numerica si verifica anche quella; lo schema alfanumerico (dal 2014) non è
    pubblico, lì ci si ferma al SIREN."""
    key, siren = b[:2], b[2:]
    if not siren.isdigit() or not _luhn(siren):
        return False
    return (12 + 3 * (int(siren) % 97)) % 97 == int(key) if key.isdigit() else True


def vat_gb(b):
    """GB: 9 cifre (le 12 aggiungono il suffisso di gruppo), mod 97 nelle due
    varianti - quella storica e quella "9755" delle partite più recenti.
    GD/HA sono enti pubblici e non hanno checksum: li tiene la sola forma."""
    if b[:2] in ("GD", "HA"):
        return True
    b = b[:9]
    tot = sum(int(c) * (8 - i) for i, c in enumerate(b[:7])) + int(b[7:])
    return tot % 97 == 0 or (tot + 55) % 97 == 0


def vat_lu(b):
    """LU: 8 cifre, le ultime due sono le prime sei mod 89."""
    return int(b[:6]) % 89 == int(b[6:])


def vat_nl(b):
    """NL: 9 cifre + B + 2. Le partite storiche usano il mod 11 pesato, quelle
    emesse dal 2020 il mod 97 sull'intera stringa "NL..." (come un IBAN)."""
    s = sum(int(c) * (9 - i) for i, c in enumerate(b[:8]))
    if s % 11 != 10 and s % 11 == int(b[8]):
        return True
    n = int("".join(str(ord(c) - 55) if c.isalpha() else c for c in "NL" + b))
    return n % 97 == 1


def vat_pl(b):
    """PL: 10 cifre, pesi 6,5,7,2,3,4,5,6,7 mod 11 (10 non è un controllo valido)."""
    s = sum(int(c) * w for c, w in zip(b, (6, 5, 7, 2, 3, 4, 5, 6, 7)))
    return s % 11 != 10 and s % 11 == int(b[9])


def vat_pt(b):
    """PT: 9 cifre, pesi 9..2 mod 11; resto 0 o 1 -> cifra di controllo 0."""
    s = sum(int(c) * (9 - i) for i, c in enumerate(b[:8]))
    d = 11 - s % 11
    return (0 if d >= 10 else d) == int(b[8])


# prefisso -> (forma del corpo nazionale, validatore | None).
# Il validatore manca dove il checksum non ripaga il codice: col prefisso
# obbligatorio la sola forma è già molto discriminante ("NL123456789B01" non
# è nient'altro), e senza validatore il match resta semplicemente NON validato
# (niente ✓, ma coperto lo stesso).
EU_VAT = {
    "AT": (r"U\d{8}", vat_at),
    "BE": (r"[01]\d{9}", vat_be),
    "BG": (r"\d{9,10}", None),
    "CY": (r"\d{8}[A-Z]", None),
    "CZ": (r"\d{8,10}", None),
    "DE": (r"\d{9}", vat_de),
    "DK": (r"\d{8}", None),
    "EE": (r"\d{9}", None),
    # Grecia: il prefisso IVA è EL, il codice ISO è GR. Si accettano entrambi.
    "EL": (r"\d{9}", vat_el),
    "GR": (r"\d{9}", vat_el),
    "ES": (r"[A-Z0-9]\d{7}[A-Z0-9]", vat_es),
    "FI": (r"\d{8}", None),
    "FR": (r"[A-Z0-9]{2}\d{9}", vat_fr),
    "HR": (r"\d{11}", None),
    "HU": (r"\d{8}", None),
    "IE": (r"\d{7}[A-W]|\d[A-Z0-9+*]\d{5}[A-W]|\d{7}[A-W][A-I]", None),
    "IT": (r"\d{11}", piva_ok),
    "LT": (r"\d{9}|\d{12}", None),
    "LU": (r"\d{8}", vat_lu),
    "LV": (r"\d{11}", None),
    "MT": (r"\d{8}", None),
    "NL": (r"\d{9}B\d{2}", vat_nl),
    "PL": (r"\d{10}", vat_pl),
    "PT": (r"\d{9}", vat_pt),
    # la specifica dice 2-10 cifre: sotto le quattro ("RO12") è rumore, non una
    # partita IVA, e senza validatore non ci sarebbe niente a fermarlo.
    "RO": (r"\d{4,10}", None),
    "SE": (r"\d{10}01", None),
    "SI": (r"\d{8}", None),
    "SK": (r"\d{10}", None),
    # fuori dall'Unione, ma sulle fatture italiane si vedono
    "GB": (r"\d{9}|\d{12}|(?:GD|HA)\d{3}", vat_gb),
    "CHE": (r"\d{9}", None),
    "NO": (r"\d{9}", None),
}

# Candidato grezzo: prefisso + corpo. La forma esatta NON si può mettere qui
# (dipende dal paese appena matchato): la si verifica dopo, con `fullmatch`
# sulla voce di EU_VAT. Il corpo è volutamente avido - se inghiotte una cifra
# di troppo il fullmatch fallisce, ed è giusto così: "DE1234567890" non è
# una partita IVA tedesca a cui manca un pezzo, è un altro numero.
#
# Prefisso in MAIUSCOLO obbligatorio (niente IGNORECASE): metà dei prefissi
# sono parole italiane correntissime - SE, SI, NO, IT, CHE - e in minuscolo
# "che 123456789" diventerebbe una partita IVA svizzera.
_VAT_CAND = re.compile(
    r"(?<![A-Za-z0-9])(?P<cc>"
    + "|".join(sorted(EU_VAT, key=len, reverse=True))
    + r")(?P<sep>[\s.\-]?)(?P<body>[A-Z0-9](?:[A-Z0-9.\-]*[A-Z0-9])?)(?![A-Za-z0-9])")

# Ancora lessicale: l'etichetta che precede il numero. Vale come prova
# alternativa al checksum, in italiano e nelle lingue dei paesi in tabella.
_VAT_CUE = re.compile(
    r"(?:p\.?\s*(?:iva|i\.?\s*v\.?\s*a\.?)|partita\s+iva|part\.?\s*iva|cod\.?\s*iva"
    r"|vat|tva|btw|ust-?\s*id(?:\.?nr\.?)?|mwst|nif|nipc|cif|cvr|alv|dph|nip)"
    r"[\s:.\-]*(?:n(?:o|r|um)?\.?)?[\s:.\-]*$", re.IGNORECASE)
_VAT_CUE_BACK = 30                     # caratteri di contesto a sinistra


def has_vat_cue(text, i):
    """C'è un'etichetta di partita IVA subito prima della posizione `i`?"""
    return bool(_VAT_CUE.search(text[max(0, i - _VAT_CUE_BACK):i]))


def detect_eu_vat(text):
    """Partite IVA europee scritte col prefisso del paese ("DE811569869")."""
    ents = []
    for m in _VAT_CAND.finditer(text):
        shape, validator = EU_VAT[m.group("cc")]
        body = re.sub(r"[.\-]", "", m.group("body"))
        if not re.fullmatch(shape, body):
            continue
        ok = bool(validator(body)) if validator else False
        if validator and not ok:
            continue                   # checksum implementato e fallito: non è una VAT
        # Prefisso separato da uno SPAZIO: è la forma che collide con la prosa
        # ("SE 1234567801" è "se" più un numero prima che una VAT svedese).
        # Lì la forma da sola non basta più: serve il checksum o l'etichetta.
        # Punto e trattino no: sono separatori di stampa, non spaziatura di
        # frase - la Svizzera scrive "CHE-123.456.789" e nessuno scrive
        # "che-123456789" in prosa.
        if m.group("sep").isspace() and not (ok or has_vat_cue(text, m.start())):
            continue
        ents.append({"label": "PIVA", "start": m.start(), "end": m.end(),
                     "score": 1.0 if ok else 0.9, "validated": ok, "source": "regex"})
    return ents


# Ogni detector: (label, regex, validatore-o-None, strict).
#   validatore None  -> match accettato sulla sola forma (validated=False).
#   strict=True      -> il match si scarta se il checksum FALLISCE (forma troppo generica:
#                       IBAN/PIVA/carta -> servono i numeri giusti per non avere falsi positivi).
#   strict=False     -> si redige comunque (forma molto specifica, es. CF: meglio nascondere);
#                       validated=True solo se il checksum passa (mette il ✓).
DETECTORS = [
    ("EMAIL",
     # Il confine sinistro non serve solo alla correttezza: senza, su una lunga
     # parola ASCII priva di "@" il quantificatore riparte da ogni carattere e
     # rende la ricerca quadratica.
     re.compile(r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
     None, True),
    ("CF",
     re.compile(r"\b[A-Za-z]{6}\d{2}[A-Za-z]\d{2}[A-Za-z]\d{3}[A-Za-z]\b"),
     cf_ok, False),
    # CF OMOCODICO: quando due contribuenti collidono, l'Agenzia sostituisce le
    # cifre da destra con una lettera (0->L 1->M 2->N 3->P 4->Q 5->R 6->S 7->T
    # 8->U 9->V), quindi la forma sopra non lo trova. Voce separata e non classe
    # allargata sulla precedente: qui la forma da sola è troppo generica (sette
    # posizioni che accettano lettere), perciò strict=True e il checksum
    # diventa obbligatorio. `cf_ok` già calcola l'omocodia. La forma canonica
    # matcha entrambe le voci, ma sullo stesso span: `_merge` tiene il candidato
    # validato e scarta il duplicato.
    ("CF",
     re.compile(r"\b[A-Za-z]{6}[\dLMNPQRSTUVlmnpqrstuv]{2}[A-Za-z]"
                r"[\dLMNPQRSTUVlmnpqrstuv]{2}[A-Za-z]"
                r"[\dLMNPQRSTUVlmnpqrstuv]{3}[A-Za-z]\b"),
     cf_ok, True),
    # IBAN, forma compatta: copre qualsiasi paese, anche fuori dal registro ISO.
    # Il formato di stampa a gruppi NON è una regex ma `detect_iban()` qui sotto,
    # perché per sapere dove finisce serve la lunghezza prevista per il paese.
    ("IBAN",
     re.compile(r"\b[A-Za-z]{2}\d{2}[A-Za-z0-9]{11,30}\b"),
     iban_ok, True),
    # Carta: al gruppo dei separatori si aggiunge il punto ("4111.1111...").
    # Qui NON si usa \s: a differenza dell'IBAN il vincolo è il solo Luhn (una
    # sequenza qualunque lo supera una volta su dieci) e con l'a-capo una
    # colonna di numeri in tabella diventerebbe un candidato.
    # Il match ora finisce per forza su una CIFRA: con il separatore in coda
    # ("(?:\d[ .\-]?){13,19}") il punto che chiude la frase entrerebbe nello
    # span e finirebbe dentro il placeholder.
    ("CREDITCARDNUMBER", _CARD_RE, card_ok, True),
    ("PIVA",
     re.compile(r"(?<!\d)\d{11}(?!\d)"),
     piva_ok, True),
    # Telefono: non è più una voce di questa tabella ma phones.detect_phones,
    # che conosce i piani di numerazione nazionali e gira per ultimo in
    # detect_regex (un numero dentro un IBAN o una carta non è un telefono).
    ("AMOUNT",
     re.compile(r"(?:€|EUR|euro)\s?\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})?"
                r"|\d{1,3}(?:\.\d{3})*,\d{2}\s?(?:€|EUR|euro)", re.IGNORECASE),
     None, True),
    # il trattino è un separatore quanto lo spazio ("AB-123-CD" nei moduli e negli
    # export): senza, la targa scritta così non viene vista da nessuno dei due lati,
    # perché nemmeno il modello la riconosce, e resta in chiaro.
    ("TARGA",
     re.compile(r"\b[A-Za-z]{2}[\s-]?\d{3}[\s-]?[A-Za-z]{2}\b"),
     None, True),
    # URL: tre forme, dalla più esplicita alla più rischiosa.
    #   1) con schema (http/https/ftp)     -> sempre un URL
    #   2) www.<dominio>                   -> sempre un URL
    #   3) dominio nudo, ma SOLO con un TLD della lista chiusa (lexicon.PUBLIC_TLD).
    # Il dominio nudo generico (r"\w+\.\w{2,}") non si può usare: nel legalese
    # italiano prenderebbe "p.iva", "n.ro", "S.r.l." e simili. Con la lista chiusa
    # "p.iva" non matcha ("iva" non è un TLD) e i falsi positivi crollano.
    # Prezzo del compromesso: un dominio con TLD esotico resta in chiaro.
    ("URL",
     re.compile(r"(?:https?|ftp)://[^\s<>\"']+"
                r"|www\.[A-Za-z0-9\-._~%]+\.[A-Za-z]{2,}(?:/[^\s<>\"']*)?"
                r"|\b(?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?\.)+"
                r"(?:" + _lx.alt(_lx.PUBLIC_TLD_LOOSE) + r")"
                r"\b(?:/[^\s<>\"']*)?"
                # ccTLD e TLD di due lettere (fr, de, es, nl, uk, co.uk...): davanti
                # un'etichetta di almeno tre caratteri e suffisso minuscolo, altrimenti
                # "p.es.", "i.e.", "u.a." e le sigle scritte col punto diventano domini
                r"|\b(?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?\.)*"
                r"[A-Za-z0-9][A-Za-z0-9\-]+[A-Za-z0-9]\."
                r"(?-i:(?:" + _lx.alt([t.replace(".", r"\.") for t in _lx.PUBLIC_TLD_2ND + _lx.PUBLIC_TLD_STRICT])
                + r"))\b(?:/[^\s<>\"']*)?", re.IGNORECASE),
     None, True),
    # DOCID: il codice di un atto è scritto sempre dopo la sua sigla ("R.G. 1234/2024",
    # "Prot. 123/2024", "Rep. 45"). È la sigla a renderlo riconoscibile: il numero da
    # solo ("1234/2024") non si distingue da una frazione o da un articolo di legge,
    # quindi la si pretende sempre e la si include nello span.
    # Il "n." fra la sigla e il numero è opzionale e vale per TUTTE le sigle: si scrive
    # "Prot. 456/2024" quanto "Prot. n. 456/2024", e negli atti la seconda è la forma
    # più frequente. Vale anche per le sigle ABBREVIATE, non solo per la parola
    # "protocollo" per esteso: "Prot. n. 456/2024" è il caso più comune di tutti.
    ("DOCID",
     re.compile(r"\b(?:R\.?G\.?\s*N\.?R\.?|R\.?G\.?|RG|Prot\.?|protocollo"
                r"|Rep\.?|repertorio)"
                r"(?:\s*(?:n\.?|num\.?|nro\.?))?"
                r"\s*\d{1,8}(?:[/\-]\d{2,4})?\b",
                re.IGNORECASE),
     None, True),
    # Date numeriche: forma specifica ma SENZA validatore possibile (una data non ha
    # checksum). Sta in SOFT_REGEX_LABELS -> in fusione non eredita la priorità della
    # rete regex, quindi il modello può sovrascriverla: "06-11-2014" dentro un
    # riferimento di laboratorio resta un falso positivo accettabile, non un verdetto.
    ("DATE",
     re.compile(r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[/.\-]"
                r"(?:0?[1-9]|1[0-2])[/.\-](?:19|20)\d{2}"
                r"(?:\s+\d{1,2}[:.]\d{2})?(?!\d)"),
     None, True),
]

# Label della rete regex SENZA validatore forte: la forma da sola non basta a dire
# "è certamente questo campo". In _merge non ereditano la priorità della rete regex,
# così il modello può sovrascriverle.
# USERNAME è soft per un altro motivo: "Utente: Mario" è quasi sempre un nome, e
# se il modello lo vede come FULLNAME deve vincere lui (PASSWORD e SECRET no: il
# modello non li conosce e taglierebbe una chiave a metà).
SOFT_REGEX_LABELS = {"DATE", "USERNAME"}

# Label a span ESATTA (credentials.py + cyber.py): in core._merge niente
# allargamento ai confini di parola né taglio della punteggiatura ai bordi, nel
# registro della chat confronto e ricerca esatti e case-sensitive. Una password
# può finire con "!", un hash con "." o "=", "-pSegreto" è la password dietro
# il flag -p, e "Abc" e "abc" sono due valori diversi. HOSTNAME e DEVICE_ID
# (devices.py) restano FUORI apposta: un nome di macchina e un numero di serie
# sono case-insensitive (SRV01 e srv01 devono avere lo stesso placeholder) e la
# punteggiatura ai bordi non gli appartiene.
EXACT_SPAN_LABELS = frozenset(CREDENTIAL_LABELS | CYBER_LABELS)

# Gruppi che la UI mostra sotto un'unica voce richiudibile (CategoriesPicker,
# AnonTags), esposti da /api/tags: l'appartenenza la decide il backend, così un
# tag nuovo compare al posto giusto senza toccare il frontend. Per ora un solo
# gruppo, «Cybersecurity»: credenziali + identificativi tecnici + nomi di macchina
# e identificativi di dispositivo.
TAG_GROUPS = {"cyber": sorted(EXACT_SPAN_LABELS | DEVICE_LABELS)}

# Punteggiatura che chiude la frase e non fa parte dell'URL: "vedi https://x.it/pagina."
_URL_TRAIL = ".,;:!?)]}»\"'"


# ISO 13616: lunghezza dell'IBAN per paese. Serve a sapere DOVE finisce quando è
# scritto a gruppi: indovinare il confine significa inghiottire le parole vicine o
# fermarsi a metà lasciando la coda dell'IBAN in chiaro.
IBAN_LEN = {c[:2]: int(c[2:]) for c in (
    "AD24 AE23 AL28 AT20 AZ28 BA20 BE16 BG22 BH22 BI27 BR29 BY28 CH21 CR22 CY28 CZ24 "
    "DE22 DJ27 DK18 DO28 EE20 EG29 ES24 FI18 FK18 FO18 FR27 GB22 GE22 GI23 GL18 GR27 "
    "GT28 HN28 HR21 HU28 IE22 IL23 IQ23 IS26 IT27 JO30 KW30 KZ20 LB28 LC32 LI21 LT20 "
    "LU20 LV21 LY25 MC27 MD24 ME22 MK19 MN20 MR27 MT31 MU30 NI28 NL18 NO15 OM23 PK24 "
    "PL28 PS29 PT25 QA29 RO24 RS22 RU33 SA24 SC31 SD18 SE24 SI19 SK24 SM27 SO23 ST25 "
    "SV28 TL23 TN24 TR26 UA29 VA22 VG24 XK20 YE30").split()}

# sigla e cifre di controllo attaccate: sono sempre stampate insieme, e pretenderlo
# evita che "il 17 luglio 2008" o "RO 48 2897 ..." si aggancino come IBAN.
_IBAN_HEAD = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}\d{2}")
_IBAN_MAX_SEP = 3                      # separatori di fila (PDF giustificati, tabelle)
_IBAN_RUN = 5                          # caratteri per gruppo: più lunghi sono parole


def _iban_char(c):
    """ASCII: senza isascii() la scansione inghiotte "età", "più", ... — le
    lettere accentate sono alnum, e attaccate a un IBAN entrerebbero nel
    conteggio dei caratteri."""
    return c.isascii() and c.isalnum()


# IT: CIN alfabetico + ABI e CAB numerici + 12 caratteri di conto. Serve a
# distinguere un IBAN italiano storpiato dall'OCR (mod-97 sbagliato ma struttura
# intatta: si copre lo stesso) da un codice qualunque lungo 27 caratteri.
_IT_IBAN = re.compile(r"IT\d{2}[A-Z]\d{10}[A-Z0-9]{12}")


def it_iban_ok(norm):
    """Struttura di un IBAN italiano, checksum a parte. `norm` già normalizzato."""
    return bool(_IT_IBAN.fullmatch(norm))


def scan_iban(text):
    """Candidati IBAN nel formato di stampa a gruppi: [(start, end, checksum_ok)].

    Separato da `detect_iban` perché il post-filtro sull'output del modello
    (core._clean_iban) ha bisogno ANCHE dei candidati con la forma e la lunghezza
    giuste e il mod-97 sbagliato: un IBAN storpiato dall'OCR resta un dato da
    coprire, mentre per la rete regex - che gira su tutto il testo - sarebbe un
    falso positivo. Un solo scanner per i due usi: due copie divergerebbero, e
    qui divergere significa lasciare in chiaro metà di un conto corrente.
    """
    out = []
    for m in _IBAN_HEAD.finditer(text):
        n, i = IBAN_LEN.get(m.group()[:2].upper()), m.start()
        if not n or (i and text[i - 1].isalnum()):
            continue
        chars, sep, run, nl, grp, start = [], 0, 0, 0, False, i
        while i < len(text) and len(chars) < n:
            c = text[i]
            if _iban_char(c):
                chars.append(c)
                run += 1
                sep = 0
            elif sep < _IBAN_MAX_SEP and run <= _IBAN_RUN and (c in ".-" or c.isspace()):
                sep, run, grp = sep + 1, 0, True
                nl += c.isspace() and c not in " \t "
                if nl > 1:
                    break              # un IBAN va a capo una volta; una colonna a ogni cella
            else:
                break
            i += 1
        if len(chars) < n or (i < len(text) and _iban_char(text[i])):
            continue                    # troncato, oppure il codice prosegue oltre
        if grp:
            g = [x for x in re.split(r"[\s.\-]+", text[start:i]) if x]
            if len(set(map(len, g[:-1]))) > 1 or len(g[-1]) > len(g[0]):
                continue                # gruppi disuguali: è prosa, non un IBAN stampato
        out.append((start, i, iban_ok(text[start:i])))
    return out


def detect_iban(text):
    """IBAN nel formato di stampa a gruppi ("IT60 X054 2811 ...").

    Dalla sigla del paese consuma esattamente i caratteri previsti da IBAN_LEN,
    tollerando i separatori; poi decide il mod-97.

    Uno scanner e non una regex perché il confine destro non è deducibile dalla
    forma: senza la lunghezza attesa il match o inghiotte la parola dopo, o si ferma
    a metà e lascia la coda dell'IBAN in chiaro sotto un placeholder rassicurante.
    """
    ents = [{"label": "IBAN", "start": s, "end": e,
             "score": 1.0, "validated": True, "source": "regex"}
            for s, e, ok in scan_iban(text) if ok]
    # due candidati sovrapposti: uno è l'artefatto di una scansione partita da un codice
    # vicino, ma quale dei due non è decidibile dal testo. Si maschera l'unione, perché
    # scartarne uno lascia in chiaro un pezzo dell'altro sotto un [IBAN_1] rassicurante.
    ents.sort(key=lambda e: e["start"])
    for a, b in zip(ents, ents[1:]):
        if b["start"] < a["end"]:
            b["start"], b["end"] = a["start"], max(a["end"], b["end"])
            a["end"] = a["start"]       # assorbito in b
    return [e for e in ents if e["end"] > e["start"]]


def detect_regex(text):
    """Entità della rete regex. validated=True solo quando il checksum passa."""
    national = detect_national_ids(text)
    ents = (detect_iban(text) + detect_eu_vat(text) + national + detect_credentials(text)
            + detect_cyber(text) + detect_devices(text))
    for label, rx, validator, strict in DETECTORS:
        for m in rx.finditer(text):
            start, end = m.start(), m.end()
            # un identificativo nazionale ha sempre etichetta o forma di stampa e
            # vince sul candidato più generico: una Steuer-ID di undici cifre non
            # è una partita IVA italiana, un SIRET di quattordici cifre Luhn non
            # è una carta di credito
            if label in ("PIVA", "CREDITCARDNUMBER", "CF") \
                    and any(start < n["end"] and end > n["start"] for n in national):
                continue
            if label == "URL":
                while end > start and text[end - 1] in _URL_TRAIL:
                    end -= 1
                if end <= start:
                    continue
            ok = validator(m.group(0)) if validator else False
            if validator and strict and not ok:
                continue
            ents.append({
                "label": label,
                "start": start,
                "end": end,
                "score": 1.0 if ok else 0.9,
                "validated": ok,
                "source": "regex",
            })
    # telefono per ultimo: un numero dentro un IBAN, una carta, un identificativo,
    # una data o un importo non è un telefono, anche quando il piano di
    # numerazione lo ammetterebbe. Si bloccano anche i candidati IBAN col
    # checksum rotto (sigla + cifre: mai un telefono); i candidati carta NON
    # validati no, perché "0033 6 12 34 56 78" ha quattordici cifre come una carta.
    blocked = [(e["start"], e["end"]) for e in ents]
    blocked += [(s, e) for s, e, _ in scan_iban(text)]
    ents += detect_phones(text, blocked)
    return ents



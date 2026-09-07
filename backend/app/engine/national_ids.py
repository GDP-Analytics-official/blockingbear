"""
Identificativi nazionali di persona e d'impresa fuori dall'Italia, per i paesi
delle lingue supportate (FR, DE, ES, NL, UK) e dei vicini che scrivono nelle
stesse lingue (BE, CH, AT). Due label, le stesse dell'Italia:

  CF     l'identificativo fiscale o previdenziale DELLA PERSONA: NIR francese
         (sécurité sociale) e numéro fiscal, Steuer-ID tedesca, DNI/NIE e NSS
         spagnoli, BSN olandese, NINO e UTR britannici, AHV/AVS svizzero,
         numero di registro nazionale belga, SVNR austriaco.
  PIVA   l'identificativo DELL'IMPRESA senza prefisso IVA: SIREN e SIRET,
         Steuernummer, CIF, KvK e RSIN, Companies House number, numero
         d'impresa belga. (Con il prefisso del paese - "FR" + chiave + SIREN -
         la trova già detectors.detect_eu_vat.)

Si riusano CF e PIVA e non si aprono tag nuovi: i placeholder delle chat
salvate ([CF_1], [PIVA_1]) restano decodificabili, il modello emette gli
stessi nomi, frontend e post-filtri non cambiano. La differenza la fa la
descrizione mostrata all'utente, non l'identificatore.

Tre regole di accettazione, decise per voce (colonna `cue_needed`):
  - forma specifica + checksum: basta il checksum (DNI, NIR, AHV, registro
    belga puntato). Se il checksum FALLISCE ma l'etichetta è lì davanti
    ("DNI 12345678X" battuto male) si copre lo stesso, non validato: un
    documento d'identità storpiato dall'OCR resta un dato da coprire.
  - forma generica (8-11 cifre nude) anche con checksum: l'etichetta è
    OBBLIGATORIA (BSN, UTR, SIREN, Steuer-ID compatta). Nove cifre a caso
    superano la prova dell'undici una volta su undici, e un numero di
    protocollo non deve diventare un BSN.
  - senza checksum (KvK, NINO, Steuernummer, Companies House): etichetta
    obbligatoria, mai validato. Fa eccezione la forma di stampa
    inconfondibile (NINO "AB 12 34 56 C", Steuer-ID "12 345 678 901").

Le collisioni con i formati che detectors.py già conosce (11 cifre della
partita IVA italiana, 14 cifre Luhn di una carta di credito) le risolve
`detectors.detect_regex`: un identificativo trovato qui ha sempre etichetta o
forma di stampa, e vince sul candidato più generico.

Come detectors.py il modulo lavora su stringhe, senza modello.
"""

import re

from .text_patterns import normalized_detector


# ---------------------------------------------------------------------------
# Checksum
# ---------------------------------------------------------------------------


def _luhn(d):
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


def _iso7064_mod11_10(d):
    """ISO 7064 MOD 11,10: cifra di controllo per la stringa di cifre `d`
    (senza la cifra di controllo)."""
    p = 10
    for c in d:
        s = (int(c) + p) % 10 or 10
        p = (2 * s) % 11
    return (11 - p) % 10


ES_NIF_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_ES_CIF_LETTERS = "JABCDEFGHI"


def es_id_ok(b):
    """Spagna: NIF (8 cifre + lettera), NIE (X/Y/Z + 7 cifre + lettera), CIF
    (lettera + 7 cifre + controllo). La lettera di controllo cambia tabella a
    seconda della forma. `b` normalizzato, maiuscolo, 9 caratteri."""
    if len(b) != 9:
        return False
    tail = b[8]
    if b[:8].isdigit():                                   # NIF: 8 cifre + lettera
        return tail == ES_NIF_LETTERS[int(b[:8]) % 23]
    if b[0] in "XYZ" and b[1:8].isdigit():                # NIE: X/Y/Z + 7 cifre + lettera
        return tail == ES_NIF_LETTERS[int(str("XYZ".index(b[0])) + b[1:8]) % 23]
    if not (b[0].isalpha() and b[1:8].isdigit()):
        return False
    tot = 0                                               # CIF: lettera + 7 cifre + controllo
    for i, c in enumerate(b[1:8]):
        n = int(c) * (2 if i % 2 == 0 else 1)
        tot += n - 9 if n > 9 else n
    d = (10 - tot % 10) % 10
    if b[0] in "KPQRSNW":
        return tail == _ES_CIF_LETTERS[d]
    if b[0] in "ABEH":
        return tail == str(d)
    return tail in (str(d), _ES_CIF_LETTERS[d])           # le altre accettano entrambe


def fr_nir_ok(d):
    """Francia, NIR: 13 cifre + chiave = 97 - (numero mod 97). La Corsica
    scrive il dipartimento 2A/2B: il calcolo INSEE sostituisce la lettera con
    zero e sottrae 1.000.000 / 2.000.000. Sostituire direttamente 2A/2B con
    19/18 è la stessa operazione, quindi non si deve sottrarre di nuovo."""
    if len(d) != 15:
        return False
    body, key = d[:13], d[13:]
    if not key.isdigit():
        return False
    dept = body[5:7]
    if dept == "2A":
        n = int(body.replace("2A", "19", 1))
    elif dept == "2B":
        n = int(body.replace("2B", "18", 1))
    elif body.isdigit():
        n = int(body)
    else:
        return False
    return 97 - n % 97 == int(key)


def de_steuer_id_ok(d):
    """Germania, steuerliche Identifikationsnummer: 11 cifre, la prima non
    zero; fra le prime dieci esattamente una cifra compare due o tre volte
    (se tre, non consecutive); l'undicesima è ISO 7064 MOD 11,10."""
    if len(d) != 11 or not d.isdigit() or d[0] == "0":
        return False
    head = d[:10]
    counts = {c: head.count(c) for c in set(head)}
    rep = [c for c, n in counts.items() if n > 1]
    if len(rep) != 1 or counts[rep[0]] > 3:
        return False
    if counts[rep[0]] == 3 and rep[0] * 3 in head:
        return False
    return _iso7064_mod11_10(head) == int(d[10])


def nl_elfproef_ok(d):
    """Paesi Bassi, BSN e RSIN: prova dell'undici con pesi 9..2 e -1 sull'ultima."""
    if len(d) == 8:
        d = "0" + d
    if len(d) != 9 or not d.isdigit() or len(set(d)) == 1:
        return False
    s = sum(int(c) * w for c, w in zip(d, (9, 8, 7, 6, 5, 4, 3, 2, -1)))
    return s % 11 == 0


def uk_utr_ok(d):
    """Regno Unito, UTR: 10 cifre, la prima è la cifra di controllo delle
    altre nove (pesi 6,7,8,9,10,5,4,3,2; tabella "21987654321" sul mod 11)."""
    if len(d) != 10 or not d.isdigit():
        return False
    s = sum(int(c) * w for c, w in zip(d[1:], (6, 7, 8, 9, 10, 5, 4, 3, 2)))
    return d[0] == "21987654321"[s % 11]


def es_nss_ok(d):
    """Spagna, numero di Seguridad Social: provincia (2) + numero (8) +
    controllo (2) = (provincia·10^8 + numero) mod 97; se il numero è sotto
    i dieci milioni si moltiplica la provincia per 10^7."""
    if len(d) != 12 or not d.isdigit():
        return False
    prov, num, ctrl = int(d[:2]), int(d[2:10]), int(d[10:])
    if not 1 <= prov <= 56:                              # codici provinciali TGSS
        return False
    n = prov * 10000000 + num if num < 10000000 else prov * 100000000 + num
    return n % 97 == ctrl


def ch_ahv_ok(d):
    """Svizzera, AHV/AVS: EAN-13 con prefisso 756."""
    if len(d) != 13 or not d.isdigit() or not d.startswith("756"):
        return False
    s = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(d[:12]))
    return (10 - s % 10) % 10 == int(d[12])


def be_national_ok(d):
    """Belgio, numero di registro nazionale: 11 cifre, le ultime due sono
    97 - (prime nove mod 97); per i nati dal 2000 si antepone un 2 alle nove."""
    if len(d) != 11 or not d.isdigit():
        return False
    body, key = int(d[:9]), int(d[9:])
    return 97 - body % 97 == key or 97 - int("2" + d[:9]) % 97 == key


def be_enterprise_ok(d):
    """Belgio, numero d'impresa (KBO/BCE): 10 cifre, le ultime due sono
    97 - (prime otto mod 97). È lo stesso corpo della partita IVA BE."""
    if len(d) != 10 or not d.isdigit() or d[0] not in "01":
        return False
    return 97 - int(d[:8]) % 97 == int(d[8:])


def at_svnr_ok(d):
    """Austria, Sozialversicherungsnummer: 10 cifre, la quarta è la cifra di
    controllo (pesi 3,7,9,0,5,8,4,2,1,6 mod 11)."""
    if len(d) != 10 or not d.isdigit() or d[0] == "0":
        return False
    s = sum(int(c) * w for c, w in zip(d, (3, 7, 9, 0, 5, 8, 4, 2, 1, 6)))
    return s % 11 == int(d[3])


# ---------------------------------------------------------------------------
# Etichette (cue) davanti al numero
# ---------------------------------------------------------------------------
CUE_BACK = 128                          # caratteri di contesto a sinistra

# fra l'etichetta e il numero: separatori, "n°", "nr.", "de", "is"...
_CUE_TAIL = re.compile(
    r"(?:[\s:.\-()/#«»\"']|(?<![\w])(?:n[°º]?|nr|no|num|nummer|number|num[ée]ro|n[úu]mero|numero|de|d[’']|du|der"
    r"|des|die|dem|di|del|della|van|het|the|is|ist|est|es|lautet|luidt|è|e'|sono|sind|are|sont|son|zijn|:)\.?)+$",
    re.IGNORECASE)


def _cue(words):
    return re.compile(r"(?<![\w])(?:" + words + r")$", re.IGNORECASE)


def has_cue(cue_rx, text, i):
    """C'è l'etichetta `cue_rx` subito prima della posizione `i` (separatori
    e parole di raccordo a parte)?"""
    window = text[max(0, i - CUE_BACK):i]
    tail = _CUE_TAIL.sub("", window)
    return bool(cue_rx.search(tail))


_WORD = r"[A-Za-zÀ-ÿ\-]+"

CUE_FR_NIR = _cue(r"n(?:um[ée]ro|[°º])?[ \t.]*(?:de[ \t]+)?s[ée]cu(?:rit[ée])?(?:[ \t]+sociale)?|NIR|s[ée]cu"
                  r"|s[ée]curit[ée][ \t]+sociale|num[ée]ro[ \t]+d[’']assur[ée](?:[ \t]+social)?|n[°º][ \t]*SS|NISS|INSZ"
                  r"|num[ée]ro[ \t]+d[’']immatriculation(?:[ \t]+(?:à[ \t]+la[ \t]+)?s[ée]curit[ée][ \t]+sociale)?")
CUE_FR_SPI = _cue(r"num[ée]ro[ \t]+fiscal(?:[ \t]+de[ \t]+r[ée]f[ée]rence)?|identifiant[ \t]+fiscal|SPI|n[°º][ \t]*fiscal"
                  r"|num[ée]ro[ \t]+d[’']identification[ \t]+fiscale?")
CUE_FR_SIREN = _cue(r"SIRENE?|RCS(?:[ \t]+" + _WORD + r"){0,2}|RM(?:[ \t]+" + _WORD + r")?"
                    r"|immatricul[ée]e?(?:[ \t]+au[ \t]+RCS(?:[ \t]+de)?(?:[ \t]+" + _WORD + r")?)?")
CUE_FR_SIRET = _cue(r"SIRET")
CUE_DE_STEUER_ID = _cue(r"steuer-?id(?:entifikationsnummer|entnummer|nr\.?)?|steuerliche[ \t]+identifikationsnummer"
                        r"|identifikationsnummer|idnr\.?|TIN|steuer-?identifikationsnummer|pers[öo]nliche[ \t]+identifikationsnummer")
CUE_DE_STEUERNUMMER = _cue(r"steuernummer|steuer-?nr\.?|st\.?-?nr\.?|stnr\.?|steuernr\.?")
CUE_ES_NIF = _cue(r"DNI|NIF|NIE|N\.I\.F\.?|D\.N\.I\.?|N\.I\.E\.?|documento[ \t]+nacional(?:[ \t]+de[ \t]+identidad)?"
                  r"|n[úu]mero[ \t]+de[ \t]+identificaci[óo]n(?:[ \t]+fiscal)?|identificaci[óo]n[ \t]+fiscal|c[ée]dula"
                  r"|n[úu]mero[ \t]+de[ \t]+identidad[ \t]+de[ \t]+extranjero")
CUE_ES_CIF = _cue(r"CIF|C\.I\.F\.?|NIF|N\.I\.F\.?|c[óo]digo[ \t]+de[ \t]+identificaci[óo]n[ \t]+fiscal")
CUE_ES_NSS = _cue(r"NSS|NAF|n[úu]mero[ \t]+de[ \t]+(?:la[ \t]+)?seguridad[ \t]+social|seguridad[ \t]+social"
                  r"|n[úu]mero[ \t]+de[ \t]+afiliaci[óo]n|afiliaci[óo]n")
CUE_NL_BSN = _cue(r"BSN|burgerservicenummer|burgerservicenr\.?|sofi-?nummer|sofinummer|persoonsnummer|burgerservice")
CUE_NL_RSIN = _cue(r"RSIN|fiscaal[ \t]+nummer|rechtspersonen[ \t]+en[ \t]+samenwerkingsverbanden[ \t]+informatienummer")
CUE_NL_KVK = _cue(r"KvK(?:-?nummer|-?nr\.?)?|K\.v\.K\.?|kamer[ \t]+van[ \t]+koophandel|handelsregister(?:nummer)?|HR-?nummer"
                  r"|inschrijvingsnummer")
CUE_UK_NINO = _cue(r"NI(?:NO)?(?:[ \t]+(?:number|no\.?))?|national[ \t]+insurance(?:[ \t]+(?:number|no\.?))?")
CUE_UK_UTR = _cue(r"UTR|unique[ \t]+tax(?:payer)?[ \t]+reference|tax(?:payer)?[ \t]+reference(?:[ \t]+number)?")
# "number", "no." e simili li toglie già _CUE_TAIL: qui resta la testa
CUE_UK_COMPANY = _cue(r"company(?:[ \t]+registration)?|companies[ \t]+house"
                      r"|registered(?:[ \t]+in[ \t]+england(?:[ \t]+and[ \t]+wales)?|[ \t]+in[ \t]+scotland)?"
                      r"|reg(?:istration)?\.?|CRN")
CUE_CH_AHV = _cue(r"AHV|AVS|AHV-?(?:nummer|nr\.?)|AVS-?(?:nummer|nr\.?)|num[ée]ro[ \t]+AVS|n[°º][ \t]*AVS|numero[ \t]+AVS"
                  r"|sozialversicherungsnummer|versichertennummer|versicherten-?nr\.?")
CUE_BE_NATIONAL = _cue(r"rijksregister(?:nummer)?|registre[ \t]+national|num[ée]ro[ \t]+national|nationaal[ \t]+nummer"
                       r"|NISS|INSZ|num[ée]ro[ \t]+de[ \t]+registre[ \t]+national|RRN|NN|identificatienummer[ \t]+van[ \t]+"
                       r"(?:de[ \t]+)?sociale[ \t]+zekerheid|num[ée]ro[ \t]+d[’']identification[ \t]+(?:de[ \t]+la[ \t]+)?"
                       r"s[ée]curit[ée][ \t]+sociale")
CUE_BE_ENTERPRISE = _cue(r"KBO|BCE|ondernemingsnummer|num[ée]ro[ \t]+d[’']entreprise|n[°º][ \t]*d[’']entreprise"
                         r"|entreprise[ \t]+n[°º]|BTW|TVA|ondernemingsnr\.?")
CUE_AT_SVNR = _cue(r"SVNR|SV-?Nr\.?|SV-?Nummer|sozialversicherungsnummer|versicherungsnummer|versicherungs-?nr\.?"
                   r"|versichertennummer")

# ---------------------------------------------------------------------------
# Tabella: (label, nome, regex, validatore, cue, cue_needed(raw) -> bool)
#   - regex: gruppo "v" col valore, confini di parola ai lati
#   - validatore: sul valore NORMALIZZATO (separatori tolti, maiuscolo) o None
#   - cue_needed: l'etichetta è obbligatoria per QUESTA forma di stampa?
# ---------------------------------------------------------------------------
_B = r"(?<![\w])"                       # confine sinistro
_E = r"(?![\w])"                        # confine destro
_HSP = r"[ \u00a0\u202f]"               # spazi normali/non separabili dei PDF
_SEP = r"[ .\-\u00a0\u202f]?"


def _compact(raw):
    """Nessun separatore: la forma nuda, quella che collide con tutto."""
    return re.sub(r"\d", "", raw) == ""


def _always(raw):
    return True


def _never(raw):
    return False


RULES = [
    # US SSN: explicit cue required; format checks do not prove issuance.
    # https://secure.ssa.gov/poms.nsf/lnx/0110201035
    ("CF", "US_SSN",
     re.compile(_B + r"(?P<v>(?!000|666|9)[0-9]{3}(?P<sep>[- ]?)"
                r"(?!00)[0-9]{2}(?P=sep)(?!0000)[0-9]{4})" + _E),
     None, _cue(r"SSN|social[ \t]+security(?:[ \t]+number)?"), _always),
    # --- Francia ---
    ("CF", "FR_NIR",
     re.compile(_B + r"(?P<v>[12]" + _SEP + r"\d{2}" + _SEP + r"\d{2}" + _SEP + r"(?:\d{2}|2[AB])" + _SEP
                + r"\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{2})" + _E),
     fr_nir_ok, CUE_FR_NIR, _never),
    ("CF", "FR_SPI",
     re.compile(_B + r"(?P<v>\d{2}" + _SEP + r"\d{2}" + _SEP + r"\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{3})" + _E),
     None, CUE_FR_SPI, _always),
    ("PIVA", "FR_SIRET",
     re.compile(_B + r"(?P<v>\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{5})" + _E),
     _luhn, CUE_FR_SIRET, _always),
    ("PIVA", "FR_SIREN",
     re.compile(_B + r"(?P<v>\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{3})" + _E),
     _luhn, CUE_FR_SIREN, _always),
    # --- Germania ---
    # a gruppi 2-3-3-3 è la forma ufficiale di stampa e non è una partita IVA
    # italiana; compatta serve l'etichetta
    ("CF", "DE_STEUER_ID",
     re.compile(_B + r"(?P<v>[1-9]\d(?:" + _HSP + r"\d{3}){3}|[1-9]\d{10})" + _E),
     de_steuer_id_ok, CUE_DE_STEUER_ID, _compact),
    ("PIVA", "DE_STEUERNUMMER",
     re.compile(_B + r"(?P<v>\d{1,3}/\d{3,4}/\d{4,5}|\d{10,13})" + _E),
     None, CUE_DE_STEUERNUMMER, _always),
    # --- Spagna ---
    ("CF", "ES_NIF",
     re.compile(_B + r"(?P<v>(?:\d{8}|\d{2}\.\d{3}\.\d{3})[ .\-\u00a0\u202f]?[A-Z]|[XYZ][ .\-\u00a0\u202f]?\d{7}[ .\-\u00a0\u202f]?[A-Z])" + _E),
     es_id_ok, CUE_ES_NIF, _never),
    ("PIVA", "ES_CIF",
     re.compile(_B + r"(?P<v>[ABCDEFGHJNPQRSUVW][ .\-\u00a0\u202f]?\d{7}[ .\-\u00a0\u202f]?[0-9A-J])" + _E),
     es_id_ok, CUE_ES_CIF, _always),
    ("CF", "ES_NSS",
     re.compile(_B + r"(?P<v>\d{2}[ /\-\u00a0\u202f]?\d{8}[ /\-\u00a0\u202f]?\d{2})" + _E),
     es_nss_ok, CUE_ES_NSS, _always),
    # --- Paesi Bassi ---
    ("CF", "NL_BSN",
     re.compile(_B + r"(?P<v>\d{4}\.\d{2}\.\d{3}|\d{8,9})" + _E),
     nl_elfproef_ok, CUE_NL_BSN, _always),
    ("PIVA", "NL_RSIN",
     re.compile(_B + r"(?P<v>\d{9})" + _E),
     nl_elfproef_ok, CUE_NL_RSIN, _always),
    ("PIVA", "NL_KVK",
     re.compile(_B + r"(?P<v>\d{8})" + _E),
     None, CUE_NL_KVK, _always),
    # --- Regno Unito ---
    ("CF", "UK_NINO",
     re.compile(_B + r"(?P<v>(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]"
                r"(?:" + _HSP + r")?\d{2}(?:" + _HSP + r")?\d{2}(?:" + _HSP
                + r")?\d{2}(?:" + _HSP + r")?[A-D])"
                + _E),
     None, CUE_UK_NINO, lambda raw: not any(c.isspace() for c in raw)),
    ("CF", "UK_UTR",
     re.compile(_B + r"(?P<v>\d{5}(?:" + _HSP + r")?\d{5})" + _E),
     uk_utr_ok, CUE_UK_UTR, _always),
    ("PIVA", "UK_COMPANY",
     re.compile(_B + r"(?P<v>(?:[A-Z]{2}\d{6}|\d{8}))" + _E),
     None, CUE_UK_COMPANY, _always),
    # --- Svizzera ---
    ("CF", "CH_AHV",
     re.compile(_B + r"(?P<v>756\.?\d{4}\.?\d{4}\.?\d{2})" + _E),
     ch_ahv_ok, CUE_CH_AHV, _never),
    # --- Belgio ---
    ("CF", "BE_NATIONAL",
     re.compile(_B + r"(?P<v>\d{2}\.\d{2}\.\d{2}[\-.]\d{3}\.\d{2}|\d{11})" + _E),
     be_national_ok, CUE_BE_NATIONAL, _compact),
    ("PIVA", "BE_ENTERPRISE",
     re.compile(_B + r"(?P<v>[01]\d{3}\.\d{3}\.\d{3}|[01]\d{9})" + _E),
     be_enterprise_ok, CUE_BE_ENTERPRISE, _compact),
    # --- Austria ---
    ("CF", "AT_SVNR",
     re.compile(_B + r"(?P<v>\d{4}(?:" + _HSP + r")?\d{6})" + _E),
     at_svnr_ok, CUE_AT_SVNR, _always),
]


RULES = [(label, name, re.compile(rx.pattern, rx.flags | re.I), validator, cue, needed)
         for label, name, rx, validator, cue, needed in RULES]


def _norm(raw):
    return re.sub(r"[ .\-/\u00a0\u202f]", "", raw).upper()


def scan_national_ids(text):
    """Candidati: [(start, end, label, nome, validated)]. Espone anche la voce
    della tabella, per i test e per il post-filtro."""
    out = []
    for label, name, rx, validator, cue_rx, cue_needed in RULES:
        for m in rx.finditer(text):
            raw = m.group("v")
            norm = _norm(raw)
            ok = bool(validator(norm)) if validator else False
            cued = has_cue(cue_rx, text, m.start("v"))
            if validator and not ok and not cued:
                continue                # checksum fallito e nessuna etichetta: non è lui
            if cue_needed(raw) and not cued:
                continue                # forma generica senza etichetta: non basta
            out.append((m.start("v"), m.end("v"), label, name, ok))
    return out


@normalized_detector
def detect_national_ids(text):
    """Entità CF / PIVA estere, nella forma di `detect_regex`. Sovrapposizioni:
    vince il validato, poi il più lungo."""
    cands = sorted(scan_national_ids(text), key=lambda c: (-int(c[4]), -(c[1] - c[0]), c[0]))
    kept = []
    for s, e, label, name, ok in cands:
        if any(s < ke and e > ks for ks, ke, *_ in kept):
            continue
        kept.append((s, e, label, name, ok))
    return [{"label": label, "start": s, "end": e, "score": 1.0 if ok else 0.9,
             "validated": ok, "source": "regex"}
            for s, e, label, _, ok in sorted(kept)]

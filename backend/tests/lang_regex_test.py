"""Rete regex nelle lingue supportate (IT, EN, FR, DE, ES, NL) e nei paesi
corrispondenti: telefoni, identificativi nazionali, URL con ccTLD, fusione.

  - `engine/phones.py`: TELEPHONENUM con la libreria phonenumbers sui piani
    di numerazione IT/FR/DE/ES/NL/GB/BE/CH/AT/US; la forma sola basta con il
    prefisso internazionale, lo zero del piano nazionale o il cellulare
    italiano, altrimenti serve l'etichetta (tel., Telefon, móvil...); un numero
    dentro un IBAN, una carta, un identificativo o una data non è un telefono;
  - `engine/national_ids.py`: CF e PIVA fuori dall'Italia (NIR, Steuer-ID,
    DNI/NIE/CIF, BSN/KvK, NINO/UTR, AHV, registro belga, SVNR...): checksum,
    etichetta obbligatoria dove la forma è generica, precedenza sulla partita
    IVA italiana e sulla carta di credito;
  - detector URL: ccTLD (fr, de, es, nl, uk, co.uk...) con etichetta di almeno
    tre caratteri e suffisso minuscolo, così "p.es." e "i.e." restano prosa;
  - fusione con `core`: regex validata batte il modello, placeholder e
    decodifica, tags().

I valori con checksum sono COSTRUITI qui dai validatori (nessun dato reale).
Le parole chiave nelle quattro lingue di credenziali, dispositivi e 2FA sono
nei rispettivi test (credentials_test, devices_test, cyber_test).

Uso:  python backend/tests/lang_regex_test.py
"""
import io
import itertools
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.engine import core, national_ids as ni, phones  # noqa: E402
from app.engine.detectors import EU_VAT, detect_eu_vat, detect_regex, piva_ok  # noqa: E402

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {msg}")


def fix(prefix, n, ok, alphabet="0123456789"):
    """Completa `prefix` con n caratteri fino a superare il validatore `ok`."""
    for tail in itertools.product(alphabet, repeat=n):
        c = prefix + "".join(tail)
        if ok(c):
            return c
    raise AssertionError("nessun valore valido per " + prefix)


def found(text, label=None):
    return [(e["label"], text[e["start"]:e["end"]], e["validated"])
            for e in sorted(detect_regex(text), key=lambda e: e["start"])
            if label is None or e["label"] == label]


def values(text, label):
    return [v for _, v, _ in found(text, label)]


# --- identificativi costruiti dai validatori ---------------------------------
NIR = fix("1850578006084", 2, ni.fr_nir_ok)
NIR_F = f"{NIR[0]} {NIR[1:3]} {NIR[3:5]} {NIR[5:7]} {NIR[7:10]} {NIR[10:13]} {NIR[13:]}"
NIR_NBSP = NIR_F.replace(" ", "\u00a0")
def nir_corsica(body):
    """Chiave costruita dalla formula INSEE, indipendente dal validatore."""
    number = int(body.replace("2A", "19").replace("2B", "18"))
    return body + f"{97 - number % 97:02d}"


NIR_CORSICA = nir_corsica("2" + "85" + "05" + "2A" + "004" + "012")
NIR_CORSICA_B = nir_corsica("1" + "85" + "05" + "2B" + "004" + "012")
STID = fix("6598237", 4, ni.de_steuer_id_ok)
STID_F = f"{STID[:2]} {STID[2:5]} {STID[5:8]} {STID[8:]}"
STID_NBSP = STID_F.replace(" ", "\u00a0")
BSN = fix("1234567", 2, ni.nl_elfproef_ok)
UTR = fix("", 1, lambda d: ni.uk_utr_ok(d + "234567890")) + "234567890"
SIREN = fix("73282", 4, ni._luhn)
SIRET = fix(SIREN + "000", 2, lambda d: len(d) == 14 and ni._luhn(d))
DNI = fix("12345678", 1, ni.es_id_ok, "TRWAGMYFPDXBNJZSQVHLCKE")
NIE = fix("X1234567", 1, ni.es_id_ok, "TRWAGMYFPDXBNJZSQVHLCKE")
CIF = fix("B1234567", 1, ni.es_id_ok, "0123456789ABCDEFGHIJ")
NSS = fix("2812345678", 2, ni.es_nss_ok)
AHV = "756.9217.0769.85"                                               # esempio ufficiale
BEN = fix("850730033", 2, ni.be_national_ok)
BEN_F = f"{BEN[:2]}.{BEN[2:4]}.{BEN[4:6]}-{BEN[6:9]}.{BEN[9:]}"
BEE = fix("04081209", 2, ni.be_enterprise_ok)
BEE_F = f"{BEE[:4]}.{BEE[4:7]}.{BEE[7:]}"
SVNR = fix("123", 1, lambda d: ni.at_svnr_ok(d + "010180")) + "010180"
PIVA_IT = fix("1234567890", 1, piva_ok)
VAT_FR_SIREN = fix("12345001", 1, ni._luhn)
VAT_FR = "FR" + fix("", 2, lambda key: EU_VAT["FR"][1](key + VAT_FR_SIREN)) + VAT_FR_SIREN
VAT_DE = "DE" + fix("12345678", 1, EU_VAT["DE"][1])
VAT_NL = "NL" + BSN + "B12"
CF, PIVA, TEL, URL = "CF", "PIVA", "TELEPHONENUM", "URL"

# (testo, telefoni attesi)
PHONES = [
    # Numeri dimostrativi pubblici dei metadati libphonenumber: sequenze
    # riconoscibili, non contatti di persone o imprese.
    # italiano: com'era
    ("Tel. +39 312 345 6789 e fisso 02 1234 5678", ["+39 312 345 6789", "02 1234 5678"]),
    ("cell 312 345 6789 e 312-345-6789", ["312 345 6789", "312-345-6789"]),
    # forma internazionale: sempre
    ("Tél. +33 6 12 34 56 78", ["+33 6 12 34 56 78"]),
    ("Telefon +49 30 123456", ["+49 30 123456"]),
    ("Teléfono +34 612 34 56 78", ["+34 612 34 56 78"]),
    ("Telefoon +31 6 12345678", ["+31 6 12345678"]),
    ("Phone +44 121 234 5678", ["+44 121 234 5678"]),
    ("0033 6 12 34 56 78", ["0033 6 12 34 56 78"]),
    # zero del piano nazionale: basta
    ("appelez le 06 12 34 56 78 ou le 01 42 68 53 00", ["06 12 34 56 78", "01 42 68 53 00"]),
    ("Handy 01512 3456789, Fax 030/123456", ["01512 3456789", "030/123456"]),
    ("bel 06-12345678 of 010-1234567", ["06-12345678", "010-1234567"]),
    ("07400 123456 or 0121 234 5678", ["07400 123456", "0121 234 5678"]),
    # senza zero (ES, US): solo con l'etichetta
    ("móvil 612 34 56 78", ["612 34 56 78"]),
    ("llámame al 612 345 678", ["612 345 678"]),
    ("Phone (201) 555-0123", ["(201) 555-0123"]),
    ("Tél. +33\u00a06\u00a012\u00a034\u00a056\u00a078", ["+33\u00a06\u00a012\u00a034\u00a056\u00a078"]),
    ("factura 912345678", []),
    ("ref 201-555-0123", []),
    # numeri che non sono telefoni
    ("SIREN 303 265 045", []),                            # PIVA con etichetta
    ("Kundennummer 4711-0815", []),
    ("il 12/03/2024 alle 14:30, totale € 1.234,56, CAP 20121, 2024-03-12", []),
    ("IBAN DE89 3704 0044 0532 0130 00", []),
    ("carta 4111 1111 1111 1111", []),
    (f"Steuer-ID {STID_F}", []),
    ("fattura n. 2024/00123 del 03/09/2026", []),
]

# (testo, [(label, valore, validated)])
IDS = [
    # Francia
    (f"N° de sécurité sociale : {NIR_F}", [(CF, NIR_F, True)]),
    (f"NIR {NIR}", [(CF, NIR, True)]),
    (f"NIR {NIR_NBSP}", [(CF, NIR_NBSP, True)]),                         # spazi non separabili da PDF
    (f"nudo {NIR_F}", [(CF, NIR_F, True)]),                                # forma + checksum bastano
    (f"numéro de sécurité sociale {NIR_CORSICA}", [(CF, NIR_CORSICA, True)]),
    (f"numéro de sécurité sociale {NIR_CORSICA_B}", [(CF, NIR_CORSICA_B, True)]),
    (f"n° sécu {NIR[:-1]}{'0' if NIR[-1] != '0' else '1'}",              # checksum rotto, ma etichetta
     [(CF, NIR[:-1] + ("0" if NIR[-1] != "0" else "1"), False)]),
    (f"SIREN {SIREN}", [(PIVA, SIREN, True)]),
    (f"RCS Paris {SIREN[:3]} {SIREN[3:6]} {SIREN[6:]}", [(PIVA, f"{SIREN[:3]} {SIREN[3:6]} {SIREN[6:]}", True)]),
    (f"SIRET {SIRET}", [(PIVA, SIRET, True)]),
    (f"nudo {SIREN}", []),                                                  # nove cifre senza etichetta
    ("numéro fiscal : 12 34 567 890 123", [(CF, "12 34 567 890 123", False)]),
    (f"TVA intracom {VAT_FR}", [(PIVA, VAT_FR, True)]),                    # com'era
    # Germania
    (f"Steuer-ID {STID}", [(CF, STID, True)]),
    (f"IdNr. {STID_F}", [(CF, STID_F, True)]),
    (f"IdNr. {STID_NBSP}", [(CF, STID_NBSP, True)]),                     # spazi non separabili da PDF
    (f"nudo {STID_F}", [(CF, STID_F, True)]),                              # forma di stampa 2-3-3-3
    (f"nudo compatto {STID}", []),                                          # undici cifre nude
    ("Steuernummer 21/815/08150", [(PIVA, "21/815/08150", False)]),
    (f"USt-IdNr. {VAT_DE}", [(PIVA, VAT_DE, True)]),
    # Spagna
    (f"DNI {DNI}", [(CF, DNI, True)]),
    (f"nudo {DNI}", [(CF, DNI, True)]),
    (f"nudo {DNI[:2]}.{DNI[2:5]}.{DNI[5:8]}-{DNI[8]}", [(CF, f"{DNI[:2]}.{DNI[2:5]}.{DNI[5:8]}-{DNI[8]}", True)]),
    (f"nudo {DNI[:-1]}A", []),                                              # lettera sbagliata
    (f"DNI {DNI[:-1]}A", [(CF, DNI[:-1] + "A", False)]),                   # etichetta: coperto, non validato
    (f"NIE {NIE}", [(CF, NIE, True)]),
    (f"CIF {CIF}", [(PIVA, CIF, True)]),
    (f"C.I.F. {CIF[0]}-{CIF[1:]}", [(PIVA, f"{CIF[0]}-{CIF[1:]}", True)]),
    (f"nudo {CIF}", []),
    (f"NSS {NSS[:2]} {NSS[2:10]} {NSS[10:]}", [(CF, f"{NSS[:2]} {NSS[2:10]} {NSS[10:]}", True)]),
    (f"ES{CIF}", [(PIVA, f"ES{CIF}", True)]),                               # con prefisso: detect_eu_vat
    # Paesi Bassi
    (f"BSN {BSN}", [(CF, BSN, True)]),
    (f"Burgerservicenummer: {BSN[:4]}.{BSN[4:6]}.{BSN[6:]}", [(CF, f"{BSN[:4]}.{BSN[4:6]}.{BSN[6:]}", True)]),
    (f"nudo {BSN}", []),
    (f"RSIN {BSN}", [(PIVA, BSN, True)]),
    ("KvK-nummer 12345678", [(PIVA, "12345678", False)]),
    ("Kamer van Koophandel 12345678", [(PIVA, "12345678", False)]),
    (f"BTW-nummer {VAT_NL}", [(PIVA, VAT_NL, True)]),                      # com'era: mod 11 storico
    # Regno Unito
    ("NI number AB123456C", [(CF, "AB123456C", False)]),
    ("nudo AB 12 34 56 C", [(CF, "AB 12 34 56 C", False)]),
    ("nudo AB123456C", []),
    ("National Insurance No: QQ 12 34 56 C", []),                           # Q non è un prefisso ammesso
    (f"UTR {UTR}", [(CF, UTR, True)]),
    (f"UTR {UTR[:5]} {UTR[5:]}", [(CF, f"{UTR[:5]} {UTR[5:]}", True)]),
    ("Company No. 01234567", [(PIVA, "01234567", False)]),
    ("Registered in England and Wales No. 01234567", [(PIVA, "01234567", False)]),
    # Svizzera, Belgio, Austria
    (f"AHV {AHV}", [(CF, AHV, True)]),
    (f"nudo {AHV}", [(CF, AHV, True)]),
    (f"Rijksregisternummer {BEN}", [(CF, BEN, True)]),
    (f"nudo {BEN_F}", [(CF, BEN_F, True)]),
    (f"nudo {BEN}", []),
    (f"KBO {BEE_F}", [(PIVA, BEE_F, True)]),
    (f"nudo {BEE_F}", [(PIVA, BEE_F, True)]),
    (f"SVNR {SVNR[:4]} {SVNR[4:]}", [(CF, f"{SVNR[:4]} {SVNR[4:]}", True)]),
    # Italia: com'era
    (f"P.IVA {PIVA_IT}", [(PIVA, PIVA_IT, True)]),
    (f"nuda {PIVA_IT}", [(PIVA, PIVA_IT, True)]),
    ("CF RSSMRA85M01H501Z", [(CF, "RSSMRA85M01H501Z", False)]),
]

# (testo, URL attesi)
URLS = [
    ("www.example.fr e https://kanzlei.de/impressum", ["www.example.fr", "https://kanzlei.de/impressum"]),
    ("kanzlei.de, empresa.es, bedrijf.nl, shop.co.uk, site.tv, startup.ai", ["kanzlei.de", "empresa.es", "bedrijf.nl", "shop.co.uk", "site.tv", "startup.ai"]),
    ("acme.it e portale.eu", ["acme.it", "portale.eu"]),                    # com'era
    ("p.es. questo, i.e. quello, u.a. Berlin, z.B. così", []),
    ("x.de e s.co sono troppo corti", []),
    ("Kanzlei.DE maiuscolo e qui.De attaccato", []),
    ("vgl. S. 12 f. der Akte", []),
    ("p.iva e n.ro e S.r.l. restano prosa", []),
]


def main():
    print("[A] telefoni")
    if phones.PhoneNumberMatcher is None:
        print("  (phonenumbers non installato: si verifica solo il ripiego italiano)")
    for text, expect in PHONES:
        if phones.PhoneNumberMatcher is None and not text.startswith(("Tel. +39", "cell")):
            continue
        got = values(text, TEL)
        check(got == expect, f"{text!r}\n        atteso {expect}\n        trovato {got}")
    check(phones.written_as_phone("+33 6 12 34 56 78", "FR"), "internazionale")
    check(phones.written_as_phone("06 12 34 56 78", "FR"), "zero nazionale FR")
    check(not phones.written_as_phone("612 34 56 78", "ES"), "ES senza zero: serve l'etichetta")
    check(not phones.written_as_phone("(201) 555-0123", "US"), "US: serve l'etichetta")
    check(phones.written_as_phone("312 345 6789", "IT"), "cellulare italiano")
    check(not phones.written_as_phone("303 265 045", "FR"), "nove cifre nude: no")
    check(phones.has_phone_cue("Telefon: ", 9) and phones.has_phone_cue("llámame al ", 11)
          and not phones.has_phone_cue("Kundennummer ", 13), "etichette")

    print("[B] identificativi nazionali")
    for text, expect in IDS:
        got = [(l, v, ok) for l, v, ok in found(text) if l in (CF, PIVA)]
        check(got == expect, f"{text!r}\n        atteso {expect}\n        trovato {got}")
    # validatori: casi noti
    check(ni.es_id_ok(DNI) and not ni.es_id_ok(DNI[:-1] + "A"), "DNI")
    check(ni.ch_ahv_ok("7569217076985") and not ni.ch_ahv_ok("7569217076986"), "AHV")
    check(ni.fr_nir_ok(NIR) and ni.fr_nir_ok(NIR_CORSICA) and ni.fr_nir_ok(NIR_CORSICA_B)
          and not ni.fr_nir_ok(NIR[:-1] + ("0" if NIR[-1] != "0" else "1")), "NIR e Corsica")
    check(ni.de_steuer_id_ok(STID) and not ni.de_steuer_id_ok("12345678901"), "Steuer-ID (nessuna cifra ripetuta)")
    check(ni.nl_elfproef_ok(BSN) and not ni.nl_elfproef_ok("123456789"), "BSN")
    check(ni.uk_utr_ok(UTR) and ni.uk_utr_ok("2234567890") and not ni.uk_utr_ok("1234567890"),
          "UTR (vettori pubblici HMRC)")
    invalid_nss_province = "0012345678" + f"{12345678 % 97:02d}"
    check(not ni.es_nss_ok(invalid_nss_province), "NSS: provincia da 01 a 56")
    check(not ni.at_svnr_ok("0015010180"), "SVNR: primo progressivo diverso da zero")
    check(ni.be_national_ok(BEN) and ni.be_enterprise_ok(BEE) and ni.at_svnr_ok(SVNR) and ni.es_nss_ok(NSS),
          "BE/AT/ES")
    # detect_eu_vat usa lo stesso controllo spagnolo
    vat = [(e["validated"]) for e in detect_eu_vat(f"ES{CIF} e ES{DNI}")]
    check(vat == [True, True], f"vat_es via national_ids: {vat}")

    print("[C] URL con ccTLD")
    for text, expect in URLS:
        got = values(text, URL)
        check(got == expect, f"{text!r}\n        atteso {expect}\n        trovato {got}")
    # la lista dei TLD è una sola per URL e HOSTNAME
    from app.engine import devices, lexicon
    check(devices._PUBLIC_TLD is lexicon.PUBLIC_TLD, "PUBLIC_TLD condivisa")
    check(values("server web www.acme.es e host srv-01", URL) == ["www.acme.es"]
          and [v for _, v, _ in found("server web www.acme.es e host srv-01", "HOSTNAME")] == ["srv-01"],
          "URL resta URL, HOSTNAME non lo contende")

    print("[D] fusione con core")

    def merged(text, model=()):
        kept = core._merge(list(model) + detect_regex(text), text)
        return {(e["label"], text[e["start"]:e["end"]]) for e in kept}

    vals = merged(f"Steuer-ID {STID_F}")
    check(vals == {(CF, STID_F)}, f"Steuer-ID a gruppi: CF, niente PIVA né telefono: {vals}")
    vals = merged(f"SIRET {SIRET}")
    check(vals == {(PIVA, SIRET)}, f"SIRET: PIVA, non carta di credito: {vals}")
    vals = merged(f"P.IVA {PIVA_IT} e DNI {DNI}")
    check(vals == {(PIVA, PIVA_IT), (CF, DNI)}, f"Italia e Spagna insieme: {vals}")
    # il modello vede un telefono nel SIREN: la regex validata vince
    text = f"SIREN {SIREN[:3]} {SIREN[3:6]} {SIREN[6:]}"
    model = [{"label": TEL, "start": 6, "end": len(text), "score": 0.9, "validated": False, "source": "modello"}]
    vals = merged(text, model)
    check(vals == {(PIVA, text[6:])}, f"SIREN batte il telefono del modello: {vals}")
    # un telefono vero accanto a un IBAN: solo il telefono è telefono
    text = "Tel. 06 12 34 56 78, IBAN FR76 3000 6000 0112 3456 7890 189"
    vals = merged(text)
    check(vals == {(TEL, "06 12 34 56 78"), ("IBAN", "FR76 3000 6000 0112 3456 7890 189")}, f"telefono + IBAN: {vals}")

    class NoModel(core.PiiEngine):
        def detect_model(self, text, ctl=None):
            return []
    eng = NoModel(str(HERE / "models" / "none"))
    res = eng.analyze(f"Tél. 06 12 34 56 78, BSN {BSN}, Passwort: Sommer2024!")
    check(res["anonymized_text"] == "Tél. [TELEPHONENUM_1], BSN [CF_1], Passwort: [PASSWORD_1]", res["anonymized_text"])
    check(core.decode_text(res["anonymized_text"], res["mapping"])[0]
          == f"Tél. 06 12 34 56 78, BSN {BSN}, Passwort: Sommer2024!", "decodifica")
    res = eng.analyze(f"DNI {DNI}; lo stesso DNI {DNI}")
    phs = [e["ph"] for e in res["entities"]]
    check(phs == ["[CF_1]", "[CF_1]"], f"stesso valore, stesso placeholder: {phs}")

    cfg = HERE / "models"
    model_dirs = [d for d in cfg.glob("*") if (d / "config.json").exists()] if cfg.exists() else []
    if model_dirs:
        tags = core.PiiEngine(str(model_dirs[0])).tags()
        check({TEL, CF, PIVA, URL} <= set(tags["all"]), f"tags(): {tags['all']}")
    else:
        print("  (nessun modello scaricato: tags() non verificato)")

    print(f"\nPASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

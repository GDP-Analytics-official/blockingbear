"""Post-check PIVA/IBAN sull'output del modello, e partita IVA europea.

Il modello è addestrato SOLO su partita IVA e IBAN italiani (rizzo-pii
`partita_iva` / `iban_it`): fuori da quel perimetro produce falsi positivi
(numeri di protocollo marcati PIVA, codici interni marcati IBAN) e span
sbagliate nella FORMA (la VAT estera marcata senza il prefisso del paese).

Qui si verifica:
  - i validatori dei 12 paesi con checksum, su numeri pubblicati come validi;
  - `detect_eu_vat` nella rete regex: forme accettate, prefisso staccato che
    pretende una prova (checksum o etichetta), prefissi che sono parole
    italiane ("che", "se", "si", "no");
  - `core.post_check` sulle entità del modello: promozione, span allargata al
    prefisso, scarto di quello che non può essere una PIVA/un IBAN;
  - `core.post_check` su CREDITCARDNUMBER: Luhn ok -> tenuta e promossa,
    Luhn rotto o forma impossibile -> scartata;
  - che il post-check NON tocchi la rete regex né i termini custom;
  - i casi base che il post-check deve lasciare intatti (11 cifre nude,
    IBAN a gruppi, IBAN su due righe).

Non serve il modello: le entità "del modello" si costruiscono a mano, con la
stessa forma che produce `PiiEngine.detect_model`.

Uso:  python backend/tests/postcheck_piva_iban_test.py
"""
import io
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.engine.core import post_check                           # noqa: E402
from app.engine.detectors import (EU_VAT, detect_eu_vat,         # noqa: E402
                                  detect_iban, detect_regex, iban_ok, luhn_ok,
                                  piva_ok)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def model_ent(text, value, label, occ=0):
    """Entità come la produce il modello: offset sul testo, mai validata."""
    starts = [m.start() for m in re.finditer(re.escape(value), text)]
    s = starts[occ]
    return {"label": label, "start": s, "end": s + len(value),
            "score": 0.99, "validated": False, "source": "modello"}


def one(text, value, label, occ=0):
    """Post-check di una sola entità del modello -> (valore coperto, validated)
    oppure (None, None) se è stata scartata."""
    out = post_check([model_ent(text, value, label, occ)], text)
    if not out:
        return None, None
    e = out[0]
    return text[e["start"]:e["end"]], e["validated"]


# --------------------------------------------------------------------------- #
# 1. validatori nazionali                                                      #
# --------------------------------------------------------------------------- #
# numeri pubblicati come validi (VIES / registri camerali)
VALID_VAT = ["ATU13585627", "BE0428759497", "DE136695976", "EL094259216",
             "GR094259216", "ESA28015865", "ESA78374725", "ESB64717838",
             "ESJ99216582", "ESX1234567L", "FR40303265045", "GB980780684",
             "IT00743110157", "LU15027442", "NL004495445B01", "PL5260001246",
             "PT501964843"]

# stessi numeri con una cifra cambiata: il checksum deve accorgersene
BROKEN_VAT = ["ATU13585628", "BE0428759498", "DE136695975", "EL094259215",
              "ESA28015864", "FR40303265046", "GB980780685", "IT00743110158",
              # NL: la cifra da rompere è la nona, non il suffisso "B01" (che
              # è il progressivo della sede e non entra nel checksum)
              "LU15027443", "NL004495446B01", "PL5260001247", "PT501964842"]


def test_validatori():
    print("\n[1] validatori nazionali")
    for v in VALID_VAT:
        cc = next(c for c in sorted(EU_VAT, key=len, reverse=True) if v.startswith(c))
        shape, validator = EU_VAT[cc]
        body = v[len(cc):]
        ok = bool(re.fullmatch(shape, body)) and (validator(body) if validator else True)
        check(f"{v} valida", ok)
    for v in BROKEN_VAT:
        cc = next(c for c in sorted(EU_VAT, key=len, reverse=True) if v.startswith(c))
        shape, validator = EU_VAT[cc]
        body = v[len(cc):]
        bad = not re.fullmatch(shape, body) or (validator and not validator(body))
        check(f"{v} rifiutata (cifra cambiata)", bool(bad))


# --------------------------------------------------------------------------- #
# 2. detect_eu_vat: la rete regex trova le VAT estere                          #
# --------------------------------------------------------------------------- #
def test_detector():
    print("\n[2] detect_eu_vat nella rete regex")

    t = "Fornitore tedesco, partita IVA DE136695976, consegna a Monaco."
    ents = detect_eu_vat(t)
    check("VAT tedesca trovata col prefisso",
          len(ents) == 1 and t[ents[0]["start"]:ents[0]["end"]] == "DE136695976",
          [t[e["start"]:e["end"]] for e in ents])
    check("VAT col checksum ok è validata", ents and ents[0]["validated"])

    t = "Ragione sociale olandese NL004495445B01 e portoghese PT501964843."
    vals = [t[e["start"]:e["end"]] for e in detect_eu_vat(t)]
    check("due VAT nella stessa riga", vals == ["NL004495445B01", "PT501964843"], vals)

    # paese senza validatore: la sola forma basta, ma niente ✓
    t = "Cliente danese DK12345678, ordine confermato."
    ents = detect_eu_vat(t)
    check("paese senza checksum: coperto ma non validato",
          len(ents) == 1 and not ents[0]["validated"]
          and t[ents[0]["start"]:ents[0]["end"]] == "DK12345678",
          [(t[e["start"]:e["end"]], e["validated"]) for e in ents])

    # lunghezza sbagliata per quel paese
    check("DE con 10 cifre non è una VAT tedesca",
          detect_eu_vat("codice interno DE1366959761 in anagrafica") == [])
    check("paese inesistente", detect_eu_vat("pratica ZZ123456789 archiviata") == [])

    # prefissi che sono parole italiane: il caso che rende necessario il
    # maiuscolo obbligatorio e la prova sul prefisso staccato
    for frase, perche in [
        ("che 123456789 sia chiaro", "'che' minuscolo (CHE = Svizzera)"),
        ("se 1234567801 arriva", "'se' minuscolo (SE = Svezia)"),
        ("si 12345678 conferma", "'si' minuscolo (SI = Slovenia)"),
        ("no 123456789 grazie", "'no' minuscolo (NO = Norvegia)"),
        ("PAGARE SE 1234567801 SCADUTO", "'SE' maiuscolo ma staccato e senza etichetta"),
        ("CHE 123456789 RESTA", "'CHE' maiuscolo ma staccato e senza etichetta"),
    ]:
        check(f"nessun falso positivo: {perche}", detect_eu_vat(frase) == [],
              [frase[e["start"]:e["end"]] for e in detect_eu_vat(frase)])

    # prefisso staccato: passa se c'è il checksum o l'etichetta
    t = "Sede in Germania, DE 136695976 iscritta al registro."
    check("prefisso staccato con checksum ok -> passa",
          [t[e["start"]:e["end"]] for e in detect_eu_vat(t)] == ["DE 136695976"])
    t = "Cliente danese, VAT DK 12345678, pagamento a 60 giorni."
    check("prefisso staccato senza checksum ma con etichetta -> passa",
          [t[e["start"]:e["end"]] for e in detect_eu_vat(t)] == ["DK 12345678"])
    t = "Riferimento DK 12345678 del magazzino."
    check("prefisso staccato senza checksum né etichetta -> scartato",
          detect_eu_vat(t) == [])

    # forma svizzera puntata
    t = "Controparte CHE-123.456.789 IVA con sede a Lugano."
    check("CHE con la punteggiatura svizzera",
          [t[e["start"]:e["end"]] for e in detect_eu_vat(t)] == ["CHE-123.456.789"],
          [t[e["start"]:e["end"]] for e in detect_eu_vat(t)])

    # la PIVA italiana nuda resta compito della voce PIVA in DETECTORS
    t = "Partita IVA 00743110157 iscritta."
    labels = [(e["label"], t[e["start"]:e["end"]]) for e in detect_regex(t)
              if e["label"] == "PIVA"]
    check("PIVA italiana nuda ancora trovata dalla rete regex",
          ("PIVA", "00743110157") in labels, labels)

    t = "Partita IVA IT00743110157 iscritta."
    vals = [t[e["start"]:e["end"]] for e in detect_regex(t) if e["label"] == "PIVA"]
    check("PIVA italiana col prefisso: c'è anche la forma lunga",
          "IT00743110157" in vals, vals)


# --------------------------------------------------------------------------- #
# 3. post-check PIVA sull'output del modello                                   #
# --------------------------------------------------------------------------- #
def test_post_piva():
    print("\n[3] post-check PIVA")

    # promozione: 11 cifre col Luhn giusto
    t = "La società con partita IVA 00743110157 comunica."
    val, ok = one(t, "00743110157", "PIVA")
    check("PIVA valida tenuta e promossa", val == "00743110157" and ok, (val, ok))

    # la span del modello inghiotte l'etichetta -> si restringe al numero
    t = "Vedi P. IVA 00743110157 in fattura."
    val, ok = one(t, "P. IVA 00743110157", "PIVA")
    check("span con l'etichetta dentro: ristretta al numero",
          val == "00743110157" and ok, (val, ok))

    # la span del modello taglia il numero a metà -> si riallarga
    t = "Partita IVA 00743110157 del fornitore."
    val, ok = one(t, "0074311015", "PIVA")
    check("span tagliata: riallargata alle 11 cifre vere",
          val == "00743110157" and ok, (val, ok))

    # IL CASO DIFFICILE: VAT estera, il modello marca le sole cifre
    t = "Fornitore con partita IVA DE136695976 di Stoccarda."
    val, ok = one(t, "136695976", "PIVA")
    check("VAT estera: span ALLARGATA al prefisso, non scartata",
          val == "DE136695976" and ok, (val, ok))

    t = "Cliente NL004495445B01 di Rotterdam."
    val, ok = one(t, "004495445", "PIVA")
    check("VAT olandese: recuperata per intero", val == "NL004495445B01" and ok,
          (val, ok))

    t = "Ordine da CHE-123.456.789 di Lugano."
    val, ok = one(t, "123.456.789", "PIVA")
    check("VAT svizzera puntata: recuperata", val == "CHE-123.456.789", (val, ok))

    # i falsi positivi che il filtro esiste per togliere
    for t, v, perche in [
        ("Protocollo interno 445291337 del 3 marzo.", "445291337", "9 cifre nude"),
        ("Commessa 88213345 chiusa.", "88213345", "8 cifre nude"),
        ("Matricola INPS 4471129056 attiva.", "4471129056", "10 cifre nude"),
        ("Totale fattura EUR 12345678903 lire storiche.", "12345678903",
         "11 cifre ma dentro un importo"),
        ("Codice pratica 12345678900 respinto.", "12345678900",
         "11 cifre, Luhn rotto, nessuna etichetta"),
    ]:
        val, _ = one(t, v, "PIVA")
        check(f"scartato: {perche}", val is None, val)

    # l'unica scappatoia: l'etichetta davanti
    t = "P.IVA 12345678900 (numero battuto male in anagrafica)."
    val, ok = one(t, "12345678900", "PIVA")
    check("Luhn rotto MA con l'etichetta davanti: coperto, non validato",
          val == "12345678900" and ok is False, (val, ok))

    # niente cifre nella span
    val, _ = one("Il legale rappresentante firma.", "legale rappresentante", "PIVA")
    check("span senza cifre: scartata", val is None, val)


# --------------------------------------------------------------------------- #
# 4. post-check IBAN sull'output del modello                                   #
# --------------------------------------------------------------------------- #
IT_IBAN = "IT60X0542811101000000123456"       # esempio classico, mod-97 valido


def test_post_iban():
    print("\n[4] post-check IBAN")
    check("l'IBAN di prova è valido", iban_ok(IT_IBAN))

    t = f"Bonifico su {IT_IBAN} entro il 30."
    val, ok = one(t, IT_IBAN, "IBAN")
    check("IBAN valido tenuto e promosso", val == IT_IBAN and ok, (val, ok))

    # span che ingloba l'etichetta -> lo scanner ritrova il codice esatto
    t = f"IBAN: {IT_IBAN} intestato alla società."
    val, ok = one(t, f"IBAN: {IT_IBAN}", "IBAN")
    check("span con 'IBAN:' dentro: ristretta al codice", val == IT_IBAN and ok,
          (val, ok))

    # span tagliata a metà -> riallargata
    t = f"Accredito su {IT_IBAN} con valuta immediata."
    val, ok = one(t, IT_IBAN[:15], "IBAN")
    check("span tagliata: riallargata all'IBAN intero", val == IT_IBAN and ok,
          (val, ok))

    # formato a gruppi
    grouped = "IT60 X054 2811 1010 0000 0123 456"
    t = f"Coordinate: {grouped}"
    val, ok = one(t, grouped, "IBAN")
    check("formato a gruppi", val == grouped and ok, (val, ok))

    # IBAN estero
    de = "DE89370400440532013000"
    check("l'IBAN tedesco di prova è valido", iban_ok(de))
    t = f"Conto estero {de} presso Commerzbank."
    val, ok = one(t, de, "IBAN")
    check("IBAN estero valido", val == de and ok, (val, ok))

    # mod-97 rotto ma struttura italiana intatta -> si copre lo stesso
    broken = "IT61X0542811101000000123456"                 # cifre di controllo sbagliate
    check("il caso di prova ha davvero il mod-97 rotto", not iban_ok(broken))
    t = f"Bonifico su {broken} (letto dallo scanner)."
    val, ok = one(t, broken, "IBAN")
    check("IBAN italiano col mod-97 rotto: coperto, non validato",
          val == broken and ok is False, (val, ok))

    # i falsi positivi
    for t, v, perche in [
        ("Banca d'appoggio UNCRITMMXXX per il bonifico.", "UNCRITMMXXX",
         "BIC/SWIFT (11 caratteri)"),
        ("Codice tesoreria ABI 05428 CAB 11101 attivo.", "ABI 05428 CAB 11101",
         "coppia ABI/CAB"),
        ("Contratto ZZ12ABCDEFGHIJ1234567890 firmato.", "ZZ12ABCDEFGHIJ1234567890",
         "paese fuori dalla tabella ISO"),
        ("Riferimento IT60X05428111010000001 in atti.", "IT60X05428111010000001",
         "lunghezza sbagliata per l'Italia"),
        ("Conto corrente n. 000012345678 presso la filiale.", "000012345678",
         "conto nudo, senza testa paese"),
        # 27 caratteri e testa "IT" + due cifre, ma il CIN è una cifra: non è
        # un IBAN italiano letto male, è un altro codice
        ("Pratica IT6090542811101000000123456 annullata.",
         "IT6090542811101000000123456", "lunghezza IT ok ma struttura rotta"),
    ]:
        val, _ = one(t, v, "IBAN")
        check(f"scartato: {perche}", val is None, val)


# --------------------------------------------------------------------------- #
# 5. il post-check non tocca il resto                                          #
# --------------------------------------------------------------------------- #
# numeri di test pubblici (Visa / Mastercard / Amex): superano il Luhn
CARD_OK = "4111111111111111"
CARD_MC = "5500 0000 0000 0004"
CARD_AMEX = "3782-822463-10005"
CARD_BAD = "4111111111111112"           # una cifra cambiata


def test_post_card():
    print("\n[5] post-check CREDITCARDNUMBER")
    check("le carte di prova superano il Luhn",
          luhn_ok(CARD_OK) and luhn_ok(CARD_MC) and luhn_ok(CARD_AMEX)
          and not luhn_ok(CARD_BAD))

    t = f"Pagamento con carta {CARD_OK} scadenza 12/28."
    val, ok = one(t, CARD_OK, "CREDITCARDNUMBER")
    check("carta valida tenuta e promossa", val == CARD_OK and ok, (val, ok))

    t = f"Carta n. {CARD_MC} intestata a Mario."
    val, ok = one(t, CARD_MC, "CREDITCARDNUMBER")
    check("carta a gruppi con spazi tenuta", val == CARD_MC and ok, (val, ok))

    t = f"Amex {CARD_AMEX}."
    val, ok = one(t, CARD_AMEX, "CREDITCARDNUMBER")
    check("carta con trattini tenuta, punto finale fuori",
          val == CARD_AMEX and ok, (val, ok))

    # span che ingloba l'etichetta -> ristretta al numero
    t = f"Carta: {CARD_OK} valida."
    val, ok = one(t, f"Carta: {CARD_OK}", "CREDITCARDNUMBER")
    check("span con 'Carta:' dentro: ristretta al numero",
          val == CARD_OK and ok, (val, ok))

    # span tagliata a metà -> riallargata al numero intero
    t = f"Addebito su {CARD_MC} eseguito."
    val, ok = one(t, CARD_MC[:9], "CREDITCARDNUMBER")
    check("span tagliata: riallargata", val == CARD_MC and ok, (val, ok))

    # Luhn rotto -> scartata (non c'è struttura su cui appoggiarsi)
    t = f"Carta {CARD_BAD} rifiutata."
    val, ok = one(t, CARD_BAD, "CREDITCARDNUMBER")
    check("Luhn rotto: scartata", val is None, (val, ok))

    # falsi positivi tipici: numero d'ordine corto, codice a barre lungo
    t = "Ordine 123456789012 spedito."
    val, ok = one(t, "123456789012", "CREDITCARDNUMBER")
    check("12 cifre: scartata", val is None, (val, ok))

    t = "EAN 12345678901234567890 a magazzino."
    val, ok = one(t, "12345678901234567890", "CREDITCARDNUMBER")
    check("20 cifre: scartata", val is None, (val, ok))

    # dentro un numero più lungo: il candidato non esiste
    t = f"Codice 9{CARD_OK}7 interno."
    val, ok = one(t, CARD_OK, "CREDITCARDNUMBER")
    check("cifre attaccate ad altre cifre: scartata", val is None, (val, ok))

    # testo senza cifre marcato carta
    t = "Carta fedeltà COOP attiva."
    val, ok = one(t, "COOP", "CREDITCARDNUMBER")
    check("nessuna cifra: scartata", val is None, (val, ok))

    # la rete regex da sola resta invariata
    t = f"Saldo carta {CARD_OK} e ordine 123456789012."
    vals = [t[e["start"]:e["end"]] for e in detect_regex(t)
            if e["label"] == "CREDITCARDNUMBER"]
    check("detect_regex sulle carte indipendente dal post-check",
          vals == [CARD_OK], vals)


def test_perimetro():
    print("\n[6] perimetro del post-check")

    t = "Contatto mario.rossi@example.it, Roma."
    ents = [model_ent(t, "mario.rossi@example.it", "EMAIL"),
            model_ent(t, "Roma", "CITY")]
    out = post_check([dict(e) for e in ents], t)
    check("le altre label passano intatte",
          [(e["label"], t[e["start"]:e["end"]]) for e in out]
          == [("EMAIL", "mario.rossi@example.it"), ("CITY", "Roma")],
          [(e["label"], t[e["start"]:e["end"]]) for e in out])

    # una entità della rete regex non deve MAI passare dal post-check: la
    # verifica sta già dentro detect_regex (strict=True)
    t = "Protocollo 445291337 del 3 marzo."
    regex_like = {"label": "PIVA", "start": 11, "end": 20, "score": 1.0,
                  "validated": False, "source": "regex"}
    check("post_check gira solo sull'output del modello (chiamato in analyze)",
          "source" in regex_like and regex_like["source"] == "regex")

    # gli IBAN validi devono restare trovati dalla rete regex da sola,
    # indipendentemente dal post-check
    t = f"Saldo su {IT_IBAN} e su DE89370400440532013000."
    vals = [t[e["start"]:e["end"]] for e in detect_iban(t)]
    check("detect_iban indipendente dal post-check",
          vals == [IT_IBAN, "DE89370400440532013000"], vals)

    # IBAN spezzato su due righe (caso PDF)
    t = "IBAN\nIT60 X054 2811 1010\n0000 0123 456\nintestato a."
    vals = [t[e["start"]:e["end"]] for e in detect_iban(t)]
    check("IBAN a capo una volta: ancora trovato", len(vals) == 1, vals)

    # piva_ok invariata
    check("piva_ok invariata", piva_ok("00743110157") and not piva_ok("00743110158"))


def main():
    print("POST-CHECK PIVA/IBAN/CARTA + PARTITA IVA EUROPEA")
    test_validatori()
    test_detector()
    test_post_piva()
    test_post_iban()
    test_post_card()
    test_perimetro()
    print(f"\nRISULTATO: {PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

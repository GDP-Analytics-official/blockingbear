"""
Analisi PII: modello mmBERT (rizzo-pii-0.3B) + rete regex/checksum, fusione e
placeholder reversibili. Logica ripresa da rizzo-pii `src/app/app.py`
(MIT, (c) 2026 Simone Rizzo — Rizzo AI Academy; avviso di licenza nel NOTICE), con
un solo adattamento: classe `PiiEngine` con caricamento LAZY del modello
(l'API parte subito, il primo /process paga il caricamento una sola volta).

I placeholder sono quelli ORIGINALI di rizzo-pii: `[FULLNAME_1]`, `[IBAN_1]`,
... — stesso (tag, valore normalizzato) -> stesso placeholder, quindi la mappa
è un dizionario {placeholder: valore originale} decodificabile dopo.
"""

import bisect
import re
import threading
import unicodedata

from .text_patterns import WORD, canonical, literal, PLACEHOLDER_RE

from .detectors import (DETECTORS, DEVICE_LABELS, EXACT_SPAN_LABELS, SOFT_REGEX_LABELS, TAG_GROUPS,
                        detect_eu_vat, detect_regex,
                        has_vat_cue, it_iban_ok, piva_ok, scan_card, scan_iban)
from .progress import NULL as _NULL_CTL

MAX_WORDS = 120      # parole per chunk (~180 subword, sotto i 512 del training)
OVERLAP = 20         # parole di sovrapposizione tra chunk consecutivi
PIVA_DIGITS = 11     # partita IVA italiana: 11 cifre attaccate, mai una in meno
BATCH_CHUNKS = 8     # chunk per chiamata alla pipeline: granularità di
                     # progresso/cancellazione (~6s a batch su CPU d'ufficio);
                     # spezzare non costa throughput (torch satura già i core)


def chunk_text(text, max_words=MAX_WORDS, overlap=OVERLAP):
    """Ritorna [(sottostringa, offset_char_globale), ...] senza tagliare parole."""
    words = list(re.finditer(r"\S+", text))
    if not words:
        return []
    chunks, i = [], 0
    step = max(1, max_words - overlap)
    while i < len(words):
        block = words[i:i + max_words]
        start, end = block[0].start(), block[-1].end()
        chunks.append((text[start:end], start))      # slice esatto -> offset diretti
        if i + max_words >= len(words):
            break
        i += step
    return chunks


def _is_word(ch):
    """Carattere interno a una parola (lettere accentate e cifre incluse)."""
    return ch.isalnum() or ch == "_" or bool(unicodedata.combining(ch))


def _merge(cands, text):
    """Greedy senza overlap. Priorità: checksum-valido > regex (non soft) > score
    > lunghezza. (Identica a rizzo-pii, commenti inclusi.)"""
    order = sorted(
        cands,
        key=lambda e: (1 if e["validated"] else 0,
                       1 if (e["source"] == "regex"
                             and e["label"] not in SOFT_REGEX_LABELS) else 0,
                       e["score"], e["end"] - e["start"]),
        reverse=True,
    )
    kept = []
    for e in order:
        # kept resta ordinata per start e senza sovrapposizioni: un candidato può
        # accavallarsi solo con i due vicini, che la ricerca binaria trova subito.
        i = bisect.bisect_right(kept, e["start"], key=lambda k: k["start"])
        if (i and kept[i - 1]["end"] > e["start"]) or \
           (i < len(kept) and kept[i]["start"] < e["end"]):
            continue
        kept.insert(i, e)
    # niente spazi inglobati nei placeholder
    for e in kept:
        if e["label"] in EXACT_SPAN_LABELS:
            continue
        while e["start"] < e["end"] and text[e["start"]].isspace():
            e["start"] += 1
        while e["end"] > e["start"] and text[e["end"] - 1].isspace():
            e["end"] -= 1
    kept = [e for e in kept if e["end"] > e["start"]]

    # Allineamento ai confini di parola: se una span taglia una parola a metà,
    # la si estende fino a coprirla (meglio mascherare un carattere in più).
    # Le label a span esatta no: "-pSegreto" è la password "Segreto" dietro il
    # flag -p, e "10.0.0.1:5432" è l'IP seguito dalla porta.
    for e in kept:
        if e["label"] in EXACT_SPAN_LABELS:
            continue
        while (e["start"] > 0
               and _is_word(text[e["start"] - 1]) and _is_word(text[e["start"]])):
            e["start"] -= 1
        while (e["end"] < len(text)
               and _is_word(text[e["end"]]) and _is_word(text[e["end"] - 1])):
            e["end"] += 1

    # L'estensione può rendere due span sovrapposte o adiacenti: si fondono.
    kept.sort(key=lambda e: (e["start"], -(e["end"] - e["start"])))
    merged = []
    for e in kept:
        if merged and e["start"] < merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], e["end"])
            continue
        if merged and e["start"] == merged[-1]["end"] and e["label"] == merged[-1]["label"]:
            merged[-1]["end"] = e["end"]
            continue
        merged.append(e)

    # Punteggiatura ai bordi: il modello a volte la ingloba nella span
    # ("Rossi & Figli S.r.l.)", "+39 02 ..."), ma i redattori cercano il valore
    # SENZA le ancore di punteggiatura (_value_pattern lavora sul "core"). Il
    # valore memorizzato divergerebbe dal testo davvero sostituito e in
    # decodifica la punteggiatura ricomparirebbe due volte: "S.r.l.))", "++39".
    # Si stringe la span sui caratteri di parola: ciò che resta fuori non è
    # PII e nel documento è già al posto giusto. DOPO la fusione, altrimenti
    # due span adiacenti separate da un apostrofo ("Giovanni D'" + "Amico")
    # smetterebbero di essere adiacenti e resterebbero due entità.
    # Le label a span esatta no: in "Estate2024!" il simbolo finale è parte
    # della password e va mascherato con lei, e un hash può finire con "=".
    for e in merged:
        if e["label"] in EXACT_SPAN_LABELS:
            continue
        while e["start"] < e["end"] and not _is_word(text[e["start"]]):
            e["start"] += 1
        while e["end"] > e["start"] and not _is_word(text[e["end"] - 1]):
            e["end"] -= 1
    return [e for e in merged if e["end"] > e["start"]]


def _norm(s):
    return canonical(s)


def _term_pattern(term):
    """Regex di un termine scelto dall'utente: caratteri esatti, case
    insensitive, whitespace flessibile tra i token e confini di parola agli
    estremi (niente match dentro altre parole)."""
    toks = [t for t in re.split(r"\s+", (term or "").strip()) if t]
    if not toks:
        return None
    body = r"\s+".join(literal(t) for t in toks)
    return re.compile(rf"(?<![{WORD}])" + body + rf"(?![{WORD}])", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Post-check sull'output del MODELLO                                           #
# --------------------------------------------------------------------------- #
# I formati con checksum sono l'unico posto dove si può dare torto al modello
# con una prova invece che con un'euristica, ed è lì che i suoi falsi
# positivi si vedono: un protocollo di nove cifre diventa una partita IVA, un
# codice interno diventa un IBAN. Questi filtri girano SOLO sulle entità del
# modello - la rete regex pretende già il checksum (`strict=True`) e i termini
# custom sono scelte esplicite dell'utente, entrambi non vanno rimessi in
# discussione.
#
# Tre esiti, non due:
#   - forma giusta + checksum ok -> si tiene e si PROMUOVE (validated=True: in
#     `_merge` la promozione vale quanto un match della rete regex, e in
#     interfaccia mette il ✓);
#   - forma giusta, checksum assente o fallito ma la struttura è intatta -> si
#     tiene NON validata: un IBAN storpiato dall'OCR è comunque un conto
#     corrente, e scartarlo sarebbe l'unico errore davvero costoso qui;
#   - forma strutturalmente impossibile -> si scarta.
# Il terzo caso decide sulla FORMA, mai sul solo checksum.

# Il numero sta dentro un importo o dentro un numero più lungo. Le cifre da
# sole non lo direbbero: "12345678903" supera il Luhn tanto come partita IVA
# quanto come coda di un totale.
_AMOUNT_LEFT = re.compile(r"(?:\d[.,]|€|EUR|CAP|C/C|IBAN)\s*$", re.IGNORECASE)
_AMOUNT_RIGHT = re.compile(r"[.,]\d")


def _in_amount(text, s, t):
    return bool(_AMOUNT_LEFT.search(text[max(0, s - 12):s])
                or _AMOUNT_RIGHT.match(text[t:t + 2]))


def _overlaps(e, s, t):
    return s < e["end"] and e["start"] < t


def _clean_piva(e, text, found):
    """Partita IVA, italiana o europea.

    Prima si guarda se la span si sovrappone a una VAT col prefisso del paese
    trovata dallo STESSO detector della rete regex: il modello è addestrato
    solo sull'Italia (11 cifre nude) e su "DE811569869" tipicamente marca le
    sole cifre, quindi la span va ALLARGATA a sinistra per riprendersi il
    prefisso: restringerla al posto di allargarla cancellerebbe ogni partita
    IVA estera.

    Senza prefisso resta la sola forma italiana: 11 cifre attaccate. Diverso da
    11 si scarta sempre (lì l'etichetta "P.IVA" direbbe di cosa si sta
    parlando, non che QUEL numero è una partita IVA); a 11 cifre col Luhn
    rotto si scarta pure, a meno che l'etichetta non sia proprio davanti - un
    numero battuto male sotto la scritta "P.IVA" resta un dato da coprire.
    """
    for c in found:
        if _overlaps(e, c["start"], c["end"]):
            e["start"], e["end"] = c["start"], c["end"]
            e["validated"] = c["validated"]
            return e

    runs = list(re.finditer(r"\d+", text[e["start"]:e["end"]]))
    if not runs:
        return None
    # a parità di lunghezza vince la prima: se la span ne contiene due è
    # malformata comunque
    best = max(runs, key=lambda m: m.end() - m.start())
    s, t = e["start"] + best.start(), e["start"] + best.end()
    # la span del modello può tagliare il numero a metà: la lunghezza vera si
    # misura sul testo, non su quello che il modello ha deciso di prendere
    while s and text[s - 1].isdigit():
        s -= 1
    while t < len(text) and text[t].isdigit():
        t += 1
    if t - s != PIVA_DIGITS or _in_amount(text, s, t):
        return None
    ok = piva_ok(text[s:t])
    if not ok and not has_vat_cue(text, s):
        return None
    e["start"], e["end"], e["validated"] = s, t, ok
    return e


def _clean_iban(e, text, found):
    """IBAN.

    `found` viene da `scan_iban`, lo stesso scanner della rete regex, ma qui si
    guardano anche i candidati col mod-97 sbagliato: la rete regex li scarta
    (girando su tutto il testo sarebbero falsi positivi), mentre una span che il
    MODELLO ha già indicato come IBAN, con la testa giusta e la lunghezza
    esatta del paese, è quasi sempre un IBAN vero letto male dall'OCR.

    Quello che si scarta è ciò che non può essere un IBAN: nessuna testa
    "paese + due cifre", paese fuori dalla tabella ISO, lunghezza diversa da
    quella prevista - tutte condizioni già dentro lo scanner, che infatti in
    quei casi non produce alcun candidato. Così cadono i codici interni che
    iniziano per due lettere e i BIC (8 o 11 caratteri: nessun paese ha un IBAN
    così corto, il più corto è NO a 15).
    """
    for s, t, ok in found:
        if not _overlaps(e, s, t):
            continue
        # per l'Italia la lunghezza non basta: 27 caratteri li ha anche un
        # codice qualunque. Col mod-97 rotto si pretende almeno la struttura
        # (CIN alfabetico, ABI e CAB numerici).
        if not ok and text[s:s + 2].upper() == "IT" \
                and not it_iban_ok(re.sub(r"[\s.\-]", "", text[s:t]).upper()):
            continue
        e["start"], e["end"], e["validated"] = s, t, ok
        return e
    return None


def _clean_card(e, text, found):
    """Numero di carta di credito.

    `found` viene da `scan_card`, lo stesso pattern della rete regex: la span
    del modello va bene solo se si sovrappone a una sequenza di 13-19 cifre
    (con al più un separatore tra due cifre) che supera il Luhn; la span viene
    allineata al candidato, così il placeholder copre il numero intero anche
    quando il modello ne ha preso un pezzo o si è portato dietro l'etichetta.

    A differenza dell'IBAN qui il checksum fallito SCARTA: una carta non ha
    una struttura (paese, lunghezza fissa, CIN) a cui appoggiarsi quando il
    Luhn è rotto, e la sola forma "tante cifre" la condivide con numeri
    d'ordine, protocolli, matricole e codici a barre - i falsi positivi tipici
    del modello su questo tag. Con un candidato valido su dieci sequenze
    casuali, il Luhn è la prova che manca.
    """
    for s, t, ok in found:
        if not _overlaps(e, s, t):
            continue
        if not ok:
            return None
        e["start"], e["end"], e["validated"] = s, t, True
        return e
    return None


def _scan_cf(text):
    """Structural candidates for model CF spans, including supported countries.

    The model supplies contextual evidence. A damaged checksum does not remove
    a structurally plausible identifier, but a short table code is rejected.
    Regex detection and explicit custom terms keep their own policies.
    """
    from .national_ids import RULES, _norm as id_norm
    from .text_patterns import normalized_detector

    @normalized_detector
    def scan(view):
        out = []
        for label, rx, validator, _strict in DETECTORS:
            if label == "CF":
                for m in rx.finditer(view):
                    out.append({"start": m.start(), "end": m.end(),
                                "validated": bool(validator(m.group()))})
        for label, _country, rx, validator, _cue, _needed in RULES:
            if label == "CF":
                for m in rx.finditer(view):
                    out.append({"start": m.start("v"), "end": m.end("v"),
                                "validated": bool(validator and
                                    validator(id_norm(m.group("v"))))})
        return out
    return sorted(scan(text), key=lambda e: (
        -int(e["validated"]), -(e["end"] - e["start"])))


def _clean_cf(e, text, found):
    for candidate in found:
        if _overlaps(e, candidate["start"], candidate["end"]):
            return {**e, **candidate}
    return None


POST_CHECKS = {"CF": _clean_cf, "PIVA": _clean_piva, "IBAN": _clean_iban,
               "CREDITCARDNUMBER": _clean_card}
_SCANNERS = {"CF": _scan_cf, "PIVA": detect_eu_vat, "IBAN": scan_iban,
             "CREDITCARDNUMBER": scan_card}


def post_check(ents, text):
    """Applica i post-check per label; l'entità torna corretta, oppure None e
    allora si scarta.

    I candidati si cercano UNA VOLTA su tutto il testo e non in una finestra
    attorno a ogni span: una finestra andrebbe dimensionata sul caso peggiore
    (un IBAN è lungo fino a 34 caratteri e il modello può tagliarlo ovunque),
    e soprattutto taglierebbe il contesto che serve a decidere - dentro la
    finestra il confine sinistro sembra un inizio di parola anche quando nel
    testo vero è in mezzo a un codice."""
    found = {}
    out = []
    for e in ents:
        fn = POST_CHECKS.get(e["label"])
        if fn is None:
            out.append(e)
            continue
        if e["label"] not in found:                    # pigro: molti testi non ne hanno
            found[e["label"]] = _SCANNERS[e["label"]](text)
        e = fn(e, text, found[e["label"]])
        if e is not None:
            out.append(e)
    return out


def detect_custom(text, terms):
    """Occorrenze dei termini configurati a mano (voci {"text", "tag"}, vedi
    settings_store.clean_terms): candidati con la stessa priorità dei match
    validati (chi li ha scritti SA che sono da coprire), etichettati col tag
    scelto — un tag già esistente ne prosegue semplicemente la numerazione."""
    ents = []
    for t in terms or ():
        pat = _term_pattern(t.get("text"))
        if pat is None:
            continue
        label = t.get("tag") or "CUSTOM"
        for m in pat.finditer(text):
            ents.append({"label": label, "start": m.start(), "end": m.end(),
                         "score": 1.0, "validated": True, "source": "custom"})
    return ents


class PiiEngine:
    """Modello mmBERT + rete regex, caricamento lazy e thread-safe."""

    def __init__(self, model_dir):
        self.model_dir = str(model_dir)
        self._nlp = None
        # Sul percorso CUDA ModernBERT entra in torch.compile/Triton solo al
        # primo forward: costruire la pipeline non prova che sia eseguibile.
        self._ready = False
        self._lock = threading.Lock()
        self._infer_lock = threading.Lock()

    @property
    def loaded(self):
        return self._ready

    def tags(self):
        """Tag PII rilevabili, derivati DINAMICAMENTE: quelli del modello dal suo
        config.json (id2label, senza i prefissi BIO B-/I-) + quelli della rete
        regex. Nessuna lista mantenuta a mano: un aggiornamento del modello si
        riflette qui da solo."""
        import json
        from pathlib import Path
        cfg = json.loads((Path(self.model_dir) / "config.json").read_text(encoding="utf-8"))
        model_tags = {v.split("-", 1)[-1] for v in cfg.get("id2label", {}).values()} - {"O"}
        regex_tags = ({label for label, *_ in DETECTORS} | {"IBAN", "TELEPHONENUM"}   # detect_iban/detect_phones a parte
                      | EXACT_SPAN_LABELS | DEVICE_LABELS)            # credentials.py + cyber.py + devices.py
        return {
            "model": sorted(model_tags),
            "regex_only": sorted(regex_tags - model_tags),
            "all": sorted(model_tags | regex_tags),
            # gruppi della UI: {nome: [tag, ...]} (vedi detectors.TAG_GROUPS)
            "groups": {k: list(v) for k, v in TAG_GROUPS.items()},
        }

    def load(self):
        with self._lock:
            if self._nlp is None:
                import torch
                from transformers import pipeline
                device = 0 if torch.cuda.is_available() else -1
                self._nlp = pipeline(
                    "token-classification",
                    model=self.model_dir,
                    tokenizer=self.model_dir,
                    aggregation_strategy="simple",
                    device=device,
                )
        return self._nlp

    def detect_model(self, text, ctl=None):
        """Entità trovate dal modello mmBERT su tutti i chunk, su offset globali.

        I chunk passano alla pipeline a BATCH (BATCH_CHUNKS): tra un batch e
        l'altro `ctl` riporta l'avanzamento reale (chunk fatti / totali) e
        controlla la richiesta di annullamento (vedi progress.py)."""
        ctl = ctl or _NULL_CTL
        nlp = self.load()
        chunks = chunk_text(text)
        ents = []
        with self._infer_lock:
            for i in range(0, len(chunks), BATCH_CHUNKS):
                ctl.check()
                batch = chunks[i:i + BATCH_CHUNKS]
                results = nlp([c for c, _ in batch])
                if isinstance(results, dict):             # singolo chunk -> normalizza
                    results = [results]
                for (_, off), res in zip(batch, results):
                    for e in res:
                        ents.append({
                            "label": e["entity_group"],
                            "start": int(e["start"]) + off,
                            "end": int(e["end"]) + off,
                            "score": float(e["score"]),
                            "validated": False,
                            "source": "modello",
                        })
                ctl.tick(min(i + BATCH_CHUNKS, len(chunks)), len(chunks))
            if chunks:
                # Tutti i batch sono arrivati in fondo, inclusa l'eventuale
                # compilazione Triton del primo forward CUDA.
                self._ready = True
        return ents

    def warmup(self):
        """Carica il modello ed esegue un forward reale e sintetico.

        `load()` da solo non basta a provare il percorso CUDA: torch.compile e
        Triton scattano al primo forward. `loaded` diventa vero soltanto se
        questa chiamata arriva in fondo.
        """
        return self.detect_model(
            "Il referente Mario Rossi risiede a Roma e usa il codice pratica 12345."
        )

    def analyze(self, text, excluded=None, custom_terms=None, ctl=None):
        """Testo -> entità fuse + placeholder reversibili ([FULLNAME_1], ...).

        Ritorna {entities, anonymized_text, mapping, by_label, ...}; in `entities`
        ogni voce ha offset sul testo ORIGINALE e il placeholder assegnato.
        `excluded` = tag da lasciare in chiaro. `custom_terms` = termini scelti
        a mano da coprire comunque ([{"text", "tag"}], vedi detect_custom).
        `ctl` = progresso/annullamento (vedi progress.py), opzionale.
        """
        excluded = set(excluded or ())
        cands = post_check(self.detect_model(text, ctl=ctl), text) \
            + detect_regex(text)
        if excluded:
            cands = [e for e in cands if e["label"] not in excluded]
        # dopo il filtro: i termini scelti a mano sono espliciti, `excluded`
        # non li tocca nemmeno se il tag coincide con una categoria esclusa
        cands += detect_custom(text, custom_terms)
        kept = _merge(cands, text)

        # ID reversibili: stesso (label, valore-normalizzato) -> stesso placeholder.
        counters, seen, mapping = {}, {}, {}
        for e in kept:
            val = text[e["start"]:e["end"]]
            # una password o un MAC sono case-sensitive: "Abc" e "abc" sono due valori
            key = (e["label"], val if e["label"] in EXACT_SPAN_LABELS else _norm(val))
            if key in seen:
                e["ph"] = seen[key]
            else:
                counters[e["label"]] = counters.get(e["label"], 0) + 1
                ph = f"[{e['label']}_{counters[e['label']]}]"
                seen[key] = ph
                mapping[ph] = val
                e["ph"] = ph

        anon, by_label, pos = [], {}, 0
        for e in kept:
            anon.append(text[pos:e["start"]])
            anon.append(e["ph"])
            by_label[e["label"]] = by_label.get(e["label"], 0) + 1
            pos = e["end"]
        anon.append(text[pos:])

        return {
            "entities": [{"label": e["label"], "ph": e["ph"],
                          "start": e["start"], "end": e["end"],
                          "value": text[e["start"]:e["end"]],
                          "source": e["source"], "validated": e["validated"]}
                         for e in kept],
            "anonymized_text": "".join(anon),
            "mapping": mapping,
            "n_entities": len(kept),
            "n_unique": len(seen),
            "by_label": dict(sorted(by_label.items(), key=lambda x: -x[1])),
        }


def decode_text(text, mapping):
    """Rimette i valori originali al posto dei placeholder ([FULLNAME_1] -> "Mario Rossi").

    Tokens are replaced once, including when adjacent to ordinary text.
    Replacement values are literal data and are never decoded recursively.
    """
    if not mapping:
        return text, 0
    # One pass: mapped values are data, even when they contain another token.
    count = 0
    def restore(match):
        nonlocal count
        if match.group() not in mapping:
            return match.group()
        count += 1
        return mapping[match.group()]
    return PLACEHOLDER_RE.sub(restore, text), count

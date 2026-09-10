"""Privacy boundary della chat: registro PII conversation-scoped, redazione
degli allegati e decodifica locale delle risposte.

L'anonimizzazione è una proprietà della CONVERSAZIONE (`Conversation.
anonymized`), non del singolo file o del singolo invio, e avviene tutta in un
colpo solo al momento dell'INVIO — vedi `anonymize_turn()`, che è l'unico
punto di ingresso. Prima si rileva su tutti gli allegati e sul prompt, poi si
redige ogni cosa col registro FINALE del turno: così tutti i contenuti di un
turno condividono una sola `mapping_version` e nessun file può essere scritto
con un registro incompleto.

I placeholder vengono allocati sotto un lock per conversazione, che copre
l'intero turno: un invio e un altro invio non possono osservare lo stesso
contatore e assegnarlo a entità diverse.
"""

import hashlib
import io
import json
import math
import mimetypes
import re
import threading
import unicodedata
import uuid
import zipfile
from functools import lru_cache
from pathlib import Path

import fitz  # PyMuPDF

from . import db, jobs, settings_store
from .config import DATA_DIR, MODEL_DIR
from .db import (Attachment, Conversation, ConversationEntity,
                 ConversationEntityAlias)
from .engine import image_ocr
from .engine.detectors import EXACT_SPAN_LABELS
from .engine.text_patterns import exact_pattern
from .engine.convert import to_docx, to_pdf, to_pptx, to_xlsx
from .engine.core import PiiEngine
from .engine.mupdf_lock import mupdf_serialized
from .engine.docx import extract_text as extract_docx, redact_docx
from .engine.pdf import extract_text as extract_pdf
from .engine.pdf_export import (PLACEHOLDER_RE, PdfError, _left_placeholders,
                                _too_noisy, _value_pattern, redact_pdf,
                                restore_pdf, sub_placeholders)
from .engine.pptx import extract_text as extract_pptx, redact_pptx
from .engine.progress import JobCanceled, JobControl
from .engine.txt import TEXT_EXTS
from .engine.txt import extract_text as extract_txt, redact_txt
from .engine.xlsx import anonymize_xlsx, redact_xlsx, restore_cell_styles
from .engine.xlsx import extract_text as extract_xlsx
from .logging_setup import get_logger
from .openrouter import briefing

log = get_logger("blockingbear.chat")

_locks = {}
_locks_guard = threading.Lock()
_PH_RE = re.compile(r"^\[([A-Za-z0-9_]+)_(\d+)\]$")
# Formati che passano dal redattore di testo semplice: il file È il testo,
# quindi .csv/.json/.xml costano quanto un .txt e non c'è motivo di rifiutarli.
# Stesso elenco del motore (engine.txt): chi accetta l'upload e chi redige
# devono accettare le stesse cose, o l'utente scopre il buco a metà lavoro.
_TEXT_EXTS = set(TEXT_EXTS)
_PROTECTED_EXTS = {".pdf", ".docx", ".pptx", ".xlsx"} | _TEXT_EXTS
_LEGACY_EXTS = {".doc", ".odt", ".ppt", ".odp", ".xls", ".xlsm", ".ods"}
# Allegati IMMAGINE (png/jpg/...): accettati solo se lo stack OCR c'è — la
# redazione avviene nei pixel (image_ocr), il flag ocr arriva col turno.
# I box nei pixel usano il GIALLO di default di image_ocr, lo stesso della
# redazione del testo PDF: due soli sfondi in tutta l'app (giallo=redatto,
# nero=sigillato); che il testo venga dall'OCR lo dice il tooltip dell'anteprima.
_IMAGE_EXTS = set(image_ocr.IMAGE_EXTS)

# Sotto questa soglia una superficie non si cerca per TESTO. `_too_noisy`
# (upstream) è tarato sulla redazione di un singolo PDF, dove il valore viene
# da quel documento; qui il registro è condiviso da tutti i file e da tutti i
# prompt della conversazione e la ricerca è case insensitive, quindi una sigla
# di provincia come "LO" o "MI" cancellerebbe ogni "lo" e ogni "mi" del testo
# italiano. Le superfici troppo corte restano in chiaro e vengono dichiarate in
# `skipped`: la copertura primaria non è questa ricerca cieca ma il
# rilevamento nel contesto, che gira su ogni file e su ogni prompt.
_SEARCH_MIN_ALNUM = 4
# Forme societarie da sole: non identificano nessuno e non meritano un
# placeholder (il rilevatore le produce quando taglia male una ragione sociale).
_LEGAL_FORMS = {"srl", "srls", "spa", "sas", "snc", "sapa", "ss", "scarl",
                "ltd", "limited", "inc", "llc", "plc", "gmbh", "sa", "bv",
                "coop", "cooperativa", "societacooperativa"}


@lru_cache(maxsize=8192)
def _pattern(value):
    """`_value_pattern` con memoria. Il registro di una conversazione può
    avere centinaia di superfici (una colonna di codici prodotto in un Excel
    ne produce una per valore) e ognuna viene cercata a ogni prompt, a ogni
    file e a ogni controllo di uscita: senza memoria, compilare una regex
    carattere-per-carattere costa più della ricerca che deve servire."""
    return _value_pattern(value)


@lru_cache(maxsize=4096)
def _exact_pattern(value):
    """Ricerca ESATTA: una password è case-sensitive e la sua punteggiatura
    fa parte del valore ("Estate2024!" non è "estate2024"). Confini di parola
    solo dove il valore comincia o finisce con un carattere di parola."""
    return exact_pattern(value)


def _pattern_for(ph, value):
    """La regex di ricerca giusta per il placeholder: esatta per le label a span
    esatta (credenziali, hash, chiavi, indirizzi di rete),
    tollerante (case, spazi, sillabazione) per tutto il resto."""
    m = _PH_RE.match(ph or "")
    if m and m.group(1) in EXACT_SPAN_LABELS:
        return _exact_pattern(value)
    return _pattern(value)


def _alnum(value):
    return re.sub(r"[\W_]+", "",
                  unicodedata.normalize("NFKC", str(value or "")))


def _searchable(value):
    """La superficie si può cercare per testo senza colpire parole comuni?"""
    if not value or _too_noisy(value):
        return False
    return len(_alnum(value)) >= _SEARCH_MIN_ALNUM


def _junk_value(label, value):
    """Span che non individua nessuno: un carattere, un frammento, la sola
    forma societaria. Non consuma un placeholder e resta in chiaro: non è PII
    e, entrando nel registro, avvelenerebbe ogni controllo successivo."""
    if _label_group(label) in EXACT_SPAN_LABELS:
        return not value
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if len(_alnum(text)) < 2:
        return True
    if _label_group(label) == "ORG" and text.casefold().replace(".", "").replace(
            " ", "").replace("-", "") in _LEGAL_FORMS:
        return True
    return False


def conversation_lock(conv_id):
    with _locks_guard:
        return _locks.setdefault(conv_id, threading.RLock())


def release_lock(conv_id):
    with _locks_guard:
        _locks.pop(conv_id, None)


# --- Scope del registro (chat libere vs progetti) ---------------------------
# Il registro PII è sempre "conversation-scoped" nel codice, ma lo SCOPE può
# essere un progetto: le chat di un progetto condividono registro, contatore
# mapping_version e lock. Tutte le funzioni di questo modulo che prendono un
# conv_id si aspettano in realtà lo scope id; questi helper lo risolvono.

def registry_holder(session, conv):
    """Il PORTATORE del registro: la riga con `.id` (scope delle entità) e
    `.mapping_version` (contatore monotono). Per una chat di progetto è il
    Project; per una chat libera (o per un Project passato direttamente) è
    l'oggetto stesso."""
    pid = getattr(conv, "project_id", None)
    if pid:
        holder = session.get(db.Project, pid)
        if holder is not None:
            return holder
    return conv


def registry_scope(conv_id):
    """Lo scope id del registro di una conversazione, risolto PRIMA di
    prendere il lock (serve una sessione usa-e-getta). Conversazione
    sconosciuta -> il conv_id stesso (il chiamante fallirà con il suo 404)."""
    with db.SessionLocal() as session:
        conv = session.get(Conversation, conv_id)
        if conv is not None and getattr(conv, "project_id", None):
            return conv.project_id
    return conv_id


def _label_group(label):
    label = (label or "PII").upper()
    if label in {"PERSON", "PER", "NAME", "FULL_NAME", "FULLNAME"}:
        return "FULLNAME"
    if label in {"ORGANIZATION", "ORGANISATION", "COMPANY", "ORG"}:
        return "ORG"
    return label


def _surface_core(label, value):
    """Il valore normalizzato, senza il prefisso di label: Unicode, case,
    spazi; ORG perde la forma societaria, FULLNAME perde il titolo e ordina i
    token (in italiano "Bellandi Marco" e "Marco Bellandi" sono la stessa
    persona: stesso insieme di token, nessuna ambiguità da risolvere)."""
    group = _label_group(label)
    if group in EXACT_SPAN_LABELS:
        # credenziali e identificativi tecnici: confronto esatto, niente casefold
        # né punteggiatura tolta
        return str(value or "")
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    if group == "FULLNAME":
        text = re.sub(r"^(?:dott(?:\.ssa)?|dr|avv|ing|prof|mr|mrs|ms|miss|mme|mlle|monsieur|madame|herr|frau|sr|sra|señor|señora|dhr|mevr|meneer|mevrouw)\.?\s+", "", text)
    # punteggiatura controllata: conserva lettere, numeri, @, + e trattini
    text = re.sub(r"[.,;:'\"()]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if group == "ORG":
        # `[li1|]` dove la forma societaria vuole una "l": sui documenti
        # scansionati l'OCR confonde regolarmente quel glifo con "I", "1" o
        # "|", e la "l" finale di "S.r.l." è isolata tra due punti, cioè nel
        # posto peggiore per essere letta bene. Senza questa tolleranza
        # "S.r.I." non viene riconosciuta come forma societaria, resta nel
        # core e spacca in due l'entità ("labconsulenze" da una parte,
        # "labconsulenze s r i" dall'altra). Solo questa posizione: le sigle
        # inglesi (ltd, llc) si scrivono senza punti e non corrono lo stesso
        # rischio, allargarle aggiungerebbe solo falsi positivi.
        text = re.sub(
            r"\s+(?:s\s*r\s*[li1|]\s*s?|s\s*p\s*a|s\s*a\s*s|s\s*n\s*c|"
            r"societa cooperativa|cooperativa|ltd|limited|inc|llc|gmbh|b\s*v|n\s*v|sarl|s\s*l)$", "", text)
        text = re.sub(r"\s+", " ", text).strip()
    if group == "FULLNAME":
        text = " ".join(sorted(text.split(" ")))
    return text


def _surface_key(label, value):
    """Chiave di risoluzione: confronto sempre vincolato a label compatibili."""
    return f"{_label_group(label)}:{_surface_core(label, value)}"


# Una ragione sociale senza forma societaria ("Acme Analytics" per "Acme
# Analytics Srl") può sfuggire del tutto al rilevatore nel file successivo.
# Si cerca allora anche la forma normalizzata, ma solo quando è abbastanza
# specifica: una sola parola ("Fornitore") colpirebbe il testo comune.
_ORG_VARIANT_MIN_TOKENS = 2
_ORG_VARIANT_MIN_ALNUM = 6


def _derived_surfaces(entity, surfaces):
    """Superfici NON viste nei documenti ma implicate da quelle viste."""
    if _label_group(entity.label) != "ORG":
        return []
    core = _surface_core(entity.label, entity.canonical_value)
    if (len(core.split(" ")) < _ORG_VARIANT_MIN_TOKENS
            or len(_alnum(core)) < _ORG_VARIANT_MIN_ALNUM):
        return []
    if any(s.casefold() == core for s in surfaces):
        return []
    return [core]


class ReplacementMapping(dict):
    """Dizionario canonico che espone ai redattori anche alias duplicati.

    I redattori esistenti iterano `mapping.items()`: qui possono quindi vedere
    più coppie (stesso placeholder, superfici diverse), mentre JSON e decode
    continuano ad avere un normale placeholder -> valore canonico.

    `exclude` = gruppi di label che questa conversazione ha scelto di NON
    anonimizzare. Toglie le coppie dalle SOSTITUZIONI, mai dal dizionario
    canonico: i placeholder di quelle categorie possono essere già partiti in
    un turno precedente, e devono continuare a decodificarsi per sempre. È il
    solo punto in cui l'esclusione morde, e per questo lo fa una volta sola:
    redattori, difesa deterministica e controllo di uscita leggono tutti da qui.
    """

    def __init__(self, canonical=None, replacements=None, derived=(),
                 exclude=None):
        super().__init__(canonical or {})
        self._replacements = []
        self.skipped = []       # placeholder troppo corti per essere cercati
        self.derived = set(derived)   # coppie NON viste, dedotte dal registro
        self.exclude = set(exclude or ())
        for ph, value in replacements or ():
            self.add_replacement(ph, value)
        for ph, value in dict.items(self):
            self.add_replacement(ph, value)

    def _is_excluded(self, ph):
        if not self.exclude:
            return False
        if ph in self.exclude:
            # esclusione PER-ENTITÀ (placeholder intero): l'utente ha scelto
            # dall'anteprima pre-invio di lasciare QUEL valore in chiaro
            return True
        match = _PH_RE.match(ph or "")
        return bool(match) and _label_group(match.group(1)) in self.exclude

    def add_replacement(self, ph, value):
        if not value:
            return
        if self._is_excluded(ph):
            # categoria lasciata in chiaro per scelta: NON è uno `skipped`
            # (quelli sono i valori che il redattore non sa cercare)
            return
        if not _searchable(value):
            # resta nel dizionario canonico (serve a decodificare i TAG già
            # inseriti dagli span), ma non si cerca nei documenti
            if ph not in self.skipped:
                self.skipped.append(ph)
            return
        pair = (ph, value)
        if pair not in self._replacements:
            self._replacements.append(pair)

    def items(self):
        return list(self._replacements)

    def replacement_items(self):
        return list(self._replacements)

    def copy(self):
        out = ReplacementMapping(dict(self), self._replacements, self.derived,
                                 self.exclude)
        out.skipped = list(self.skipped)
        return out

    def for_text(self, text):
        """La mappa depurata delle varianti dedotte che, in QUESTO testo,
        pescherebbero più largo della superficie che rappresentano.

        `_value_pattern` unisce i token con `\\s*`, cioè ammette ZERO spazi:
        la variante "acme analytics" trova anche "acmeanalytics" dentro
        `info@acmeanalytics.com`. Su una superficie vista nel documento è
        l'euristica (voluta) di upstream contro gli a-capo; su una superficie
        che nessuno ha mai scritto è solo un falso positivo, e fa fallire la
        verifica dei residui.

        Il rischio si giudica sul testo con le superfici VISTE già coperte,
        non sul testo nudo. La forma attaccata sta quasi sempre dentro un'altra
        superficie nota — `https://www.acmeanalytics.com` è a sua volta un
        [URL_n] del registro — che i redattori coprono per intera (la più lunga
        vince) prima che la variante possa spezzarla: lì non c'è nessun
        rischio, e scartare la variante lasciava "Acme Analytics" in chiaro
        accanto a un URL coperto. Peggio: il controllo di uscita legge il testo
        COPERTO, dove l'URL è già [URL_n] e la forma attaccata non c'è più,
        quindi rivalutava la stessa regola in senso opposto e condannava il
        risultato — pagine web scartate, conferma dei file di progetto
        rifiutata con un riallineamento che rifaceva identico il calcolo.
        Coprire prima le superfici viste rende le due letture identiche.
        Se la forma attaccata sopravvive in un punto che il registro NON
        conosce (un handle "@acmeanalytics", un nome di file) la variante
        resta esclusa come prima."""
        if not self.derived or not text:
            return self
        probe = text
        for ph, surface in sorted(self._replacements,
                                  key=lambda pair: -len(pair[1])):
            if (ph, surface) in self.derived:
                continue
            pattern = _pattern_for(ph, surface)
            if pattern is not None and pattern.search(probe):
                probe = pattern.sub(ph, probe)
        risky = set()
        for ph, surface in self.derived:
            joined = re.sub(r"\s+", "", surface)
            if len(joined) < len(surface) and re.search(
                    r"(?<!\w)" + re.escape(joined) + r"(?!\w)", probe,
                    re.IGNORECASE):
                risky.add((ph, surface))
        if not risky:
            return self
        out = ReplacementMapping(
            dict(self), [pair for pair in self._replacements
                         if pair not in risky],
            self.derived - risky, self.exclude)
        out.skipped = list(self.skipped)
        return out

    def __setitem__(self, ph, value):
        super().__setitem__(ph, value)
        self.add_replacement(ph, value)

    def __delitem__(self, ph):
        """Simmetrico di __setitem__: togliere un placeholder deve togliere
        anche le sue SUPERFICI, o i redattori (che iterano `items()`, cioè
        `_replacements`) continuerebbero a sostituirlo pur non trovandolo più
        nel dizionario canonico. Lo usa engine/xlsx_table.PlaceholderAllocator
        .forget per le intestazioni di colonna lasciate in chiaro."""
        super().__delitem__(ph)
        self._replacements = [pair for pair in self._replacements
                              if pair[0] != ph]
        self.derived = {pair for pair in self.derived if pair[0] != ph}
        if ph in self.skipped:
            self.skipped.remove(ph)

    def pop(self, ph, *default):
        if ph not in self:
            if default:
                return default[0]
            raise KeyError(ph)
        value = self[ph]
        del self[ph]
        return value


def _registry_rows(session, conv_id):
    entities = (session.query(ConversationEntity)
                .filter_by(conv_id=conv_id).all())
    aliases = (session.query(ConversationEntityAlias, ConversationEntity)
               .join(ConversationEntity,
                     ConversationEntityAlias.entity_id == ConversationEntity.id)
               .filter(ConversationEntity.conv_id == conv_id).all())
    return entities, aliases


def _resolve_merges(entities):
    """id -> entità che la assorbe (se stessa se non fusa). Le fusioni sono
    una catena corta ma si segue comunque, con guardia sui cicli."""
    by_id = {e.id: e for e in entities}
    out = {}
    for entity in entities:
        target, seen = entity, {entity.id}
        while target.merged_into and target.merged_into in by_id:
            nxt = by_id[target.merged_into]
            if nxt.id in seen:
                break
            seen.add(nxt.id)
            target = nxt
        out[entity.id] = target
    return out


def known_surfaces(entities, aliases, max_version=None):
    """[(placeholder, superficie)] cercabili nel testo.

    `max_version` limita il registro a com'era a quella versione: un allegato
    protetto non può essere giudicato con superfici diventate note DOPO che è
    stato scritto (sarebbe la riscrittura retroattiva che il design vieta, e in
    pratica bloccherebbe la conversazione per sempre)."""
    if max_version is not None:
        entities = [e for e in entities
                    if (e.mapping_version or 0) <= max_version]
        ids = {e.id for e in entities}
        aliases = [(a, e) for a, e in aliases
                   if e.id in ids and (a.mapping_version or 0) <= max_version]
    target = _resolve_merges(entities)
    by_entity = {}
    out, derived = [], []
    for alias, entity in aliases:
        by_entity.setdefault(entity.id, []).append(alias.original_surface)
        out.append((target.get(entity.id, entity).placeholder,
                    alias.original_surface))
    for entity in entities:
        if entity.merged_into:
            continue
        for surface in _derived_surfaces(entity, by_entity.get(entity.id, ())):
            derived.append((entity.placeholder, surface))
    return out + derived, derived


def conversation_mapping(session, conv_id, include_aliases=False,
                         max_version=None, exclude=None):
    """La mappa della conversazione. Senza alias è il dizionario canonico per
    la DECODIFICA, che non conosce esclusioni (un placeholder già inviato si
    decodifica sempre); con gli alias è la mappa delle SOSTITUZIONI, e lì
    `exclude` (gruppi di label lasciati in chiaro) conta."""
    entities, aliases = _registry_rows(session, conv_id)
    if max_version is not None:
        entities = [e for e in entities
                    if (e.mapping_version or 0) <= max_version]
        entity_ids = {e.id for e in entities}
        aliases = [(a, e) for a, e in aliases if e.id in entity_ids]
    # anche le entità fuse restano nel dizionario canonico: i messaggi già
    # inviati contengono il loro placeholder e devono continuare a decodificarsi
    canonical = {e.placeholder: e.canonical_value for e in entities}
    if not include_aliases:
        return canonical
    surfaces, derived = known_surfaces(entities, aliases, max_version)
    # le entità escluse una a una (anteprima pre-invio) escono dalle
    # SOSTITUZIONI esattamente come le categorie escluse: restano nel
    # dizionario canonico per la decodifica di ciò che è già partito
    excl = set(exclude or ()) | excluded_placeholders(entities)
    return ReplacementMapping(canonical, surfaces, derived, excl)


def _forced_spans(text, mapping):
    """Occorrenze delle superfici GIÀ note, sul testo originale.

    Vanno risolte PRIMA degli span del rilevatore: se il modello vede solo
    "Acme" dentro "Acme Analytics Srl", sostituire il frammento per primo
    spezzerebbe la ragione sociale e il resto uscirebbe in chiaro. Le
    superfici più lunghe vincono, e nessuna si sovrappone."""
    spans = []
    for ph, value in sorted(mapping.items(), key=lambda kv: -len(kv[1])):
        pattern = _pattern_for(ph, value)
        if pattern is None:
            continue
        for match in pattern.finditer(text):
            if any(match.start() < end and match.end() > start
                   for start, end, _ph in spans):
                continue
            spans.append((match.start(), match.end(), ph))
    return sorted(spans)


def apply_known_surfaces(text, mapping):
    """Difesa deterministica sui valori GIÀ noti: anche se il modello NER
    manca un'occorrenza, un alias del registro non può ricomparire
    nell'egress protetto. `mapping.items()` contiene solo le superfici
    cercabili (vedi _searchable): le sigle troppo corte le copre il
    rilevamento nel contesto, che gira su ogni file e su ogni prompt."""
    for ph, value in sorted(mapping.items(), key=lambda item: -len(item[1])):
        pattern = _pattern_for(ph, value)
        if pattern:
            text = pattern.sub(lambda _match, replacement=ph: replacement, text)
    return text


class ConversationEngine:
    """Adapter del PiiEngine: rileva normalmente, poi sostituisce gli ID locali
    con quelli persistenti della conversazione."""

    def __init__(self, base, session, conv):
        self.base = base
        self.session = session
        self.conv = conv
        self._created = {}          # entità NATE nell'ultima analyze()

    def keep_in_clear(self, placeholders):
        """Marca "in chiaro" (ConversationEntity.excluded, la stessa scelta
        dell'anteprima pre-invio) entità appena registrate: da qui in poi il
        loro valore non si sostituisce più, né qui né nei turni successivi.

        La usa il motore xlsx per le INTESTAZIONI DI COLONNA taggate per
        errore (engine/xlsx._header_only_phs): senza, l'etichetta resterebbe
        una superficie nota del registro e verrebbe sostituita in ogni file e
        messaggio successivo dello scope — le intestazioni sono spesso parole
        comuni ("Marchio", "Modello") e il danno sarebbe permanente.

        Solo le entità CREATE dall'ultima analyze(): una superficie già nota
        al registro è una decisione presa altrove, su un altro documento, e
        non si ribalta da qui. Ritorna quante ne ha marcate."""
        n = 0
        for ph in placeholders:
            entity = self._created.get(ph)
            if entity is None or getattr(entity, "excluded", 0):
                continue
            entity.excluded = 1
            n += 1
        if n:
            self.session.flush()
        return n

    def analyze(self, text, excluded=None, custom_terms=None, ctl=None):
        result = self.base.analyze(text, excluded=excluded,
                                   custom_terms=custom_terms, ctl=ctl)
        self._created = {}
        # Le categorie escluse valgono anche sul REGISTRO, non solo sul
        # rilevatore: un valore diventato noto quando la categoria era attiva
        # ricomparirebbe altrimenti come placeholder per sempre, e "da qui in
        # poi in chiaro" non sarebbe vero.
        skip = excluded_groups(excluded)
        entities, aliases = _registry_rows(self.session, self.conv.id)
        by_key = {}
        counters = {}
        for entity in entities:
            m = _PH_RE.match(entity.placeholder)
            if m:
                counters[m.group(1)] = max(counters.get(m.group(1), 0),
                                           int(m.group(2)))
        target = _resolve_merges(entities)
        for alias, entity in aliases:
            # una superficie fusa risolve sull'entità che l'ha assorbita
            by_key.setdefault(alias.normalized_key,
                              target.get(entity.id, entity))

        known = conversation_mapping(self.session, self.conv.id,
                                     include_aliases=True,
                                     exclude=skip).for_text(text)
        forced = _forced_spans(text, known)
        if hasattr(text, "contains_span"):
            forced = [span for span in forced if text.contains_span(span[0], span[1])]

        kept, superseded = [], set()
        for found in result["entities"]:
            label = _label_group(found["label"])
            value = found["value"]
            if _junk_value(label, value):
                continue
            overlap = [span for span in forced
                       if found["start"] < span[1] and found["end"] > span[0]]
            if overlap:
                # vince la più lunga: il frammento "GDP" non deve spezzare la
                # superficie nota "Acme Analytics", ma "AZIENDA X SRL" appena
                # rilevata deve prevalere sulla variante nota "azienda x"
                length = found["end"] - found["start"]
                if not all(length > span[1] - span[0] for span in overlap):
                    continue
                superseded.update(overlap)
            kept.append(found)
        forced = [span for span in forced if span not in superseded]

        resolved = []
        for found in kept:
            label = _label_group(found["label"])
            value = found["value"]
            key = _surface_key(label, value)
            entity = by_key.get(key)
            if entity is not None and getattr(entity, "excluded", 0):
                # scelta esplicita dell'utente (anteprima pre-invio): questo
                # valore resta in chiaro, qui e nei turni successivi
                continue
            if entity is None:
                counters[label] = counters.get(label, 0) + 1
                ph = f"[{label}_{counters[label]}]"
                self.conv.mapping_version = (self.conv.mapping_version or 0) + 1
                entity = ConversationEntity(
                    conv_id=self.conv.id, placeholder=ph, label=label,
                    canonical_value=value,
                    mapping_version=self.conv.mapping_version)
                self.session.add(entity)
                self.session.flush()
                entities.append(entity)
                target[entity.id] = entity
                self._created[entity.placeholder] = entity
            found["ph"] = entity.placeholder
            # Un alias nuovo vale per gli invii successivi: i model_content
            # già inviati non si toccano mai.
            exists = any(a.entity_id == entity.id and
                         a.original_surface == value
                         for a, _e in aliases)
            if not exists:
                self.conv.mapping_version = (self.conv.mapping_version or 0) + 1
                alias = ConversationEntityAlias(
                    entity_id=entity.id, original_surface=value,
                    normalized_key=key, source=found.get("source") or "detector",
                    confidence=("deterministic" if label in {"ORG", "FULLNAME"}
                                else "exact"),
                    mapping_version=self.conv.mapping_version)
                self.session.add(alias)
                aliases.append((alias, entity))
                by_key[key] = entity
            resolved.append(found)
        kept = resolved
        for start, end, ph in forced:
            match = _PH_RE.match(ph)
            kept.append({"label": match.group(1) if match else "PII",
                         "ph": ph, "start": start, "end": end,
                         "value": text[start:end], "source": "registro",
                         "validated": True})
        kept.sort(key=lambda e: e["start"])
        result["entities"] = kept

        pieces, pos = [], 0
        for found in kept:
            pieces.extend((text[pos:found["start"]], found["ph"]))
            pos = found["end"]
        pieces.append(text[pos:])
        anonymized = "".join(pieces)
        mapping = conversation_mapping(
            self.session, self.conv.id, include_aliases=True,
            exclude=skip).for_text(text)
        result["anonymized_text"] = apply_known_surfaces(anonymized, mapping)
        result["mapping"] = mapping
        result["n_entities"] = len(kept)
        result["n_unique"] = len({e["ph"] for e in kept})
        by_label = {}
        for found in kept:
            by_label[found["label"]] = by_label.get(found["label"], 0) + 1
        result["by_label"] = dict(sorted(by_label.items(), key=lambda x: -x[1]))
        return result


def sync_allocated_mapping(session, conv, mapping):
    """Registra i placeholder aggiunti deterministicamente dal percorso
    tabellare XLSX (valori unici di colonna), sempre sotto il lock della
    conversazione già detenuto dal chiamante."""
    entities, aliases = _registry_rows(session, conv.id)
    by_ph = {e.placeholder: e for e in entities}
    for ph, value in dict.items(mapping):
        match = _PH_RE.match(ph)
        if match is None or _junk_value(match.group(1), value):
            continue
        entity = by_ph.get(ph)
        if entity is None:
            conv.mapping_version = (conv.mapping_version or 0) + 1
            entity = ConversationEntity(
                conv_id=conv.id, placeholder=ph, label=match.group(1),
                canonical_value=value, mapping_version=conv.mapping_version)
            session.add(entity)
            session.flush()
            by_ph[ph] = entity
        key = _surface_key(entity.label, value)
        if not any(a.entity_id == entity.id and a.original_surface == value
                   for a, _e in aliases):
            conv.mapping_version = (conv.mapping_version or 0) + 1
            alias = ConversationEntityAlias(
                entity_id=entity.id, original_surface=value,
                normalized_key=key, source="xlsx_table",
                confidence="deterministic",
                mapping_version=conv.mapping_version)
            session.add(alias)
            aliases.append((alias, entity))


# --- Categorie da anonimizzare, per conversazione --------------------------
# I default dell'admin (settings_store) sono la base; la toolbar della chat
# può spegnere qualche categoria per QUELLA conversazione, e la scelta vale
# dai turni successivi. Le esclusioni non riscrivono niente all'indietro: i
# messaggi già partiti restano come sono partiti, e i loro placeholder
# continuano a decodificarsi (vedi ReplacementMapping.exclude).

def _own_excluded(conv):
    """Le categorie escluse SCELTE da questa conversazione, oppure None se non
    ha mai scelto (allora valgono quelle dell'amministratore). Un JSON corrotto
    a mano nel DB ricade su None: mai bloccare un invio per questo."""
    try:
        own = json.loads(getattr(conv, "anon_options_json", None) or "{}")
    except ValueError:
        return None
    tags = own.get("excluded_tags") if isinstance(own, dict) else None
    if not isinstance(tags, list):
        return None
    try:
        return settings_store.clean_excluded(tags)
    except ValueError:
        return None


def _owner_terms(session, holder):
    """I termini personali di CHI POSSIEDE il registro (il progetto, o la chat
    libera), non di chi sta facendo la richiesta.

    È la sola definizione che regge: la redazione gira anche nei job in
    background (upload, riallineamento, rielaborazione OCR), dove nessun utente
    è collegato e non esiste alcun utente della richiesta. E dove esiste sarebbe
    la scelta sbagliata: un amministratore che apre il progetto di un altro lo
    ri-redigerebbe con i PROPRI termini, e lo stesso file uscirebbe diverso a
    seconda di chi l'ha toccato per ultimo. Un registro, un proprietario, una
    lista."""
    owner_id = getattr(holder, "owner_id", None)
    owner = session.get(db.User, owner_id) if owner_id else None
    return settings_store.user_terms(owner)


def anon_options(session, conv):
    """Le opzioni di anonimizzazione EFFETTIVE del turno: i default dell'admin
    con, se la conversazione ha scelto, le sue categorie escluse. Per le chat
    di PROGETTO le categorie sono quelle del progetto (scelte alla creazione):
    chat diverse con esclusioni diverse produrrebbero contenuti incoerenti
    sullo stesso registro.

    I termini specifici sono l'UNIONE di due liste: quella globale
    dell'amministratore (regola dell'installazione, nessuno se la toglie) e
    quella personale del proprietario del registro. Questo è l'unico punto in
    cui l'unione si calcola: ci passano tutte le redazioni — turno di chat,
    upload di progetto, anteprima pre-invio, colonne xlsx, riallineamento e
    rielaborazione OCR — quindi non esiste una strada che veda una lista
    diversa da un'altra."""
    defaults = settings_store.anon_defaults(session)
    holder = registry_holder(session, conv)
    own = _own_excluded(holder)
    if own is not None:
        defaults["excluded_tags"] = own
    defaults["custom_terms"] = settings_store.merge_terms(
        defaults["custom_terms"], _owner_terms(session, holder))
    return defaults


def excluded_groups(excluded_tags):
    """I gruppi di label del registro coperti dalle categorie escluse: è la
    forma che serve a ReplacementMapping (il registro normalizza le label con
    `_label_group`, es. PERSON -> FULLNAME)."""
    return {_label_group(t) for t in (excluded_tags or ())}


def excluded_placeholders(entities):
    """I placeholder delle entità che l'utente ha scelto di lasciare in
    chiaro (anteprima pre-invio): entrano nello stesso set `exclude` dei
    gruppi di label — un placeholder intero non collide mai con un nome di
    gruppo, quindi il set può contenerli entrambi."""
    return {e.placeholder for e in entities if getattr(e, "excluded", 0)}


def anon_terms_view(session, holder):
    """I termini effettivi ANNOTATI con la provenienza (`scope`: "global" =
    fissato dall'amministratore, "personal" = del proprietario del registro).
    Serve a chi li mostra: l'elenco presentato come globale (chiave i18n
    `anon.panel.alwaysGlobal`) mentre contiene anche i propri è
    un'informazione falsa, e i due livelli si tolgono in posti diversi."""
    return settings_store.merge_terms(
        settings_store.anon_defaults(session)["custom_terms"],
        _owner_terms(session, holder), scoped=True)


def anon_options_view(session, conv):
    """Ciò che la toolbar della chat mostra: categorie escluse effettive,
    termini sempre coperti (in sola lettura, con la loro provenienza) e se la
    conversazione sta ancora seguendo i default. Per le chat di progetto
    `inherited` racconta lo stato del PROGETTO (le sue categorie valgono per
    tutte le chat; il frontend lo sa già da conv.project_id)."""
    holder = registry_holder(session, conv)
    return {"excluded_tags": anon_options(session, conv)["excluded_tags"],
            "custom_terms": anon_terms_view(session, holder),
            "inherited": _own_excluded(holder) is None}


def set_anon_options(conv, excluded_tags):
    """Fissa le categorie escluse della conversazione; `excluded_tags` None
    torna a seguire i default dell'amministratore. ValueError user-facing."""
    if excluded_tags is None:
        conv.anon_options_json = "{}"
        return None
    tags = settings_store.clean_excluded(excluded_tags)
    conv.anon_options_json = json.dumps({"excluded_tags": tags},
                                        ensure_ascii=False)
    return tags


def _base_engine():
    # Riusa il modello già caricato dalla coda documenti. PiiEngine protegge
    # l'inferenza con il proprio lock, quindi worker documenti e chat non
    # attraversano contemporaneamente la stessa pipeline transformers.
    if jobs.ENGINES:
        return jobs.ENGINES[0]
    return PiiEngine(MODEL_DIR)


def _converted_input(data, filename):
    low = filename.lower()
    if low.endswith((".doc", ".odt")) and not low.endswith(".docx"):
        return to_docx(data, suffix=Path(low).suffix), ".docx"
    if low.endswith((".ppt", ".odp")) and not low.endswith(".pptx"):
        return to_pptx(data, suffix=Path(low).suffix), ".pptx"
    if low.endswith((".xls", ".xlsm", ".ods")) and not low.endswith(".xlsx"):
        return to_xlsx(data, suffix=Path(low).suffix), ".xlsx"
    return data, Path(low).suffix


def _scrub_without_entities(data, redactor):
    """Esegue comunque la pulizia metadati del formato quando il detector non
    trova entità. Il valore casuale non può avere occorrenze nel documento e
    viene rimosso dal report prima della persistenza."""
    ph = "[BLOCKINGBEAR_META_0]"
    dummy = ReplacementMapping(
        {ph: f"blockingbear-no-match-{uuid.uuid4().hex}"})
    out, report = redactor(data, dummy)
    report["by_placeholder"] = {}
    for key in ("not_found", "skipped", "residual"):
        report[key] = [value for value in report.get(key, []) if value != ph]
    report["occurrences"] = 0
    return out, report


def _stats(analysis):
    return {"n_entities": analysis.get("n_entities", 0),
            "n_unique": analysis.get("n_unique", 0),
            "by_label": analysis.get("by_label", {})}


def _detect(data, ext, engine, defaults, ctl=None, xlsx_max_chunks=None,
            ocr=False):
    """Pass A del turno: estrazione + RILEVAMENTO, che alloca le entità nel
    registro della conversazione. Non produce nessun file: la redazione arriva
    dopo, quando il registro del turno è completo (vedi `anonymize_turn`).

    Con ocr=True le immagini (dentro pdf/docx/pptx/xlsx, o l'allegato
    IMMAGINE stesso) vengono lette con RapidOCR e il testo letto passa allo
    STESSO engine.analyze del resto (vedi engine/image_ocr): placeholder e
    registro restano quelli della conversazione. `text` ritorna col corpus
    OCR accodato, così mapping.for_text del pass B include anche le
    superfici viste solo nelle immagini.

    L'xlsx è l'eccezione: rilevamento e allocazione per colonna sono un
    blocco solo (si campionano le righe, si classificano le colonne e si
    allocano i valori unici in modo deterministico) e non sono separabili.
    Il file che ne esce si tiene da parte: il pass B lo riuserà se nel
    frattempo il registro non è cambiato, altrimenti lo riscrive."""
    kw = {"excluded": defaults["excluded_tags"],
          "custom_terms": defaults["custom_terms"]}
    ocr = bool(ocr) and image_ocr.available()
    if ext == ".xlsx":
        result = anonymize_xlsx(data, engine, max_chunks=xlsx_max_chunks,
                                make_preview=False, allow_empty=True,
                                ctl=ctl, ocr=ocr, **kw)
        analysis = result["analysis"]
        # le voci [UNREADABLE_n] e [SIGNATURE_n] restano nella cache OCR
        # dell'allegato: nel registro della conversazione avvelenerebbero
        # i turni successivi
        local = ("[" + image_ocr.UNREADABLE + "_",
                 "[" + image_ocr.SIGNATURE + "_")
        clean = {ph: v for ph, v in analysis["mapping"].items()
                 if not ph.startswith(local)}
        sync_allocated_mapping(engine.session, engine.conv, clean)
        return {"text": None, "file": result["file"],
                "report": result["report"],
                "exact_phs": set(result["report"].get("table_phs") or ()),
                "stats": _stats(analysis),
                "ocr_cache": result.get("ocr_cache")}
    if ext in _IMAGE_EXTS:
        if not ocr:
            raise ValueError(
                "è un'immagine: per anonimizzarla serve l'OCR — ripeti "
                "l'invio attivandolo nel popup, oppure rimuovi il file.")
        text, images = "", [{"key": "img", "ext": ext.lstrip("."),
                             "data": data}]
    elif ext == ".pdf":
        text, _pages = extract_pdf(data, allow_empty=ocr)
        images = image_ocr.pdf_images(data) if ocr else []
    elif ext == ".docx":
        text = extract_docx(data, allow_empty=ocr)
        images = image_ocr.ooxml_images(data) if ocr else []
    elif ext == ".pptx":
        text = extract_pptx(data, full=True, allow_empty=ocr)
        images = image_ocr.ooxml_images(data) if ocr else []
    elif ext in _TEXT_EXTS:
        text, images = extract_txt(data), []
    else:
        raise NotImplementedError("Formato non supportato dalla pipeline di "
                                  "anonimizzazione della chat.")
    pdf_ocr = ocr and ext == ".pdf"
    if ctl is not None:
        ctl.phases((["image_ocr"] if images or pdf_ocr else []) + ["analysis"])
        if images or pdf_ocr:
            ctl.phase("image_ocr")
    if pdf_ocr:
        cache = image_ocr.build_pdf_cache(data, images=images, ctl=ctl)
    else:
        cache = image_ocr.build_cache(images, ctl=ctl) if images else None
    if not text.strip() and cache is None and ext != ".pdf" \
            and ext not in _IMAGE_EXTS:
        raise ValueError("il file non contiene testo.")
    if ctl is not None:
        ctl.phase("analysis")
    analysis = image_ocr.analyze_with_corpus(engine, text, cache, ctl=ctl, **kw)
    if cache:
        corpus_text = image_ocr.corpus(cache)[0]
        text = (text + "\n\n" + corpus_text) if text.strip() else corpus_text
    return {"text": text, "file": None, "report": None, "exact_phs": set(),
            "stats": _stats(analysis), "ocr_cache": cache}


def apply_seals(data, ext, sealed):
    """Aree SIGILLATE dall'utente (rettangolo nero SEALED, contenuto rimosso
    davvero): applicate al file GIÀ redatto. Solo PDF e immagini — gli unici
    formati in cui l'anteprima coincide geometricamente col file — negli
    altri è un no-op (gli endpoint non permettono di crearle).
    Ritorna (bytes, boxes {pagina: [...]}) come pdf.seal_pdf."""
    if not sealed:
        return data, {}
    if ext == ".pdf":
        from .engine.pdf import seal_pdf
        return seal_pdf(data, sealed)
    if ext in _IMAGE_EXTS:
        return image_ocr.seal_image(data, ext, sealed)
    return data, {}


def _redact_with(data, ext, mapping, exact_phs=None, ctl=None, ocr_cache=None,
                 sealed=None):
    """Pass B del turno + aree sigillate: la redazione vera e propria sta in
    `_redact_plain`; qui, DOPO, si applicano le aree sigillate dell'allegato
    (che devono coprire anche il caso 'nessuna entità nel file') e i loro
    box si fondono in report["boxes"] per l'anteprima."""
    out, report = _redact_plain(data, ext, mapping, exact_phs=exact_phs,
                                ctl=ctl, ocr_cache=ocr_cache)
    if sealed:
        out, seal_boxes = apply_seals(out, ext, sealed)
        if seal_boxes:
            boxes = report.setdefault("boxes", {})
            for pno, blist in seal_boxes.items():
                boxes.setdefault(pno, []).extend(blist)
    return out, report


def _redact_plain(data, ext, mapping, exact_phs=None, ctl=None, ocr_cache=None):
    """Redazione con una mappa GIÀ completa, nessuna inferenza. Sono i
    redattori nativi del motore (engine.redact_*), senza le anteprime PDF
    (la chat non le usa).

    ocr_cache (vedi image_ocr): i box nelle immagini seguono la stessa mappa
    del testo; le righe [UNREADABLE_n] — che nel registro della conversazione
    non entrano — si aggiungono alla mappa del momento qui."""
    if ctl is not None:
        ctl.phases(["redaction"])
        ctl.phase("redaction")
    if ocr_cache:
        # .copy() e l'assegnazione voce per voce (NON dict(mapping) +
        # update): una ReplacementMapping deve conservare le coppie alias —
        # boxes_for le ri-cerca nelle righe OCR, e appiattirla ai canonici
        # lascerebbe scoperte le superfici alternative nelle immagini
        mapping = mapping.copy()
        for ph, val in image_ocr.unreadable_mapping(ocr_cache).items():
            mapping[ph] = val
    if ext in _IMAGE_EXTS:
        if not ocr_cache:
            # OCR senza righe lette: niente da coprire, l'immagine passa com'è
            return data, {"occurrences": 0, "by_placeholder": {},
                          "not_found": [], "skipped": [], "residual": []}
        return image_ocr.redact_single_image(data, ext, ocr_cache, mapping)
    redactor = {".pdf": redact_pdf, ".docx": redact_docx,
                ".pptx": redact_pptx, ".xlsx": redact_xlsx}.get(ext, redact_txt)
    if not mapping:
        # nessuna entità in tutto il turno: resta comunque la pulizia dei
        # metadati del formato (autore, ultimo salvataggio, percorsi...)
        if ext in _TEXT_EXTS:
            return data, {}
        return _scrub_without_entities(data, redactor)
    if ext == ".pdf" and ocr_cache:
        # prima le immagini (pixel), poi la redazione del layer testuale:
        # stesso ordine e stesso motivo di pdf.rebuild_pdf
        src, overlay, img_by_ph = image_ocr.redact_pdf_images(
            data, ocr_cache, mapping)
        out, report = redact_pdf(src, mapping)
        for pno, blist in overlay.items():
            report["boxes"].setdefault(pno, []).extend(blist)
        image_ocr.merge_image_report(report, img_by_ph)
        return out, report
    if ext == ".xlsx":
        return redact_xlsx(data, mapping, exact_phs=exact_phs, ctl=ctl,
                           ocr_cache=ocr_cache)
    if ext in (".docx", ".pptx"):
        return redactor(data, mapping, ocr_cache=ocr_cache)
    return redactor(data, mapping)


def upload_eligibility(filename, data):
    """Il file si può anonimizzare? None, oppure il motivo del rifiuto.

    Si guarda SOLO il formato (e, per i PDF, la presenza di un layer
    testuale): nessun rilevamento PII, che è roba del turno. Così l'upload
    resta immediato e l'utente scopre subito cosa non ha strada — invece di
    scoprirlo al primo invio.

    Con lo stack OCR installato (image_ocr.available) si accettano anche le
    IMMAGINI e i PDF scansionati senza layer testuale: la lettura avverrà
    all'invio, se l'utente attiva l'OCR nel popup; se non lo attiva, il turno
    fallisce su quel file con un messaggio chiaro."""
    name = filename or "file"
    ext = Path(name).suffix.lower()
    ocr_ok = image_ocr.available()
    if ext in _IMAGE_EXTS:
        if not ocr_ok:
            return (f"{name}: in una chat anonimizzata le immagini "
                    "richiederebbero l'OCR, che su questo server non è "
                    "installato: per inviarle serve una chat normale.")
        return None
    if ext not in _PROTECTED_EXTS | _LEGACY_EXTS:
        return (f"{name}: in una chat anonimizzata si possono allegare solo "
                "documenti con testo estraibile (PDF, Word, PowerPoint, "
                "Excel, testo, CSV, JSON, XML)"
                + (" o immagini (lette con l'OCR all'invio)." if ocr_ok
                   else ": per altri formati serve una chat normale."))
    try:
        if ext == ".pdf":
            extract_pdf(data, allow_empty=ocr_ok)
        elif ext in _TEXT_EXTS:
            extract_txt(data)
    except (PdfError, ValueError) as exc:
        return f"{name}: {exc}"
    except Exception:
        return f"{name}: il file non è leggibile."
    return None


class TurnAnonymizationError(Exception):
    """Anonimizzazione del turno fallita. `attachment_id` dice su quale file
    (None = il messaggio o la conversazione); `canceled` distingue lo stop
    dell'utente da un guasto."""

    def __init__(self, message, attachment_id=None, canceled=False,
                 code=None):
        super().__init__(message)
        self.attachment_id = attachment_id
        self.canceled = canceled
        # chiave di traduzione (app/errors.py). Questa eccezione esce dal
        # canale SSE del turno, che oggi porta solo `message`: il codice sta
        # qui pronto per quando l'evento d'errore lo trasporterà.
        self.code = code


def _turn_ctl(cancel, on_progress, index, total, filename, stage):
    """Il JobControl che inoltra l'avanzamento del motore verso la UI, con
    l'etichetta del pezzo in lavorazione. Snapshot standard di JobControl
    (fase x di y + done/total), più "pezzo i di n"."""
    if on_progress is None:
        return JobControl(cancel_event=cancel)

    def emit(snap):
        on_progress({"index": index, "total": total, "filename": filename,
                     "stage": stage, "phase": snap.get("phase"),
                     "phase_index": snap.get("phase_index"),
                     "phase_total": snap.get("phase_total"),
                     "units_done": snap.get("done"),
                     "units_total": snap.get("total")})

    return JobControl(cancel_event=cancel, on_progress=emit)


def _turn_report(item, mapping):
    """Il report che finisce sulla scheda dell'allegato.

    `not_found` sparisce: con un registro condiviso da tutta la conversazione,
    elencare i placeholder che NON compaiono in questo file vuol dire
    elencare quelli degli altri file. `skipped` resta, perché quelle sono
    superfici che il redattore ha deciso di non cercare (troppo corte) e
    quindi sono rimaste in chiaro davvero."""
    report = dict(item.get("report") or {})
    report.pop("not_found", None)
    report["by_placeholder"] = {ph: n for ph, n
                                in (report.get("by_placeholder") or {}).items()
                                if n}
    report["skipped"] = sorted(set(report.get("skipped") or ())
                               | set(getattr(mapping, "skipped", ())))
    report.update(item["stats"])
    return report


def att_sealed(att):
    """Aree sigillate persistite sull'allegato ([{n, page, all, rect}]):
    vivono sulla riga perché OGNI ri-protezione riparte dall'originale
    (anteprima, invio diretto, reinvio) e deve riapplicarle."""
    try:
        return json.loads(att.sealed_json or "[]")
    except ValueError:
        return []


def _ocr_cache_path(att):
    """La cache OCR dell'allegato (righe lette + piano dei box), accanto
    all'originale: serve a ogni ri-redazione del turno (anteprima pre-invio)."""
    if not att.original_path:
        return None
    return Path(att.original_path).parent / f"{att.id}_ocr.json"


def load_att_ocr_cache(att):
    """Cache OCR persistita di un allegato, o None (turno senza OCR)."""
    p = _ocr_cache_path(att)
    if p is None or not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_att_ocr_cache(att, cache):
    """Riscrive la cache OCR di un allegato (es. dopo una deanonimizzazione
    di voci locali: image_ocr.exclude_local)."""
    p = _ocr_cache_path(att)
    if p is not None:
        p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def anonymize_turn(conv_id, prompt, att_ids, cancel=None, on_progress=None,
                   capture=None, ocr=False):
    """Anonimizza in un colpo solo TUTTO il turno: gli allegati che partono
    adesso e il messaggio. È l'unico punto di ingresso dell'anonimizzazione.

    Due passaggi, ed è il motivo di questa funzione:

      A) si estrae e si RILEVA su ogni file e sul prompt, così il registro
         della conversazione si completa;
      B) si REDIGE ogni cosa con il registro FINALE.

    I due passaggi servono perché anonimizzare un file all'upload vorrebbe
    dire scriverlo con un registro ancora incompleto: il primo file resterebbe
    in chiaro sulle superfici che il quarto rende note, e all'invio il
    controllo di uscita bloccherebbe il turno senza un rimedio raggiungibile
    dall'interfaccia. Con un solo ingresso quel caso non è rappresentabile:
    tutto il turno condivide una sola `mapping_version`.

    Il turno è ATOMICO: i file redatti si scrivono su disco soltanto quando
    tutti sono riusciti, e un guasto a metà non lascia né file protetti a
    metà né placeholder allocati (rollback).

    `capture` (dict, facoltativo) riceve ciò che serve all'ANTEPRIMA
    pre-invio (chat_staging): gli span del prompt, la mappa di sostituzione
    del turno e i metadati per allegato. Non cambia in niente il percorso
    normale.

    Ritorna (model_content, mapping_version, [descrittori degli allegati])."""
    # lo scope del registro (il progetto, per le chat di progetto) si risolve
    # PRIMA del lock: è il lock stesso a essere scope-wide
    scope = registry_scope(conv_id)
    with conversation_lock(scope), db.SessionLocal() as session:
        conv = session.get(Conversation, conv_id)
        if conv is None:
            raise TurnAnonymizationError("Conversazione non trovata.",
                                         code="conversation_not_found")
        holder = registry_holder(session, conv)
        atts = [a for a in (session.get(Attachment, i) for i in att_ids)
                if a is not None]
        # le categorie scelte in toolbar per QUESTA chat (o, in un progetto,
        # quelle del progetto; altrimenti i default admin)
        defaults = anon_options(session, conv)
        skip = excluded_groups(defaults["excluded_tags"])
        max_chunks = settings_store.get_int(session, "xlsx_max_chunks")
        engine = ConversationEngine(_base_engine(), session, holder)
        total = len(atts) + 1               # +1: il messaggio
        items, failed_id = [], None
        try:
            # --- pass A: rilevamento su tutto il turno ----------------------
            for i, att in enumerate(atts, start=1):
                failed_id = att.id
                name = att.display_filename or att.filename
                original = (Path(att.original_path) if att.original_path
                            else None)
                if original is None or not original.is_file():
                    raise ValueError(
                        f"{name}: il file non è più disponibile sul server.")
                data, ext = _converted_input(original.read_bytes(), name)
                try:
                    found = _detect(data, ext, engine, defaults,
                                    ctl=_turn_ctl(cancel, on_progress, i, total,
                                                  name, "detect"),
                                    xlsx_max_chunks=max_chunks, ocr=ocr)
                except (ValueError, PdfError) as exc:
                    # il nome del file in testa: l'errore arriva alla UI
                    raise ValueError(f"{name}: {exc}") from exc
                items.append({"att": att, "name": name, "data": data,
                              "ext": ext, "version": conv.mapping_version or 0,
                              **found})
            failed_id = None
            ctl = _turn_ctl(cancel, on_progress, total, total,
                            "Il tuo messaggio", "detect")
            ctl.phases(["analysis"])
            ctl.phase("analysis")
            analysis = engine.analyze(prompt, ctl=ctl,
                                      excluded=defaults["excluded_tags"],
                                      custom_terms=defaults["custom_terms"])
            session.flush()
            version = holder.mapping_version or 0
            mapping = conversation_mapping(session, holder.id,
                                           include_aliases=True, exclude=skip)

            # --- pass B: redazione con il registro finale -------------------
            for i, item in enumerate(items, start=1):
                failed_id = item["att"].id
                ctl = _turn_ctl(cancel, on_progress, i, total, item["name"],
                                "redact")
                if item["file"] is not None and item["version"] == version:
                    # xlsx già redatto nel pass A e registro immutato da
                    # allora: rifare la redazione darebbe gli stessi byte
                    ctl.phases(["redaction"])
                    ctl.phase("redaction")
                    out, report = item["file"], item["report"]
                else:
                    text = item["text"]
                    if text is None:        # xlsx da ri-redigere
                        try:
                            text = extract_xlsx(item["data"], full=True)
                        except Exception:
                            text = ""
                        if item.get("ocr_cache"):
                            # il corpus OCR nel testo: mapping.for_text deve
                            # vedere anche le superfici lette nelle immagini
                            ctext = image_ocr.corpus(item["ocr_cache"])[0]
                            text = (text + "\n" + ctext) if text else ctext
                    out, report = _redact_with(
                        item["data"], item["ext"], mapping.for_text(text),
                        exact_phs=item["exact_phs"], ctl=ctl,
                        ocr_cache=item.get("ocr_cache"),
                        sealed=att_sealed(item["att"]))
                if report.get("residual"):
                    raise ValueError(
                        f"{item['name']}: verifica dei residui fallita, il "
                        "file non viene dichiarato protetto.")
                item["out"], item["report"] = out, report
            failed_id = None

            # --- scrittura: solo ora che tutto il turno è riuscito ---------
            for item in items:
                att = item["att"]
                ext = item["ext"]
                target = (Path(att.original_path).parent
                          / f"{att.id}_protected{ext}")
                target.write_bytes(item["out"])
                # cache OCR persistita (o rimossa: un turno senza OCR non deve
                # lasciare in giro il piano di un tentativo precedente)
                cache_path = _ocr_cache_path(att)
                if item.get("ocr_cache"):
                    cache_path.write_text(
                        json.dumps(item["ocr_cache"], ensure_ascii=False),
                        encoding="utf-8")
                elif cache_path is not None:
                    cache_path.unlink(missing_ok=True)
                model_name = str(Path(str(att.model_filename
                                          or f"allegato{ext}")).with_suffix(ext))
                card = briefing.describe(str(target), model_name,
                                         mimetypes.guess_type(model_name)[0])
                att.protected_path = str(target)
                att.model_filename = model_name
                att.model_briefing_json = json.dumps(card, ensure_ascii=False)
                att.anonymization_report_json = json.dumps(
                    _turn_report(item, mapping), ensure_ascii=False)
                att.anonymization_status = "protected"
                att.mapping_version = version
            model_content = apply_known_surfaces(analysis["anonymized_text"],
                                                 mapping.for_text(prompt))
            session.commit()
            if capture is not None:
                capture["prompt_entities"] = [
                    {"start": e["start"], "end": e["end"], "ph": e["ph"]}
                    for e in analysis["entities"]]
                capture["mapping"] = mapping
                capture["items"] = [
                    {"att_id": item["att"].id, "ext": item["ext"],
                     "text": item.get("text"),
                     "exact_phs": sorted(item["exact_phs"])}
                    for item in items]
            return model_content, version, [a.descriptor() for a in atts]
        except JobCanceled as exc:
            session.rollback()
            raise TurnAnonymizationError(str(exc), canceled=True) from exc
        except Exception as exc:
            session.rollback()
            message = str(exc) or exc.__class__.__name__
            if isinstance(exc, NotImplementedError):
                message = ("Formato non supportato dall'anonimizzazione: "
                           "rimuovi il file oppure usa una chat normale.")
            if failed_id is not None:
                # sessione fresca e best-effort: se anche questa scrittura
                # incontra contesa, alla UI deve arrivare l'errore ORIGINALE,
                # non quello annidato della marcatura
                try:
                    with db.SessionLocal() as s2:
                        att = s2.get(Attachment, failed_id)
                        if att is not None:
                            att.anonymization_status = "failed"
                            att.anonymization_report_json = json.dumps(
                                {"error": message}, ensure_ascii=False)
                            s2.commit()
                except Exception:
                    pass
            raise TurnAnonymizationError(message, failed_id) from exc


def attachment_path(att):
    if att.anonymization_status == "protected" and att.protected_path:
        return Path(att.protected_path)
    if att.original_path:
        return Path(att.original_path)
    # Compatibilità con righe/test creati prima della colonna original_path.
    return DATA_DIR / "chats" / att.conv_id / f"{att.id}_{att.filename}"


def attachment_model_name(att):
    # Anche in modalità raw un nome interno univoco evita che due upload con
    # lo stesso basename si sovrascrivano nel dizionario della sandbox. Per i
    # file protetti è inoltre la barriera contro PII nel filename.
    return att.model_filename or att.display_filename or att.filename


def attachment_model_briefing(att):
    if att.anonymization_status == "protected" and att.model_briefing_json:
        try:
            return json.loads(att.model_briefing_json)
        except ValueError:
            return None
    return att.briefing()


def known_surface_leaks(session, conv_id, text, exclude=None,
                        since_version=None):
    """Valori noti rimasti in un payload dichiarato protetto.

    Si controllano soltanto le superfici che i redattori sanno davvero cercare
    (`_searchable`) e che la conversazione ha chiesto di sostituire (`exclude` =
    categorie lasciate in chiaro): segnalare ciò che si è deciso di non
    sostituire sarebbe un allarme senza rimedio — e in più bloccherebbe ogni
    invio successivo alla disattivazione di una categoria.

    Si applica ai contenuti che partono ADESSO, mai a quelli dei turni
    precedenti: un file già inviato non può essere condannato da una
    superficie diventata nota dopo (sarebbe la riscrittura retroattiva che il
    design vieta, e bloccherebbe la conversazione per sempre).

    `since_version` restringe il controllo a ciò che il registro ha imparato
    DOPO quella versione. È il caso del file di progetto scritto alla versione
    V: le superfici <= V gli sono già state applicate, quindi un disallineamento
    può nascere solo dalle nuove — ed è anche molto più veloce, perché si
    cercano le due o tre superfici nuove invece di tutto il registro.

    Ritorna [(placeholder, superficie)] per poterlo dire all'utente."""
    if not text:
        return []
    entities, aliases = _registry_rows(session, conv_id)
    surfaces, derived = known_surfaces(entities, aliases)
    # stesse superfici che i redattori userebbero su QUESTO testo: segnalare
    # ciò che si è scelto di non sostituire sarebbe un allarme senza rimedio
    excl = set(exclude or ()) | excluded_placeholders(entities)
    usable = set(ReplacementMapping({}, surfaces, derived,
                                    excl).for_text(text).items())
    if since_version is not None:
        # tutto quello che il registro sa ADESSO meno quello che sapeva
        # allora: la differenza tiene conto di fusioni e varianti dedotte
        # esattamente come il resto del modulo
        old, _ = known_surfaces(entities, aliases, max_version=since_version)
        usable -= set(old)
    leaks = {}
    for ph, value in surfaces:
        if (ph, value) not in usable:
            continue
        pat = _pattern_for(ph, value)
        if pat and pat.search(text):
            leaks.setdefault(ph, value)
    return sorted(leaks.items())


# --- Gambe privacy dei tool web --------------------------------------------
# Il modello conosce solo i segnaposto, quindi scrive query coi TAG; la
# deanonimizzazione avviene QUI, dopo che la tool call è stata emessa (la
# query in chiaro esiste solo transitoriamente nel server, la call persistita
# resta coi tag). Il testo che torna dal web rientra dalla STESSA pipeline di
# ingresso di anonymize_turn — registro della conversazione, superfici note
# forzate, riconciliazione per chiave, alias nuovi, categorie escluse — e
# infine passa dal controllo di uscita: nessun valore noto può raggiungere
# il provider.

def _holder_by_scope(session, scope):
    """Il portatore del registro a partire dallo SCOPE id (che per le chat di
    progetto è il progetto): mai un lookup ingenuo per conv_id."""
    holder = session.get(db.Project, scope)
    if holder is None:
        holder = session.get(Conversation, scope)
    return holder


def resolve_placeholders(scope, text):
    """Deanonimizza `text` per l'uscita verso il web: OGNI segnaposto presente
    viene risolto col valore canonico del registro, nessuna whitelist di
    categorie. Ritorna (testo risolto, [tag sconosciuti]): un tag che non
    esiste nella mappa è allucinato e il chiamante deve fermarsi (il valore
    "vero" non esiste, la ricerca sarebbe spazzatura)."""
    if not text:
        return text, []
    with conversation_lock(scope), db.SessionLocal() as session:
        mapping = conversation_mapping(session, scope)
    unknown = []

    def sub(match):
        ph = match.group(0)
        value = mapping.get(ph)
        if value is None:
            unknown.append(ph)
            return ph
        return str(value)

    return _ANY_PH_RE.sub(sub, text), sorted(set(unknown))


def anonymize_web_texts(scope, texts):
    """Ri-anonimizza testi ARRIVATI DAL WEB con la pipeline di ingresso
    completa, sul registro della conversazione: i valori già mappati
    riprendono il TAG esistente, le entità nuove ricevono tag nuovi additivi
    (mapping_version avanza a metà turno: il chiamante rilegge la mappa per
    decodificare i segnaposto appena nati).

    `texts` è una lista di campi (titoli, snippet, URL, contenuto): una sola
    inferenza sul blocco unito, poi ogni campo si ricompone dagli span. In
    coda il controllo di uscita (known_surface_leaks): se un valore noto
    sopravvive anche alla sostituzione diretta (encoding strani), si alza
    TurnAnonymizationError e il risultato si scarta.

    Bloccante (inferenza NER vera): dal codice async, asyncio.to_thread."""
    texts = ["" if t is None else str(t) for t in texts]
    if not any(t.strip() for t in texts):
        return texts
    with conversation_lock(scope), db.SessionLocal() as session:
        holder = _holder_by_scope(session, scope)
        if holder is None:
            raise TurnAnonymizationError("Conversazione non trovata.",
                                         code="conversation_not_found")
        defaults = anon_options(session, holder)
        skip = excluded_groups(defaults["excluded_tags"])
        engine = ConversationEngine(_base_engine(), session, holder)
        # il separatore non è whitespace puro: _value_pattern unisce i token
        # con \s*, e una superficie a due parole non deve poter scavalcare il
        # confine tra un campo e l'altro
        pieces, offsets, pos = [], [], 0
        for t in texts:
            pieces.append(t)
            offsets.append((pos, pos + len(t)))
            pos += len(t)
            pieces.append("\n#\n")
            pos += 3
        corpus = "".join(pieces)
        try:
            analysis = engine.analyze(corpus,
                                      excluded=defaults["excluded_tags"],
                                      custom_terms=defaults["custom_terms"])
            session.commit()
        except Exception:
            session.rollback()
            raise
        mapping = conversation_mapping(session, holder.id,
                                       include_aliases=True,
                                       exclude=skip).for_text(corpus)
        spans = [(e["start"], e["end"], e["ph"])
                 for e in analysis["entities"]]
        out = []
        for (start, end), original in zip(offsets, texts):
            rebuilt, cursor = [], start
            for s, e, ph in spans:
                # gli span che scavalcano il confine di campo (includerebbero
                # il separatore) si saltano: la superficie è comunque appena
                # entrata nel registro e la copre la sostituzione qui sotto
                if s >= cursor and e <= end:
                    rebuilt.append(corpus[cursor:s])
                    rebuilt.append(ph)
                    cursor = e
            rebuilt.append(corpus[cursor:end])
            out.append(apply_known_surfaces("".join(rebuilt), mapping))
        # stesso separatore non-whitespace del corpus: una superficie a più
        # parole non deve poter "emergere" a cavallo di due campi adiacenti
        # (title che finisce in "Mario" + snippet che inizia con "Rossi")
        # facendo scartare un risultato pulito
        leaks = known_surface_leaks(session, holder.id, "\n#\n".join(out),
                                    exclude=skip)
        if leaks:
            # il NER pulisce, l'ispettore certifica: se anche la sostituzione
            # diretta non basta, il risultato non parte
            raise TurnAnonymizationError(
                "Controllo di uscita sul risultato web fallito: contiene "
                "ancora valori noti del registro. Il risultato è stato "
                "scartato.")
        return out


# --- Fusione di entità ----------------------------------------------------

def _token_set(entity):
    return set(_surface_core(entity.label, entity.canonical_value).split(" "))


def merge_suggestions(session, conv_id):
    """Coppie che il resolver ha tenuto separate ma che il contesto suggerisce
    siano la stessa entità: un cognome dentro un nome intero, una ragione
    sociale dentro l'altra. Solo un suggerimento: la fusione automatica su
    nomi parziali e omonimi non è decidibile.

    Le entità ESCLUSE restano fuori, da entrambi i lati: l'utente ha scelto di
    lasciare quel valore in chiaro, e la fusione lo ri-anonimizzerebbe di
    nascosto (l'esclusione sta sul placeholder della sorgente — vedi
    `excluded_placeholders` — che dopo la fusione non si usa più: le sue
    superfici risolverebbero su quello della destinazione, che escluso non è)."""
    entities, _aliases = _registry_rows(session, conv_id)
    live = [e for e in entities
            if not e.merged_into and not getattr(e, "excluded", 0)]
    out = []
    for short in live:
        if short.merge_checked or _label_group(short.label) not in {"FULLNAME",
                                                                    "ORG"}:
            continue
        tokens = _token_set(short)
        if not tokens:
            continue
        for full in live:
            if full.id == short.id or full.label != short.label:
                continue
            other = _token_set(full)
            if tokens < other:          # sottoinsieme proprio
                out.append({"source": short.id, "target": full.id,
                            "source_placeholder": short.placeholder,
                            "target_placeholder": full.placeholder,
                            "source_value": short.canonical_value,
                            "target_value": full.canonical_value})
                break
    return out


def merge_entities(session, conv, source, target):
    """La sorgente viene assorbita: da ora le sue superfici risolvono sul
    placeholder di destinazione. Nulla viene riscritto all'indietro — i
    messaggi già inviati conservano il placeholder che avevano, e la riga
    resta nel registro proprio per continuare a decodificarli."""
    if source.id == target.id or source.conv_id != target.conv_id:
        raise ValueError("Fusione non valida.")
    if target.merged_into:
        raise ValueError("L'entità di destinazione è già stata fusa.")
    conv.mapping_version = (conv.mapping_version or 0) + 1
    source.merged_into = target.id
    source.merge_checked = 1
    existing = {a.original_surface for a in
                session.query(ConversationEntityAlias)
                .filter_by(entity_id=target.id)}
    for alias in (session.query(ConversationEntityAlias)
                  .filter_by(entity_id=source.id)):
        if alias.original_surface in existing:
            continue
        conv.mapping_version = (conv.mapping_version or 0) + 1
        session.add(ConversationEntityAlias(
            entity_id=target.id, original_surface=alias.original_surface,
            normalized_key=alias.normalized_key, source="merge",
            confidence="user", mapping_version=conv.mapping_version))


def merged_placeholders(session, conv_id):
    """placeholder assorbito -> placeholder che lo assorbe, per le sole
    fusioni. Serve a chi conserva un placeholder calcolato PRIMA di una
    fusione (gli span dell'analisi di un turno in anteprima) e deve tradurlo
    in quello che la mappa corrente userebbe."""
    entities, _aliases = _registry_rows(session, conv_id)
    target = _resolve_merges(entities)
    return {e.placeholder: target[e.id].placeholder for e in entities
            if e.merged_into and target[e.id].id != e.id}


def merge_notes(session, conv_id):
    """Blocco per il system prompt con le fusioni fatte dall'utente. I
    messaggi e i file già protetti conservano il segnaposto vecchio (niente
    riscrittura retroattiva), quindi il modello lo incontra ancora e va
    avvisato che indica la stessa entità del segnaposto nuovo. Solo tag,
    mai valori reali. None se non c'è nessuna fusione."""
    pairs = sorted(merged_placeholders(session, conv_id).items())
    if not pairs:
        return None
    return ("Equivalenze tra segnaposto (entità unite dall'utente): i "
            "segnaposto sulla stessa riga indicano la STESSA entità; quello "
            "a sinistra compare in messaggi o allegati più vecchi. Nei tuoi "
            "testi usa UN solo segnaposto per entità e mantienilo per tutta "
            "la risposta. Se dal contesto uno sembra più affidabile "
            "(compare più volte, sta nei documenti più completi, è quello a "
            "cui l'utente si riferisce), preferisci quello. Non vedi i "
            "valori: scegli per contesto, non per etichetta.\n"
            + "\n".join(f"- {src} = {dst}" for src, dst in pairs))


def registry_view(session, conv_id):
    """Il registro come lo vede l'utente: valore, superfici, stato."""
    entities, aliases = _registry_rows(session, conv_id)
    by_entity = {}
    for alias, entity in aliases:
        by_entity.setdefault(entity.id, []).append(alias.original_surface)
    return [{
        "id": e.id,
        "placeholder": e.placeholder,
        "label": e.label,
        "value": e.canonical_value,
        "surfaces": sorted(set(by_entity.get(e.id, [e.canonical_value]))),
        "merged_into": e.merged_into,
        "searchable": _searchable(e.canonical_value),
        "excluded": bool(getattr(e, "excluded", 0)),
    } for e in entities]


_PROTECTED_MARKDOWN = re.compile(
    r"```[\s\S]*?(?:```|\Z)|`[^`\n]*(?:`|$)|<[^>\n]+>|\]\([^\n)]*\)")


def decode_for_display(text, mapping):
    """Decodifica selettiva con span sulla stringa risultante. Codice, HTML e
    destinazioni dei link restano canonici; i valori vengono poi resi dal
    frontend come nodi React testuali, mai interpolati nel parser Markdown."""
    if not text or not mapping:
        return text, []
    protected = [(m.start(), m.end()) for m in _PROTECTED_MARKDOWN.finditer(text)]

    def is_protected(start, end):
        return any(start < b and end > a for a, b in protected)

    pattern = re.compile("|".join(re.escape(ph)
                                  for ph in sorted(mapping, key=len,
                                                   reverse=True)))
    out, spans, pos, out_len = [], [], 0, 0
    for match in pattern.finditer(text):
        if is_protected(match.start(), match.end()):
            continue
        prefix = text[pos:match.start()]
        out.append(prefix)
        out_len += len(prefix)
        value = mapping[match.group(0)]
        start = out_len
        out.append(value)
        out_len += len(value)
        ph = match.group(0)
        m = _PH_RE.match(ph)
        spans.append({"start": start, "end": out_len, "placeholder": ph,
                      "label": m.group(1) if m else "PII"})
        pos = match.end()
    out.append(text[pos:])
    return "".join(out), spans


def protected_placeholders(text, mapping):
    """{placeholder: valore} dei TAG rimasti NON decodificati perché dentro
    codice, HTML o destinazioni di link. Il frontend li usa per offrire la
    copia con i valori reali: nel blocco resta la forma canonica, ma l'utente
    che incolla un CSV in Excel non deve incollarci i segnaposto."""
    if not text or not mapping:
        return {}
    out = {}
    for match in _PROTECTED_MARKDOWN.finditer(text):
        for ph in _ANY_PH_RE.findall(match.group(0)):
            if ph in mapping:
                out[ph] = mapping[ph]
    return out


_FENCE_RE = re.compile(r"^\s{0,3}```")


class StreamDecoder:
    """Decodifica progressiva della risposta, per poterla mostrare mentre
    arriva.

    Si emette solo il prefisso il cui esito non può più cambiare. Due
    insidie: OpenRouter può spezzare un TAG su più chunk
    (`[FULL` + `NAME_` + `1]`) e una regione da NON decodificare — codice,
    destinazione di un link, tag HTML — può essere ancora aperta. Le regioni
    inline non attraversano mai una riga, quindi una riga completa è sempre
    decidibile; dentro una fence non si decodifica nulla e si trattiene solo
    l'ultima riga, che potrebbe essere la chiusura.

    La forma canonica coi TAG resta quella che il chiamante accumula e
    persiste: qui esce solo la versione da mostrare."""

    def __init__(self, mapping):
        self.mapping = mapping or {}
        self.buf = ""
        self.in_fence = False
        self._protected = {}

    def take_protected(self):
        """Valori incontrati dall'ultima lettura, per il renderer live.

        Non si manda al browser l'intero registro (può essere enorme): solo i
        placeholder realmente comparsi in codice/HTML/destinazioni di link.
        """
        out, self._protected = self._protected, {}
        return out

    def feed(self, chunk):
        self.buf += chunk or ""
        return self._drain(final=False)

    def flush(self):
        return self._drain(final=True)

    def _drain(self, final):
        parts, spans, base = [], [], 0
        while True:
            nl = self.buf.find("\n")
            if nl < 0:
                break
            line, self.buf = self.buf[:nl + 1], self.buf[nl + 1:]
            base = self._push(parts, spans, line, base)
        if self.buf:
            cut = len(self.buf) if final else self._safe_cut(self.buf)
            if cut > 0:
                head, self.buf = self.buf[:cut], self.buf[cut:]
                base = self._push(parts, spans, head, base)
        return "".join(parts), spans

    def _push(self, parts, spans, piece, base):
        if _FENCE_RE.match(piece):
            self.in_fence = not self.in_fence
            parts.append(piece)
            return base + len(piece)
        if self.in_fence:
            for ph in _ANY_PH_RE.findall(piece):
                if ph in self.mapping:
                    self._protected[ph] = self.mapping[ph]
            parts.append(piece)
            return base + len(piece)
        text, entities = decode_for_display(piece, self.mapping)
        self._protected.update(protected_placeholders(piece, self.mapping))
        parts.append(text)
        for entity in entities:
            spans.append({**entity, "start": entity["start"] + base,
                          "end": entity["end"] + base})
        return base + len(text)

    def _safe_cut(self, tail):
        """Quanto della riga in corso è già definitivo."""
        if tail.startswith("`"):
            return 0                     # potrebbe diventare una fence
        cut = len(tail)
        for opener, closer in (("[", "]"), ("](", ")"), ("<", ">")):
            i = tail.rfind(opener)
            if i >= 0 and tail.find(closer, i + len(opener)) < 0:
                cut = min(cut, i)
        if tail.count("`") % 2:          # codice inline ancora aperto
            cut = min(cut, tail.rfind("`"))
        return max(cut, 0)


# --- Ripristino dei file prodotti dal modello -------------------------------

_RESTORE_TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html",
                      ".htm", ".yaml", ".yml", ".log", ".py", ".sql", ".ini"}
# L'SVG è testo anche lui, ma ha una pipeline sua (`_restore_svg`): dentro c'è
# un disegno, e sostituire e basta sposterebbe le etichette.
# Testo semplice, ma con una sintassi che i valori possono rompere: un
# "Rossi & Figli" rimesso al posto di [ORG_1] rende il file non più leggibile
# da un parser. Il valore va scritto nella forma che quel formato prevede.
_RESTORE_MARKUP_EXTS = {".xml", ".html", ".htm", ".svg"}
_RESTORE_JSON_EXTS = {".json"}
_RESTORE_OOXML_EXTS = {".docx", ".pptx", ".xlsx"}
# Un grafico salvato in png ha le etichette in pixel: non sono testo e non si
# ripristinano. Si ripristina l'SVG gemello (dove sono testo) e si RIDISEGNA il
# png sull'host — stessa strategia del PDF rigenerato dal .docx sorgente.
_RESTORE_RASTER_EXTS = {".png"}
# Immagini che il modello può consegnare come artifact: se combaciano con la
# versione redatta di un'immagine di input (MediaPool) si consegna l'originale.
_RESTORE_IMAGE_EXTS = _RESTORE_RASTER_EXTS | {
    ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
_ANY_PH_RE = PLACEHOLDER_RE


# --- Marcature della redazione da togliere al ripristino ---------------------
#
# I redattori evidenziano i placeholder (w:highlight giallo nei docx,
# a:highlight FFFF00 nei pptx, fill giallo nelle celle xlsx): è il segno
# "qui c'era una PII" della copia protetta. Negli artifact quel segno non deve
# arrivare all'utente: il run che contiene un placeholder in via di ripristino
# perde il SOLO giallo nostro. Un highlight di un altro colore è dell'utente
# e resta (i redattori non lo sostituiscono: docx._make_ph_run/pptx._make_run
# aggiungono il giallo solo dove non c'era niente). L'unico caso perso è un
# giallo dell'utente esattamente sopra una PII, indistinguibile dal nostro.
_WRUN_RE = re.compile(r"<w:r(?:>|\s[^>]*>).*?</w:r>", re.S)
_WHL_RE = re.compile(
    r'\s*<w:highlight\s+w:val="yellow"\s*(?:/>|>\s*</w:highlight>)')
_WT_RE = re.compile(r"<w:t(?:>|\s[^>]*>)(.*?)</w:t>", re.S)
_ARUN_RE = re.compile(r"<a:r(?:>|\s[^>]*>).*?</a:r>", re.S)
_AHL_RE = re.compile(
    r'\s*<a:highlight>\s*<a:srgbClr\s+val="FFFF00"\s*'
    r'(?:/>|>\s*</a:srgbClr>)\s*</a:highlight>')
_AT_RE = re.compile(r"<a:t(?:>|\s[^>]*>)(.*?)</a:t>", re.S)


def _strip_run_marks(text, mapping, ext):
    """Toglie l'evidenziazione GIALLA della redazione dai run il cui testo
    contiene un placeholder in via di ripristino. Lavora sulla stringa XML
    della part, senza reparse: è un ritocco puntuale prima della solita
    sostituzione testuale. I placeholder sconosciuti tengono il loro giallo
    (il valore resta un segnaposto, il segno è ancora vero)."""
    run_re, hl_re, t_re = ((_WRUN_RE, _WHL_RE, _WT_RE) if ext == ".docx"
                           else (_ARUN_RE, _AHL_RE, _AT_RE))

    def strip(m):
        run = m.group(0)
        if hl_re.search(run) is None:
            return run
        content = "".join(t_re.findall(run))
        if any(ph in mapping for ph in _ANY_PH_RE.findall(content)):
            return hl_re.sub("", run, count=1)
        return run

    return run_re.sub(strip, text)


# --- Immagini ripristinabili (MediaPool) --------------------------------------

class MediaPool:
    """Le immagini che il ripristino sa sostituire negli artifact: la versione
    REDATTA (quella che il modello vede e ricopia) accoppiata a quella da
    consegnare all'utente.

    Due sorgenti: i grafici del turno già ripristinati dall'SVG (add_pair,
    era il dizionario `charts`) e le immagini degli INPUT protetti della
    conversazione (load_inputs), accoppiate agli originali per posizione —
    stessa part nell'OOXML, stesso rettangolo di pagina nel PDF — cioè tra
    due file congelati sull'host, mai per aspetto.

    Il lookup è per sha1 (il round-trip della sandbox conserva i byte delle
    part) con ripiego sulla firma 16x16 di image_ocr per le copie ricompresse;
    nel ripiego il formato si adatta a quello della part da sostituire."""

    def __init__(self):
        self._exact = {}          # sha1 esadecimale -> bytes da consegnare
        self._sigs = []           # (firma 16x16, bytes, ext dell'originale)
        self._loaded = False

    def add_pair(self, redacted_bytes, restored_bytes):
        self._exact[hashlib.sha1(redacted_bytes).hexdigest()] = restored_bytes

    def _add_input(self, redacted_bytes, original_bytes, ext):
        self._exact.setdefault(hashlib.sha1(redacted_bytes).hexdigest(),
                               original_bytes)
        sig = image_ocr._img_sig(redacted_bytes)
        if sig is not None:
            self._sigs.append((sig, original_bytes, ext))

    def lookup(self, data, ext=None):
        """I bytes da consegnare al posto di `data`, o None. `ext` è il
        formato del posto che li ospiterà (l'estensione della part): nel
        ripiego per firma l'originale viene riscritto in quel formato."""
        hit = self._exact.get(hashlib.sha1(data).hexdigest())
        if hit is not None:
            return hit
        if not self._sigs:
            return None
        sig = image_ocr._img_sig(data)
        if sig is None:
            return None
        idx = image_ocr._best_match(sig, range(len(self._sigs)),
                                    lambda i: self._sigs[i][0])
        if idx is None:
            return None
        original, oext = self._sigs[idx][1], self._sigs[idx][2]
        return _adapt_image(original, oext, ext)

    def load_inputs(self, session, conv):
        """Accoppia le immagini redatte degli input protetti della
        conversazione (allegati + file di progetto confermati) ai loro
        originali. Una volta sola per turno, e solo se servono artifact."""
        if self._loaded:
            return
        self._loaded = True
        for orig, prot, ext, has_cache, sealed in _protected_inputs(session,
                                                                    conv):
            if sealed:
                # aree sigillate: contenuto distrutto APPOSTA dall'utente,
                # non deve poter tornare attraverso lo scambio immagini
                continue
            try:
                _pair_input_media(self, orig, prot, ext, has_cache)
            except Exception as e:      # un input illeggibile non ferma il turno
                log.warning(f"accoppiamento media di {prot.name} "
                            f"fallito: {e!r}")


def _adapt_image(data, from_ext, to_ext):
    """I bytes dell'originale nel formato della part che li ospiterà.
    Stesso formato (o nessuna richiesta): passaggio diretto."""
    same = {".jpg": ".jpeg", ".tif": ".tiff"}
    f = same.get(from_ext, from_ext or "")
    t = same.get(to_ext, to_ext or "")
    if not t or f == t:
        return data
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            buf = io.BytesIO()
            if t == ".jpeg":
                im.convert("RGB").save(buf, format="JPEG", quality=95)
            elif t == ".gif":
                im.convert("P", palette=1).save(buf, format="GIF")
            elif t == ".tiff":
                im.save(buf, format="TIFF")
            elif t == ".bmp":
                im.save(buf, format="BMP")
            else:
                im.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        return None                 # meglio nessuno scambio che una part rotta


def _protected_inputs(session, conv):
    """(originale, protetto, ext, ha_cache_ocr, sigillato) per ogni input
    protetto che la sandbox della conversazione vede."""
    atts = (session.query(Attachment)
            .filter_by(conv_id=conv.id, direction="in",
                       anonymization_status="protected"))
    for att in atts:
        if not (att.original_path and att.protected_path):
            continue
        orig, prot = Path(att.original_path), Path(att.protected_path)
        ext = prot.suffix.lower()
        if orig.suffix.lower() != ext:
            # formato legacy convertito all'ingresso: l'originale non è la
            # stessa superficie del protetto, nessun accoppiamento possibile
            continue
        cache = _ocr_cache_path(att)
        yield (orig, prot, ext, cache is not None and cache.is_file(),
               bool(att_sealed(att)))
    pid = getattr(conv, "project_id", None)
    if pid:
        from . import project_files as pf_mod       # import circolare: lazy
        project = session.get(db.Project, pid)
        if project is None or not project.anonymized:
            return
        for pf in pf_mod.confirmed_files(session, pid):
            try:
                sealed = bool(json.loads(pf.sealed_json or "[]"))
            except ValueError:
                sealed = True                        # illeggibile = prudenza
            yield (pf_mod.original_path(pf), pf_mod.protected_path(pf),
                   pf_mod.file_ext(pf), pf_mod.ocr_cache_path(pf).is_file(),
                   sealed)


def _pair_input_media(pool, orig, prot, ext, has_cache):
    """Le coppie (redatto, originale) di UN input. Le immagini vengono
    riscritte solo dall'OCR: senza cache non c'è niente da accoppiare."""
    if not has_cache or not orig.is_file() or not prot.is_file():
        return
    if ext in _RESTORE_OOXML_EXTS:
        with zipfile.ZipFile(io.BytesIO(prot.read_bytes())) as zp, \
                zipfile.ZipFile(io.BytesIO(orig.read_bytes())) as zo:
            onames = set(zo.namelist())
            for name in zp.namelist():
                if "/media/" not in name.lower() or name not in onames:
                    continue
                p, o = zp.read(name), zo.read(name)
                if p != o:
                    pool._add_input(p, o, Path(name).suffix.lower())
    elif ext == ".pdf":
        _pair_pdf_media(pool, orig.read_bytes(), prot.read_bytes())
    elif ext in _IMAGE_EXTS:
        p, o = prot.read_bytes(), orig.read_bytes()
        if p != o:
            pool._add_input(p, o, ext)


def _rect_sig(r):
    return tuple(round(v, 1) for v in (r.x0, r.y0, r.x1, r.y1))


@mupdf_serialized
def _pair_pdf_media(pool, orig_bytes, prot_bytes):
    """Accoppia per POSIZIONE le immagini del PDF protetto a quelle
    dell'originale: la redazione non sposta nulla, quindi stesso rettangolo
    sulla stessa pagina = stessa immagine (l'aspetto invece qui non serve a
    niente: la versione redatta ha i box gialli e non somiglia più alla
    sua originale). Una pagina vettoriale rasterizzata dalla redazione non
    ha una gemella nell'originale: l'originale è il RENDER della pagina."""
    with fitz.open(stream=orig_bytes, filetype="pdf") as od, \
            fitz.open(stream=prot_bytes, filetype="pdf") as pd:
        if od.page_count != pd.page_count:
            return
        done = set()
        for pno in range(pd.page_count):
            ppage, opage = pd[pno], od[pno]
            orects = {}
            for img in opage.get_images(full=True):
                for r in opage.get_image_rects(img[0]):
                    orects[_rect_sig(r)] = img[0]
            for img in ppage.get_images(full=True):
                xref = img[0]
                if xref in done:
                    continue
                rects = ppage.get_image_rects(xref)
                if not rects:
                    continue
                try:
                    pdata = pd.extract_image(xref)["image"]
                except Exception:
                    continue
                done.add(xref)
                paired = False
                for r in rects:
                    oxref = orects.get(_rect_sig(r))
                    if oxref is None:
                        continue
                    try:
                        info = od.extract_image(oxref)
                        odata, oext = image_ocr._stencil_positive(od, oxref,
                                                                  info)
                    except Exception:
                        break
                    if odata != pdata:
                        pool._add_input(pdata, odata, "." + (oext or "png"))
                    paired = True
                    break
                if not paired and _covers_page(rects[0], ppage.rect):
                    if opage.rotation:
                        opage.remove_rotation()
                    png = opage.get_pixmap(
                        dpi=image_ocr.PAGE_DPI).tobytes("png")
                    pool._add_input(pdata, png, ".png")


def _covers_page(rect, page_rect):
    if page_rect.width <= 0 or page_rect.height <= 0:
        return False
    inter = rect & page_rect
    return inter.get_area() >= 0.9 * page_rect.get_area()


def _restore_string(text, mapping, escape=False):
    """Sostituisce i TAG noti; quelli sconosciuti restano com'erano e non
    vengono contati (il conteggio dice quanto è stato davvero ripristinato).
    Una sola implementazione, condivisa con la pipeline PDF."""
    return sub_placeholders(text, mapping, escape=escape)


def _mapping_for_syntax(ext, mapping):
    """La mappa coi valori scritti nella forma che il formato di destinazione
    richiede. Si scappa il VALORE, non il testo intorno: il file prodotto dal
    modello è già scritto bene ed è solo il valore reale che vi entra ora a
    poter contenere `&`, `<` o una virgoletta."""
    if ext in _RESTORE_MARKUP_EXTS:
        return {ph: (str(value).replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;").replace('"', "&quot;"))
                for ph, value in mapping.items()}
    if ext in _RESTORE_JSON_EXTS:
        # json.dumps di una stringa dà il letterale completo: le virgolette
        # esterne sono quelle che il file ha già intorno al segnaposto
        return {ph: json.dumps(str(value), ensure_ascii=False)[1:-1]
                for ph, value in mapping.items()}
    return mapping


def restore_filename(name, mapping):
    """Come restore_artifact, ma sul nome del file.

    Nei nomi il modello scrive spesso il TAG senza parentesi quadre
    ("sollecito_FULLNAME_1.docx"): le graffe non stanno bene in un filename e
    le toglie da solo. Si accettano quindi entrambe le forme. Gli spazi dei
    valori diventano underscore, così il nome resta maneggevole."""
    if not name or not mapping:
        return name
    out, _n = _restore_string(name, mapping)
    bare = {ph[1:-1]: value for ph, value in mapping.items()
            if ph.startswith("[") and ph.endswith("]")}
    if bare:
        # l'underscore è il separatore tipico dei nomi file, quindi a
        # sinistra si ammette: "sollecito_FULLNAME_1.docx" è il TAG
        pattern = re.compile(r"(?<![A-Za-z0-9])(" +
                             "|".join(re.escape(k) for k in
                                      sorted(bare, key=len, reverse=True)) +
                             r")(?![A-Za-z0-9_])")
        out = pattern.sub(lambda m: bare[m.group(1)], out)
    return re.sub(r"\s+", "_", out)


@mupdf_serialized
def _pdf_images(pdf_bytes):
    """Immagini del PDF prodotto (per xref: un logo ripetuto è una sola). Un
    segnaposto disegnato DENTRO un raster non è testo: non è ripristinabile e
    non lo vede nemmeno la verifica dei residui, quindi va dichiarato."""
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            return len({img[0] for page in doc
                        for img in page.get_images(full=True)})
    except Exception:
        return 0


# Il rasterizzatore (MuPDF) conosce i font di base, non "DejaVu Sans" che
# matplotlib scrive nell'SVG: senza questa normalizzazione un grafico sans
# tornerebbe con le etichette in grazie. Si tocca solo la copia che si
# ridisegna, mai l'SVG consegnato all'utente (lì il browser ha i suoi
# fallback).
# l'elenco dei font contiene apostrofi ('DejaVu Sans', ...): si taglia solo su
# `;` e sulla virgoletta che chiude l'attributo style
_SVG_FONT_STYLE_RE = re.compile(r"font-family:\s*[^;\"]+")
_SVG_FONT_ATTR_RE = re.compile(r'font-family="[^"]*"')
_SERIF_RE = re.compile(r"serif|times|georgia|roman|garamond|book", re.I)
_MONO_RE = re.compile(r"mono|courier|consol", re.I)
_CHART_DPI = 150.0


def _generic_family(names):
    """La famiglia generica equivalente all'elenco di font dell'SVG."""
    first = names.split(",")[0].strip().strip("'\"")
    if _MONO_RE.search(first):
        return "monospace"
    if _SERIF_RE.search(first) and not re.search(r"sans", first, re.I):
        return "serif"
    return "sans-serif"


def _svg_host_fonts(svg):
    svg = _SVG_FONT_STYLE_RE.sub(
        lambda m: "font-family: " + _generic_family(m.group(0).split(":", 1)[1]),
        svg)
    return _SVG_FONT_ATTR_RE.sub(
        lambda m: 'font-family="%s"' % _generic_family(m.group(0).split("=", 1)[1]),
        svg)


# --- Il grafico non rifluisce: si allarga la finestra, non il testo ---------
#
# Un valore reale è quasi sempre più largo del segnaposto che sostituisce, e
# in un SVG di matplotlib ogni etichetta ha una posizione fissa: un nome lungo
# esce dal riquadro e il rasterizzatore lo taglia: in un grafico a barre
# orizzontali le ragioni sociali finiscono mozzate a metà.
# Rimpicciolire il testo — la via del PDF ricucito — qui non serve: un SVG ha
# una FINESTRA (`viewBox`), e allargarla di quel tanto che serve rimette tutto
# dentro senza toccare né il disegno né i corpi. Si conserva il rapporto
# d'aspetto, così il png ridisegnato ha esattamente i pixel di quello che
# sostituisce (l'immagine scambiata dentro un documento non si deforma).
_SVG_ROOT_RE = re.compile(r"<svg\b[^>]*>", re.I)
_SVG_TEXT_RE = re.compile(r"<text\b([^>]*)>(.*?)</text>", re.S | re.I)
_NUM_RE = r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?"
_ROTATE_RE = re.compile(r"rotate\(\s*(%s)(?:[ ,]+(%s)[ ,]+(%s))?\s*\)"
                        % (_NUM_RE, _NUM_RE, _NUM_RE))
_TRANSLATE_RE = re.compile(r"translate\(\s*(%s)(?:[ ,]+(%s))?\s*\)"
                           % (_NUM_RE, _NUM_RE))
_X_ATTR_RE = re.compile(r'\bx\s*=\s*"')
_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
             "&apos;": "'"}
_SVG_PAD = 0.01         # margine di cortesia: il testo non tocca il bordo
_WIDTH_SLACK = 1.05     # le metriche sono stimate su Helvetica, non misurate


@lru_cache(maxsize=1)
def _metrics_font():
    """Il font con cui si STIMA la larghezza del testo. Il rasterizzatore
    disegna con le famiglie generiche di `_svg_host_fonts`, cioè con i font di
    base di MuPDF: le metriche di Helvetica sono quelle vere, non un'analogia."""
    return fitz.Font("helv")


def _svg_attr(chunk, name):
    """Il valore di un attributo o della proprietà omonima dentro `style`:
    matplotlib scrive font-size e text-anchor nello style, x e y come
    attributi."""
    m = re.search(r'\b%s\s*=\s*"([^"]*)"' % name, chunk, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\b%s\s*:\s*([^;\"]+)" % name, chunk, re.I)
    return m.group(1).strip() if m else None


def _svg_num(value, default=0.0):
    if not value:
        return default
    m = re.search(_NUM_RE, value)
    return float(m.group(0)) if m else default


def _svg_unescape(text):
    """Il testo come lo si legge sullo schermo: le entità XML valgono un
    carattere, non cinque (misurare "&amp;" al posto di "&" gonfierebbe ogni
    ragione sociale con la e commerciale)."""
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    return re.sub(r"&#?\w+;", " ", text)


def _text_width(text, size):
    return _metrics_font().text_length(text, size) * _WIDTH_SLACK


def _text_origin(attrs):
    """Dove il renderer appoggia il testo: gli attributi x/y, oppure il
    `translate` del transform. matplotlib usa il secondo per il testo su più
    righe, che impagina riga per riga da solo (vedi `_realign_svg`)."""
    trans = _TRANSLATE_RE.search(attrs)
    if trans is not None and not _X_ATTR_RE.search(attrs):
        return float(trans.group(1)), float(trans.group(2) or 0.0), True
    return _svg_num(_svg_attr(attrs, "x")), _svg_num(_svg_attr(attrs, "y")), False


def _text_box(attrs, text):
    """Il rettangolo occupato da un nodo <text>, rotazione compresa."""
    text = _svg_unescape(text).strip()
    if not text:
        return None
    size = _svg_num(_svg_attr(attrs, "font-size"), 10.0)
    x, y, translated = _text_origin(attrs)
    width = _text_width(text, size)
    anchor = (_svg_attr(attrs, "text-anchor") or "start").lower()
    x0 = (x - width if anchor == "end" else
          x - width / 2 if anchor == "middle" else x)
    box = (x0, y - size, x0 + width, y + size * 0.35)
    m = _ROTATE_RE.search(_svg_attr(attrs, "transform") or "")
    if m is None or abs(float(m.group(1))) < 0.01:
        return box
    angle = math.radians(float(m.group(1)))
    # una rotate senza centro gira intorno all'origine del sistema corrente:
    # dopo una translate è il punto di appoggio del testo, non (0,0)
    default = (x, y) if translated else (0.0, 0.0)
    cx = float(m.group(2)) if m.group(2) else default[0]
    cy = float(m.group(3)) if m.group(3) else default[1]
    cos, sin = math.cos(angle), math.sin(angle)
    corners = [((px - cx) * cos - (py - cy) * sin + cx,
                (px - cx) * sin + (py - cy) * cos + cy)
               for px in (box[0], box[2]) for py in (box[1], box[3])]
    return (min(p[0] for p in corners), min(p[1] for p in corners),
            max(p[0] for p in corners), max(p[1] for p in corners))


# --- Le righe impaginate a mano da matplotlib -------------------------------
#
# Un'etichetta su PIÙ RIGHE non viene scritta con x/y + text-anchor: matplotlib
# se la impagina da solo e piazza ogni riga con `transform="translate(x y)"`,
# senza ancora. Per il renderer quello è un `text-anchor: start`, cioè tiene
# fermo il bordo SINISTRO: appena il segnaposto diventa un valore più largo la
# riga cresce verso destra e finisce dentro il grafico: le etichette dell'asse
# y scavalcano le barre, con la tacca a mezzo del testo.
#
# L'allineamento vero non si indovina, si LEGGE dalla geometria originale: le
# righe di uno stesso blocco condividono il bordo sinistro, quello destro o il
# centro, e basta vedere quale dei tre è costante: su un'etichetta di due
# righe gli scarti tipici sono decine di punti su due bordi e pochi punti sul
# terzo, che è quello vero (per l'asse y matplotlib allinea a destra). Il
# riallineamento sposta la riga cambiata di -Δlarghezza (destra), -Δ/2
# (centro) o 0 (sinistra).
_BLOCK_LINE_GAP = 2.5   # righe dello stesso blocco: salto < 2.5 corpi


def _text_blocks(svg):
    """I nodi <text> posizionati con translate, raggruppati per blocco. Le
    righe di un blocco sono consecutive nel documento e scendono di poco più
    di un corpo; fra un'etichetta e la successiva il salto è un altro."""
    blocks, block = [], []
    for m in _SVG_TEXT_RE.finditer(svg):
        attrs, text = m.group(1), m.group(2)
        trans = _TRANSLATE_RE.search(attrs)
        if trans is None or _X_ATTR_RE.search(attrs):
            continue
        size = _svg_num(_svg_attr(attrs, "font-size"), 10.0)
        node = {"start": m.start(1) + trans.start(),
                "end": m.start(1) + trans.end(),
                "x": float(trans.group(1)), "y": float(trans.group(2) or 0.0),
                "size": size, "text": _svg_unescape(text).strip()}
        if block and 0 < node["y"] - block[-1]["y"] <= _BLOCK_LINE_GAP * size:
            block.append(node)
        else:
            if block:
                blocks.append(block)
            block = [node]
    if block:
        blocks.append(block)
    return blocks


def _block_factor(block):
    """Quanto va spostata a sinistra una riga che si allarga di 1: 1 se il
    blocco è allineato a destra, 0.5 se centrato, 0 se a sinistra. A parità
    vince lo 0, cioè nessuno spostamento: un blocco di una riga sola non dà
    nessun indizio sull'allineamento e resta dov'è."""
    edges = [(n["x"], _text_width(n["text"], n["size"])) for n in block]
    candidates = []
    for factor in (0.0, 0.5, 1.0):
        values = [x + width * factor for x, width in edges]
        candidates.append((max(values) - min(values), factor))
    return min(candidates)[1]


def _realign_svg(svg, mapping):
    """Rimette le righe impaginate a mano dove stavano, dopo che i segnaposto
    sono diventati valori. Lavora sull'SVG ORIGINALE (serve la larghezza di
    prima e quella di dopo) e ne restituisce uno con le sole `translate`
    corrette: la sostituzione del testo è un passaggio successivo."""
    values = _mapping_for_syntax(".svg", mapping)
    edits = []
    for block in _text_blocks(svg):
        factor = _block_factor(block)
        if not factor:
            continue
        for node in block:
            after, changed = sub_placeholders(node["text"], values)
            if not changed:
                continue
            dx = (_text_width(node["text"], node["size"])
                  - _text_width(_svg_unescape(after), node["size"])) * factor
            if abs(dx) > 0.01:
                edits.append((node["start"], node["end"],
                              "translate(%.6f %.6f)" % (node["x"] + dx,
                                                        node["y"])))
    for start, end, text in sorted(edits, reverse=True):
        svg = svg[:start] + text + svg[end:]
    return svg


def _restore_svg(svg, mapping):
    """Il ripristino completo di un SVG: righe riallineate, segnaposto
    sostituiti, finestra allargata su quel che ancora sporge."""
    out, n = _restore_string(_realign_svg(svg, mapping),
                             _mapping_for_syntax(".svg", mapping))
    return (_fit_canvas(out), n) if n else (svg, 0)


def _fit_canvas(svg):
    """Allarga il `viewBox` finché nessuna etichetta resta fuori. Idempotente
    (a finestra già capiente non cambia niente) e prudente: a ogni intoppo —
    radice illeggibile, numeri strani — l'SVG torna com'era."""
    root = _SVG_ROOT_RE.search(svg)
    if root is None:
        return svg
    head = root.group(0)
    view = _svg_attr(head, "viewBox")
    box = ([float(n) for n in re.findall(_NUM_RE, view)[:4]] if view else
           [0.0, 0.0, _svg_num(_svg_attr(head, "width")),
            _svg_num(_svg_attr(head, "height"))])
    if len(box) != 4 or box[2] <= 0 or box[3] <= 0:
        return svg
    x0, y0 = box[0], box[1]
    x1, y1 = x0 + box[2], y0 + box[3]
    ex0, ey0, ex1, ey1 = x0, y0, x1, y1
    for attrs, text in _SVG_TEXT_RE.findall(svg):
        found = _text_box(attrs, text)
        if found is not None:
            ex0, ey0 = min(ex0, found[0]), min(ey0, found[1])
            ex1, ey1 = max(ex1, found[2]), max(ey1, found[3])
    if (ex0, ey0, ex1, ey1) == (x0, y0, x1, y1):
        return svg
    pad = max(ex1 - ex0, ey1 - ey0) * _SVG_PAD
    ex0, ey0, ex1, ey1 = ex0 - pad, ey0 - pad, ex1 + pad, ey1 + pad
    ratio = box[2] / box[3]
    width, height = ex1 - ex0, ey1 - ey0
    if width / height < ratio:
        width = height * ratio
    else:
        height = width / ratio
    cx, cy = (ex0 + ex1) / 2, (ey0 + ey1) / 2
    view = "%.3f %.3f %.3f %.3f" % (cx - width / 2, cy - height / 2,
                                    width, height)
    head = (re.sub(r'viewBox\s*=\s*"[^"]*"', 'viewBox="%s"' % view, head,
                   count=1) if _svg_attr(head, "viewBox")
            else head[:-1] + ' viewBox="%s">' % view)
    return svg[:root.start()] + head + svg[root.end():]


@mupdf_serialized
def _rasterize_svg(svg, width_px=None):
    """SVG -> png, alla larghezza in pixel del raster che sostituisce (così
    l'immagine scambiata dentro un documento non cambia dimensione)."""
    with fitz.open(stream=_fit_canvas(_svg_host_fonts(svg)).encode("utf-8"),
                   filetype="svg") as doc:
        page = doc[0]
        zoom = (width_px / page.rect.width if width_px and page.rect.width
                else _CHART_DPI / 72.0)
        return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                               alpha=False).tobytes("png")


def _restore_chart(png, svg_path, mapping):
    """Il grafico ridisegnato dall'SVG sorgente, coi valori reali.

    Ritorna la stessa tupla di `restore_artifact`, o None se l'SVG non è
    leggibile o il rasterizzatore non ce la fa: il chiamante lascia allora il
    png com'è e lo dichiara non ripristinabile."""
    try:
        svg = svg_path.read_text("utf-8")
    except (OSError, ValueError):
        return None
    out, n = _restore_svg(svg, mapping)
    if not n:
        # niente segnaposto nel grafico: ridisegnarlo introdurrebbe solo le
        # differenze del rasterizzatore, meglio consegnare il png originale
        return png, 0, [], {}
    try:
        data = _rasterize_svg(out, _png_width(png))
    except Exception:                   # SVG rotto, MuPDF senza handler...
        return None
    return data, n, sorted(set(_ANY_PH_RE.findall(out))), {"chart": True}


@mupdf_serialized
def _png_width(data):
    try:
        return fitz.Pixmap(data).width
    except Exception:
        return None


def _swap_media(item_name, data, images):
    """L'immagine ripristinata al posto di quella redatta o coi segnaposto,
    dentro un OOXML: i grafici del turno e le immagini degli input protetti
    (vedi MediaPool.lookup — sha1 esatto, poi firma 16x16)."""
    if images is None or "/media/" not in item_name.lower():
        return None
    return images.lookup(data, Path(item_name).suffix.lower())


def _rerender_pdf(source, mapping, images=None):
    """PDF RIGENERATO dal documento sorgente, invece che ricucito glifo per
    glifo.

    Un PDF non rifluisce: `restore_pdf` può solo rimpicciolire un valore più
    largo del segnaposto che sostituisce. Il .docx da cui il modello ha
    ricavato il PDF (openrouter/chat.py: python-docx + soffice) invece un
    layout non ce l'ha — lo calcola il renderer — quindi qui si ripristina il
    SORGENTE con la solita sostituzione testuale e lo si riconverte: a
    impaginare sulle lunghezze vere ci pensa LibreOffice, e il corpo del testo
    non si tocca.

    La conversione gira sull'HOST, mai nella sandbox: scrivere il documento
    ripristinato in /workspace/outputs (che è un mount condiviso col
    container) metterebbe i valori reali dove il modello esegue codice, e al
    turno dopo tornerebbero al modello dentro il risultato di un'esecuzione.

    Ritorna None a ogni intoppo — LibreOffice assente, conversione fallita,
    segnaposto noti ancora leggibili nell'output: il chiamante torna al
    ripristino glifo per glifo, che non ha prerequisiti."""
    data, n, _left, extra_src = restore_artifact(source, mapping, images=images)
    if data is None:
        return None
    try:
        pdf = to_pdf(data, suffix=source.suffix)
    except Exception:               # LibreOffice assente, timeout, sorgente rotto
        return None
    left = _left_placeholders(pdf)
    if any(ph in mapping for ph in left):
        return None                 # non ha fatto il suo lavoro: meglio l'altro
    extra = {"rerendered": True}
    if extra_src.get("images_swapped"):
        # i grafici del .docx sono già quelli ripristinati: il PDF li eredita
        extra["images_swapped"] = extra_src["images_swapped"]
    n_images = _pdf_images(pdf)
    if n_images:
        extra["images"] = n_images
    return pdf, n, left, extra


def restore_artifact(path, mapping, source=None, images=None):
    """Rimette i valori veri nei file che il modello ha prodotto a partire da
    input protetti.

    I documenti generati dalla sandbox sono semplici — ogni stringa è un run
    intero, nessun TAG spezzato tra due porzioni di XML — quindi per testo e
    OOXML basta la sostituzione testuale. Il PDF ha una pipeline sua
    (`engine.pdf_export.restore_pdf`): il testo di una pagina non è una
    stringa ma un content stream, e il valore va rimosso e riscritto glifo per
    glifo dov'era il segnaposto.

    `source` è il file da cui questo artifact è stato generato, quando c'è:
    il documento OOXML per un PDF (`_rerender_pdf`, l'unico modo di
    reimpaginare invece di rimpicciolire) e l'SVG per un grafico png
    (`_restore_chart`, l'unico modo di rimettere del testo dentro dei pixel).

    `images` è il MediaPool del turno (grafici ripristinati + immagini degli
    input protetti): le immagini che combaciano — dentro un OOXML o un PDF,
    o consegnate come file a sé — vengono scambiate con la versione da
    consegnare all'utente.

    Ritorna (bytes | None, n_ripristinati, [TAG rimasti], report_extra)."""
    path = Path(path)
    ext = path.suffix.lower()
    if not mapping or not path.is_file():
        return None, 0, [], {}
    if ext in _RESTORE_IMAGE_EXTS:
        data = path.read_bytes()
        if images is not None:
            hit = images.lookup(data, ext)
            if hit is not None:
                # copia (anche ricompressa) di un'immagine di input redatta:
                # all'utente va l'originale
                return hit, 0, [], {"image_restored": True}
        if ext not in _RESTORE_RASTER_EXTS:
            return None, 0, [], {}
        source = Path(source) if source else None
        if (source is None or source.suffix.lower() != ".svg"
                or not source.is_file()):
            return None, 0, [], {}      # senza SVG sorgente non c'è rimedio
        out = _restore_chart(data, source, mapping)
        return out if out is not None else (None, 0, [], {})
    if ext == ".svg":
        try:
            svg = path.read_text("utf-8")
        except (OSError, ValueError):
            return None, 0, [], {}
        out, n = _restore_svg(svg, mapping)
        return (out.encode("utf-8"), n,
                sorted(set(_ANY_PH_RE.findall(out))), {})
    if ext in _RESTORE_TEXT_EXTS:
        raw = path.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")   # va preservato, non aggiunto
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return None, 0, [], {}
        out, n = _restore_string(text, _mapping_for_syntax(ext, mapping))
        left = sorted(set(_ANY_PH_RE.findall(out)))
        return out.encode("utf-8-sig" if bom else "utf-8"), n, left, {}
    if ext == ".pdf":
        source = Path(source) if source else None
        if source is not None and source.suffix.lower() in _RESTORE_OOXML_EXTS:
            out = _rerender_pdf(source, mapping, images)
            if out is not None:
                return out
        try:
            data, report = restore_pdf(path.read_bytes(), mapping,
                                       media=images)
        except PdfError:
            return None, 0, [], {}
        extra = {key: report[key] for key in
                 ("by_placeholder", "lost", "shrunk", "degraded", "images",
                  "images_swapped", "annots", "widgets", "toc", "metadata")
                 if report.get(key)}
        return data, report["restored"], report["remaining"], extra
    if ext not in _RESTORE_OOXML_EXTS:
        return None, 0, [], {}
    try:
        src = zipfile.ZipFile(io.BytesIO(path.read_bytes()))
    except zipfile.BadZipFile:
        return None, 0, [], {}
    buf = io.BytesIO()
    total, swapped, left = 0, 0, set()
    styled = {}
    with src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        if ext == ".xlsx":
            # il giallo delle celle coi placeholder torna lo stile originale
            # (auto-descritto nel bgColor del fill: vedi engine/xlsx.py)
            parts = {i.filename: src.read(i.filename) for i in src.infolist()}
            styled = restore_cell_styles(parts, set(mapping))
            parts.update(styled)
            from .engine.xlsx_names import (restore_names, restore_formula_literals,
                                            WorkbookNameError)
            try:
                named, restored_names = restore_names(parts, mapping)
            except WorkbookNameError as exc:
                return None, 0, [], {"error": str(exc)}
            parts.update(named)
            literals, restored_literals = restore_formula_literals(parts, mapping)
            styled.update(named)
            styled.update(literals)
            total += restored_names + restored_literals
        for item in src.infolist():
            data = styled.get(item.filename) or src.read(item.filename)
            if item.filename.lower().endswith((".xml", ".rels")):
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    pass
                else:
                    if ext in (".docx", ".pptx"):
                        # via il giallo dai run coi placeholder, PRIMA della
                        # sostituzione (dopo, il placeholder non c'è più)
                        text = _strip_run_marks(text, mapping, ext)
                    text, n = _restore_string(text, mapping, escape=True)
                    total += n
                    left.update(_ANY_PH_RE.findall(text))
                    data = text.encode("utf-8")
            else:
                new = _swap_media(item.filename, data, images)
                if new is not None:
                    data = new
                    swapped += 1
            out.writestr(item, data)
    return (buf.getvalue(), total, sorted(left),
            {"images_swapped": swapped} if swapped else {})

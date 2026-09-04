# Derivato da rizzo-pii `src/app/pdf_export.py` — MIT, (c) 2026 Simone Rizzo,
# Rizzo AI Academy (https://github.com/Rizzo-AI-Academy/rizzo-pii) — con
# modifiche locali. La licenza MIT vuole che il suo avviso viaggi insieme a
# queste porzioni: il testo integrale è in fondo al file NOTICE, alla radice
# del repository.
"""
Generazione del PDF anonimizzato.

Due modalità, entrambe 100% in locale e senza dipendenze dal modello: il modulo
lavora solo su bytes + dizionario {placeholder -> valore}, quindi è testabile in
isolamento (nessun import di torch/transformers).

  redact_pdf(pdf_bytes, mapping)
      Redazione VERA del PDF originale: per ogni valore del dizionario cerca le
      occorrenze nelle pagine, RIMUOVE il testo dal content stream
      (page.apply_redactions(), non un rettangolo disegnato sopra) e scrive il
      placeholder al suo posto. Layout conservato.

      Il matching è CHAR-PRECISO con CONFINI DI PAROLA: la pagina viene
      indicizzata carattere per carattere (get_text("rawdict"), ogni glifo con il
      suo bbox) e i valori sono cercati con regex ancorate (?<!\\w)...(?!\\w).
      Così "DE" NON viene redatto dentro "CORDELLA" e una cifra non cancella i
      numeri di un referto: si redige il token/la sequenza esatta, ovunque ma
      intera. Il pattern tollera la SILLABAZIONE a fine riga ("Fran-\\ncesco") e
      gli spazi flessibili tra i token (anche a cavallo di riga).

  text_to_pdf(text)
      PDF "ricostruito" solo testo, impaginato da zero a partire dal testo già
      anonimizzato (input incollato oppure file .md/.txt).

  restore_pdf(pdf_bytes, mapping)
      La pipeline INVERSA (chat anonimizzata): un PDF prodotto dal modello a
      partire da input protetti contiene i segnaposto, e qui tornano i valori
      reali. Stessa meccanica di
      redact_pdf al contrario — indice char-preciso, rimozione dal content
      stream, scrittura al posto del testo rimosso — con tre differenze
      volute: nessuna casella colorata (l'utente riceve un documento da
      spedire, non una redazione), il valore viene scritto sulla BASELINE e col
      corpo/colore dello span originale, e la rimozione non tocca né immagini
      né linee (i bordi di tabella devono sopravvivere).

Cosa viene ripulito oltre al testo di pagina (tutti posti dove si nasconde PII e
che apply_redactions() da solo NON tocca):
  - metadati classici + XMP;
  - contenuto delle ANNOTAZIONI (commenti, FreeText, note);
  - valore dei CAMPI MODULO (widget AcroForm);
  - FIRME DIGITALI (FIX LOCALE, vedi _strip_widgets): il campo /Sig esce dal
    file per intero — l'aspetto visibile ("Firmato digitalmente da…", nome,
    data, ora) e il dizionario di firma (/Name, /M, certificato in /Contents).
    apply_redactions non tocca l'appearance stream dei widget e il valore del
    campo è vuoto: senza questo passo il nome del firmatario restava leggibile
    e la redazione veniva rifiutata dal controllo residui. La firma
    crittografica era comunque già invalidata dalla riscrittura del file;
  - widget SOLO-ASPETTO (pulsanti, caselle) il cui rettangolo è colpito da una
    redazione: il testo che mostrano non passa per il valore del campo, quindi
    si rimuove il widget (il box giallo disegnato in pagina resta visibile);
  - titoli dei SEGNALIBRI (outline/TOC);
  - ALLEGATI incorporati (embedded files), rimossi in blocco;
  - TESTO FANTASMA delle scansioni cercabili (FIX LOCALE, vedi pdf_ghost): la
    trascrizione OCR invisibile che sta sopra i pixel esce dal file all'inizio
    di redact_pdf. Non basta cercarla: le sue stringhe sono storpiate, quindi
    non combaciano coi valori della mappa e sfuggono anche a _verify_residuals.

Rete di sicurezza finale: `report["residual"]` elenca i placeholder il cui valore
è ANCORA leggibile nell'output (testo di pagina + annotazioni + widget +
dizionari di firma + TOC). Deve essere vuota; l'UI avvisa se non lo è.

Limiti noti:
  - questo modulo vede il SOLO layer testuale: il testo dentro le immagini raster
    (scansioni, loghi, blocchi firma) non esiste lì e qui non può essere né
    trovato né redatto — lo coprono i box nei pixel di engine/image_ocr, quando
    l'OCR è attivo. Se nel layer testuale non si trova NESSUNA occorrenza, il
    chiamante deve rifiutare: meglio un errore che un PDF "anonimizzato" che non
    lo è;
  - valori troppo corti/ambigui (< 2 caratteri alfanumerici, o 2 sole cifre) NON
    vengono redatti: cercarli ovunque devasterebbe il documento. Finiscono in
    `report["skipped"]` e l'UI DEVE avvisare, perché restano in chiaro.
"""

import math
import re
import unicodedata

import fitz  # PyMuPDF

from . import pdf_ghost
from .mupdf_lock import mupdf_serialized


class PdfError(ValueError):
    """Errore d'uso (PDF non valido, protetto, dizionario vuoto...)."""


# Creator/Producer dei PDF che questo modulo SCRIVE (vedi _scrub_metadata). I
# metadati originali si azzerano perché contengono PII, ma questi due campi non
# si lasciano vuoti: sono la provenienza del file, e dichiararla coerente conta
# per la marcatura AI Act (engine/ai_mark.py scrive nei metadati e conserva
# creator/producer). Il nome dell'applicazione, non quello di una dipendenza:
# l'attribuzione delle librerie sta nella licenza del progetto, non nei file
# che gli utenti spediscono.
PDF_PRODUCER = "BlockingBear"


# --------------------------------------------------------------------------- #
# Utility comuni
# --------------------------------------------------------------------------- #
# caratteri tipografici frequenti nei PDF estratti -> equivalenti Latin-1
# (il font base "helv" copre Latin-1: così la sostituzione è deterministica)
_TRANSLATE = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", "€": "EUR",
    " ": " ", "•": "-", "ﬁ": "fi", "ﬂ": "fl",
})


def _norm(s):
    """Spazi compressi + casefold: stessa normalizzazione di `core._norm`.
    Duplicata di proposito, perché importare `core` da qui si porterebbe
    dietro torch e il modello e costerebbe l'isolamento dichiarato in testa
    al modulo. Le due devono restare IDENTICHE: un valore normalizzato in un
    modo dal motore e in un altro qui non si ritroverebbe nella pagina."""
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def _fit_fontsize(text, rect, max_fs=10.0, min_fs=4.0):
    """Corpo del testo per far stare `text` dentro `rect` (0 = non ci sta:
    meglio nessuna etichetta che un'etichetta illeggibile o troncata).
    Il vincolo verticale è height/1.3 (una riga con interlinea), NON il
    "quasi tutta l'altezza": l'etichetta è poi disegnata con insert_text,
    che non tronca, quindi il fit deve essere garantito qui."""
    try:
        w10 = fitz.get_text_length(text, fontname="helv", fontsize=10.0)
    except Exception:
        return 0
    if w10 <= 0:
        return 0
    fs = min(max_fs, rect.height / 1.3, 10.0 * max(rect.width - 2.0, 0.0) / w10)
    return round(fs, 1) if fs >= min_fs else 0


def _draw_label(page, rect, text, fontsize, color):
    """Disegna il placeholder centrato dentro `rect` DOPO apply_redactions.

    Le redazioni MuPDF sanno disegnare un testo sostitutivo, ma lo scartano in
    silenzio quando il layout interno non lo fa stare (successo con etichette
    date per buone da _fit_fontsize): il risultato era un box giallo muto.
    Qui si usa insert_text a coordinate esplicite — o si disegna tutta
    l'etichetta o niente, mai un troncamento."""
    try:
        w = fitz.get_text_length(text, fontname="helv", fontsize=fontsize)
        x = rect.x0 + (rect.width - w) / 2
        y = (rect.y0 + rect.y1) / 2 + 0.35 * fontsize
        page.insert_text((x, y), text, fontname="helv", fontsize=fontsize,
                         color=color)
        return True
    except Exception:
        return False


def _covered(rect, taken, thr=0.85):
    """True se `rect` è già (quasi) tutto dentro una redazione precedente:
    evita doppioni quando un valore è contenuto in un altro (es. "Rossi"
    dentro "Mario Rossi", redatto prima perché più lungo)."""
    area = rect.get_area()
    if area <= 0:
        return True
    for t in taken:
        inter = fitz.Rect(rect)
        inter.intersect(t)
        if not inter.is_empty and inter.get_area() / area >= thr:
            return True
    return False


# --------------------------------------------------------------------------- #
# Indice char-preciso della pagina + ricerca con confini di parola
# --------------------------------------------------------------------------- #
def _page_char_index(page, styles=False):
    """(testo, [bbox per carattere]) dalla pagina: ogni carattere del layer
    testuale con il suo rettangolo (None per i newline di fine riga).

    Con styles=True torna un terzo elemento allineato agli altri due:
    (origin, span, dir) per carattere, cioè il punto di BASELINE, lo span di
    provenienza (font, corpo, colore) e la direzione della riga. Serve solo al
    ripristino, che deve riscrivere il valore dove stava il segnaposto e con la
    sua stessa faccia; la redazione non ne ha bisogno e continua a ricevere due
    valori."""
    raw = page.get_text("rawdict")
    chars, boxes, meta = [], [], []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:          # solo blocchi di testo
            continue
        for line in block.get("lines", []):
            direction = line.get("dir") or (1.0, 0.0)
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    c = ch.get("c") or ""
                    for cc in c:            # ligature -> più caratteri, stesso bbox
                        chars.append(cc)
                        boxes.append(fitz.Rect(ch["bbox"]))
                        if styles:
                            meta.append((ch.get("origin"), span, direction))
            chars.append("\n")
            boxes.append(None)
            if styles:
                meta.append(None)
    text = "".join(chars)
    return (text, boxes, meta) if styles else (text, boxes)


# Sillabazione a fine riga: "Fran-\ncesco", "Fran­\ncesco" o anche solo un
# a-capo dentro la parola. Ammesso TRA due caratteri qualsiasi del valore, ma solo
# se c'è davvero un newline: senza, il gruppo non consuma nulla e il matching
# resta char-per-char (niente tolleranza allo spacing, che aprirebbe a
# sovra-redazioni tipo "MI" dentro "M I L A N O").
_HYPHEN_BREAK = r"(?:[­‐-]?[ \t]*\n[ \t]*)?"


# Sotto questa soglia di caratteri alfanumerici un frammento NON può rinunciare
# ai confini di parola: ".it" senza ancore matcherebbe "it" dentro ogni parola.
_UNANCHORED_MIN_ALNUM = 4


def _value_pattern(value):
    """Regex del valore: caratteri esatti con CONFINI DI PAROLA agli estremi,
    sillabazione a fine riga tollerata, whitespace flessibile tra i token.
    Niente match di sottostringhe dentro altre parole.

    Eccezione: quando il valore ha punteggiatura ai bordi il modello ha tagliato
    a metà una parola spezzata a fine riga (tipico: "Fran-\\ncesco Cordella" ->
    il modello etichetta "-\\ncesco Cordella"). Lì il confine di parola su quel
    lato è proprio ciò che impedisce di trovare il valore, che resterebbe in
    chiaro: si toglie la punteggiatura e con essa l'ancora, ma solo se quel che
    resta è abbastanza lungo da non trasformarsi in una sottostringa qualunque.

    Sui bordi NUMERICI l'ancora di parola non basta: il separatore decimale non
    è un carattere di parola, quindi "4990" (etichettato civico su un importo)
    verrebbe trovato dentro "4990.90" e il numero uscirebbe come
    "[BUILDINGNUM_3].90". Su un PDF è testo storto; in un CSV il valore diventa
    spazzatura e in un JSON il file non si apre più. Un numero attaccato a un
    separatore decimale e ad altre cifre è un numero DIVERSO da quello
    etichettato, quindi non è un match e non va redatto."""
    v = _norm(value)
    core = re.sub(r"^[^\w]+", "", v)
    core = re.sub(r"[^\w]+$", "", core)
    toks = [t for t in core.split(" ") if t]
    if not toks:
        return None
    if len(re.sub(r"[\W_]+", "", core)) >= _UNANCHORED_MIN_ALNUM:
        left = "" if re.match(r"^[^\w]", v) else r"(?<!\w)"
        right = "" if re.search(r"[^\w]$", v) else r"(?!\w)"
    else:                                  # troppo corto: ancore obbligatorie
        toks = [t for t in v.split(" ") if t]
        left, right = r"(?<!\w)", r"(?!\w)"
    if toks[0][0].isdigit():
        left += r"(?<!\d[.,])"
    if toks[-1][-1].isdigit():
        right += r"(?![.,]\d)"
    body = r"\s*".join(_HYPHEN_BREAK.join(re.escape(c) for c in tok) for tok in toks)
    return re.compile(left + body + right, re.IGNORECASE)


class _Span:
    """Intervallo [start, end) di caratteri con l'interfaccia minima di un
    re.Match. I valori si cercano sul testo dell'INTERO documento
    (_doc_char_index) e il match si ritaglia poi pagina per pagina
    (_by_page): le funzioni che lavorano sull'indice di una pagina ricevono
    il pezzo locale in questa forma."""
    __slots__ = ("_s", "_e")

    def __init__(self, start, end):
        self._s, self._e = start, end

    def start(self):
        return self._s

    def end(self):
        return self._e


def _doc_char_index(doc):
    """Indice char-preciso dell'INTERO documento: (testo, [(offset, boxes)])
    con una voce per pagina. Il testo è la concatenazione dei testi di
    pagina (ognuno finisce già col newline della sua ultima riga) e gli
    offset dicono dove comincia ciascuna pagina.

    Cercare pagina per pagina perde i valori a cavallo di un salto pagina
    (una chiave su più righe, un indirizzo spezzato in fondo alla pagina):
    su nessuna delle due pagine prese da sole il valore esiste. Nel PDF
    redatto restava in chiaro (e la verifica dei residui, che legge il testo
    di tutte le pagine unito, lo segnalava bloccando il documento); nelle
    anteprime degli altri formati il lato originale restava senza box."""
    parts, pages, off = [], [], 0
    for page in doc:
        text, boxes = _page_char_index(page)
        pages.append((off, boxes))
        parts.append(text)
        off += len(text)
    return "".join(parts), pages


def _by_page(pages, m):
    """Il match `m` (sul testo dell'intero documento) spezzato per pagina:
    [(numero pagina, _Span locale)]. Quasi sempre un solo elemento."""
    out = []
    for pno, (off, boxes) in enumerate(pages):
        s = max(m.start(), off) - off
        e = min(m.end(), off + len(boxes)) - off
        if s < e:
            out.append((pno, _Span(s, e)))
    return out


def _match_rects(boxes, m):
    """Bbox dei caratteri del match, uniti per riga (overlap verticale)."""
    rects, cur = [], None
    for i in range(m.start(), m.end()):
        b = boxes[i]
        if b is None or b.is_empty:
            continue
        if cur is None:
            cur = fitz.Rect(b)
        elif b.y0 < cur.y1 and b.y1 > cur.y0:      # stessa riga
            cur |= b
        else:                                       # riga nuova
            rects.append(cur)
            cur = fitz.Rect(b)
    if cur is not None and not cur.is_empty:
        rects.append(cur)
    return rects


def _clamp_line_bleed(rect, m, boxes):
    """Ritira verticalmente il rect di un match dai caratteri FUORI dal match.

    I bbox di rawdict sono alti quanto la LINE-HEIGHT del font: nei PDF con
    interlinea più stretta (tipico output gestionale/SAP) i box di due righe
    adiacenti si sovrappongono di 1-2pt, e apply_redactions rimuove OGNI glifo
    che il rettangolo interseca — anche sulla riga sopra/sotto ("VIA QUINTILIAN"
    che cancella metà di "ACME FORNITURE SRL" sulla riga precedente). Qui il
    bordo viene spostato appena oltre i box estranei toccati di striscio
    (overlap < metà altezza: è una riga adiacente, non un valore annidato,
    che resta gestito da _covered). Con interlinee strettissime il ritiro può
    avvenire da ENTRAMBI i lati e la banda restante è sottile: va bene così,
    la rimozione MuPDF scatta su qualsiasi intersezione col box del glifo.
    Solo se la banda collassa del tutto si tiene il rect originale: meglio
    sovra-redigere che lasciare PII in chiaro, e i residui sono comunque
    verificati a valle."""
    r = fitz.Rect(rect)
    for i, b in enumerate(boxes):
        if b is None or b.is_empty or m.start() <= i < m.end():
            continue
        if b.x1 <= r.x0 or b.x0 >= r.x1:
            continue
        ov = min(r.y1, b.y1) - max(r.y0, b.y0)
        if ov <= 0 or ov >= 0.5 * min(b.height, rect.height):
            continue
        if b.y1 - rect.y0 < rect.y1 - b.y0:     # sporge dall'alto
            r.y0 = max(r.y0, b.y1 + 0.1)
        else:                                    # sporge dal basso
            r.y1 = min(r.y1, b.y0 - 0.1)
    if r.is_empty or r.height < 1.0:
        return fitz.Rect(rect)
    return r


def _too_noisy(value):
    """Valori non localizzabili in modo sicuro in un PDF: frammenti con meno di 2
    caratteri alfanumerici, o di 2 sole cifre (es. "1", "C", "05", prodotti a
    volte dal modello su testi tabellari). Cercarli ovunque cancellerebbe pezzi di
    documento non-PII: si saltano, MA il chiamante deve avvisare l'utente (restano
    in chiaro)."""
    alnum = re.sub(r"[\W_]+", "", _norm(value))
    return len(alnum) < 2 or (len(alnum) == 2 and alnum.isdigit())


# --------------------------------------------------------------------------- #
# Pulizia di tutto ciò che apply_redactions() non tocca
# --------------------------------------------------------------------------- #
def _scrub_metadata(doc):
    """Azzera i metadati classici e l'XMP: possono contenere PII (autore...)."""
    try:
        # set_metadata aggiorna SOLO le chiavi passate: vanno azzerate tutte
        # esplicitamente, altrimenti autore/titolo originali restano nel file
        doc.set_metadata({
            "title": "", "author": "", "subject": "", "keywords": "",
            "creationDate": "", "modDate": "", "trapped": "",
            "creator": PDF_PRODUCER, "producer": PDF_PRODUCER,
        })
    except Exception:
        pass
    f = getattr(doc, "del_xml_metadata", None) or getattr(doc, "delXmlMetadata", None)
    if f:
        try:
            f()
        except Exception:
            pass


def _sub_all(patterns, s):
    """Applica tutte le (regex, placeholder) alla stringa. Ritorna (nuova, n)."""
    n = 0
    for pat, ph in patterns:
        s, k = pat.subn(ph, s)
        n += k
    return s, n


# I tre _scrub_* qui sotto ricevono `sub`, una funzione str -> (nuova_str, n):
# la redazione le passa la sostituzione valore -> segnaposto, il ripristino
# quella inversa. Così i posti dove si nasconde il testo fuori dal content
# stream (annotazioni, campi modulo, segnalibri) sono coperti una volta sola e
# nelle due direzioni non possono divergere.

def _scrub_annots(page, sub):
    """Contenuto e titolo delle annotazioni (commenti, FreeText, note adesive):
    testo vero e proprio, invisibile a get_text() e non toccato dalle redazioni."""
    done = 0
    try:
        annots = list(page.annots())
    except Exception:
        return 0
    for a in annots:
        try:
            if a.type[0] == fitz.PDF_ANNOT_REDACT:      # le nostre, già gestite
                continue
            info = a.info
            if info.get("title") == MARKER_TITLE:
                # marcatore di redazione: il subject È un placeholder e deve
                # restare tale (sostituirlo scriverebbe il valore reale in
                # un'annotazione del file consegnato)
                continue
            new = dict(info)
            hit = 0
            for k in ("content", "subject", "title"):
                if info.get(k):
                    new[k], k_hit = sub(info[k])
                    hit += k_hit
            if hit:
                a.set_info(new)
                a.update()
                done += hit
        except Exception:
            continue
    return done


def _scrub_widgets(page, sub):
    """Valore dei campi modulo AcroForm: sopravvive a qualsiasi redazione del
    content stream ed è leggibile aprendo il PDF."""
    done = 0
    try:
        widgets = list(page.widgets())
    except Exception:
        return 0
    for w in widgets:
        try:
            val = w.field_value
            if not isinstance(val, str) or not val:
                continue
            new, hit = sub(val)
            if hit:
                w.field_value = new
                w.update()
                done += hit
        except Exception:
            continue
    return done


# Widget il cui testo visibile NON passa per field_value: quello che mostrano
# sta solo nell'appearance stream, che né apply_redactions né _scrub_widgets
# toccano. I campi di testo/scelta non sono qui: per loro update() rigenera
# l'aspetto dal valore riscritto.
_APPEARANCE_ONLY_WIDGETS = frozenset({
    fitz.PDF_WIDGET_TYPE_BUTTON, fitz.PDF_WIDGET_TYPE_CHECKBOX,
    fitz.PDF_WIDGET_TYPE_RADIOBUTTON, fitz.PDF_WIDGET_TYPE_UNKNOWN,
})

# Voci testuali di un dizionario di firma (/V di un campo /Sig): il nome del
# firmatario e i suoi recapiti stanno qui in chiaro, fuori da get_text().
_SIG_TEXT_KEYS = ("Name", "Reason", "Location", "ContactInfo")


def _sig_surfaces(doc, page):
    """Stringhe leggibili dei dizionari di firma dei widget /Sig della pagina."""
    out = []
    try:
        widgets = list(page.widgets())
    except Exception:
        return out
    for w in widgets:
        try:
            if w.field_type != fitz.PDF_WIDGET_TYPE_SIGNATURE:
                continue
            for key in _SIG_TEXT_KEYS:
                kind, val = doc.xref_get_key(w.xref, f"V/{key}")
                if kind == "string" and val:
                    out.append(val)
        except Exception:
            continue
    return out


def _strip_widgets(page, placed_rects):
    """Toglie dalla pagina i widget che una redazione del content stream non
    può ripulire. Ritorna (firme_rimosse, widget_rimossi).

    - campi FIRMA (/Sig): SEMPRE via, colpiti o no. L'aspetto porta nome, data
      e ora del firmatario dentro il proprio appearance stream; il dizionario
      di firma porta lo stesso nome in /Name e il certificato in /Contents,
      invisibili a get_text() e quindi a ogni controllo testuale. In un file
      anonimizzato la firma è comunque già rotta (il file viene riscritto):
      tenerne il guscio significherebbe solo consegnare PII;
    - widget solo-aspetto (pulsanti, caselle): via solo se il loro rettangolo
      interseca una casella redatta — lì dentro il matcher ha trovato un valore
      che è disegnato dall'appearance stream e non da field_value. Il box
      giallo con l'etichetta è già nel content stream e resta visibile."""
    try:
        widgets = list(page.widgets())
    except Exception:
        return 0, 0
    sigs = others = 0
    for w in widgets:
        try:
            if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE:
                page.delete_widget(w)
                sigs += 1
            elif w.field_type in _APPEARANCE_ONLY_WIDGETS:
                wr = fitz.Rect(w.rect)
                if any(not (wr & r).is_empty for r in placed_rects):
                    page.delete_widget(w)
                    others += 1
        except Exception:
            continue
    return sigs, others


def _drop_signature_flags(doc):
    """Dopo la rimozione dei campi /Sig: via /SigFlags dall'AcroForm e /Perms
    dal catalogo (DocMDP/UR3 riferiscono firme che non esistono più)."""
    try:
        cat = doc.pdf_catalog()
        if doc.xref_get_key(cat, "AcroForm/SigFlags")[0] != "null":
            doc.xref_set_key(cat, "AcroForm/SigFlags", "null")
        if doc.xref_get_key(cat, "Perms")[0] != "null":
            doc.xref_set_key(cat, "Perms", "null")
    except Exception:
        pass


def _scrub_toc(doc, sub):
    """Titoli dei segnalibri: spesso ricalcano intestazioni con nomi e numeri."""
    try:
        toc = doc.get_toc(simple=True)
    except Exception:
        return 0
    if not toc:
        return 0
    done, new_toc = 0, []
    for entry in toc:
        lvl, title, pg = entry[0], entry[1], entry[2]
        title, hit = sub(title or "")
        done += hit
        new_toc.append([lvl, title, pg])
    if done:
        try:
            doc.set_toc(new_toc)
        except Exception:
            return 0
    return done


def _strip_embedded(doc):
    """Allegati incorporati: non ispezionabili in modo affidabile (possono essere
    di qualunque formato), quindi si rimuovono tutti."""
    removed = 0
    try:
        names = list(doc.embfile_names())
    except Exception:
        return 0
    for name in names:
        try:
            doc.embfile_del(name)
            removed += 1
        except Exception:
            continue
    return removed


def _readable_text(doc):
    """TUTTO il testo leggibile del documento: pagine + annotazioni + campi
    modulo + segnalibri. È la base della verifica dei residui: se un valore
    compare qui, l'anonimizzazione NON è completa.
    NB: non vede il testo dentro immagini raster (loghi, firme, scansioni)."""
    parts = []
    for page in doc:
        parts.append(page.get_text())
        try:
            for a in page.annots():
                if a.type[0] == fitz.PDF_ANNOT_REDACT:
                    continue
                info = a.info
                parts.extend(str(info.get(k) or "") for k in ("content", "subject", "title"))
        except Exception:
            pass
        try:
            for w in page.widgets():
                if isinstance(w.field_value, str):
                    parts.append(w.field_value)
        except Exception:
            pass
        parts.extend(_sig_surfaces(doc, page))
    try:
        parts.extend(str(e[1] or "") for e in doc.get_toc(simple=True))
    except Exception:
        pass
    return "\n".join(parts)


@mupdf_serialized
def _verify_residuals(pdf_bytes, items):
    """Placeholder il cui valore è ANCORA leggibile nell'output. Usa lo STESSO
    pattern della redazione (sillabazione inclusa), altrimenti dichiarerebbe
    "0 residui" proprio nei casi che il matcher non sa gestire."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        text = _readable_text(doc)
    residual = []
    for ph, val in items:
        pat = _value_pattern(val)
        if pat and pat.search(text):
            residual.append(ph)
    return residual


# --------------------------------------------------------------------------- #
# API pubblica
# --------------------------------------------------------------------------- #
# Colore della casella di redazione: giallo evidenziatore con il placeholder
# in nero sopra — lo stesso di HIGHLIGHT_FILL (pdf.py) e di image_ocr.YELLOW,
# così in tutta l'app le caselle hanno due soli sfondi: giallo (redatto) e
# nero (area sigillata dall'utente).
REDACT_FILL = (1.0, 0.92, 0.30)
REDACT_TEXT = (0.0, 0.0, 0.0)


@mupdf_serialized
def redact_pdf(pdf_bytes, mapping, fill=REDACT_FILL, text_color=REDACT_TEXT):
    """PDF originale -> PDF con redazione vera + placeholder al posto delle PII.

    mapping: {"[FULLNAME_1]": "Mario Rossi", ...} (il dizionario di analyze()).
    Ritorna (bytes, report) con report = {
        "boxes":          {pagina: [{x0,y0,x1,y1,ph}]}: TUTTI i rettangoli
                          redatti (in punti PDF), etichettati o no — è la
                          base dei box interattivi dell'anteprima,
        "occurrences":    occorrenze redatte nel testo di pagina,
        "by_placeholder": {placeholder: n_occorrenze},
        "not_found":      placeholder cercati ma senza occorrenze di pagina,
        "skipped":        placeholder NON cercati perché troppo corti/ambigui
                          (restano in chiaro: va segnalato all'utente),
        "residual":       placeholder ancora leggibili nell'output (deve essere []),
        "annots":         sostituzioni nelle annotazioni,
        "widgets":        sostituzioni nei campi modulo,
        "signatures":     campi firma digitale rimossi (aspetto + dizionario),
        "widgets_removed": widget solo-aspetto rimossi perché colpiti da una
                          redazione (il loro testo non passa per il valore),
        "toc":            sostituzioni nei segnalibri,
        "embedded":       allegati rimossi,
    }
    """
    if not isinstance(mapping, dict) or not mapping:
        raise PdfError("Dizionario vuoto: anonimizza prima il documento.")
    items = [(ph, v) for ph, v in mapping.items()
             if isinstance(ph, str) and isinstance(v, str) and v.strip()]
    if not items:
        raise PdfError("Dizionario non valido.")

    # Testo FANTASMA fuori dal file PRIMA di redigere (vedi pdf_ghost): è la
    # trascrizione invisibile che una scansione cercabile porta sopra i propri
    # pixel, e la redazione da sola non la toglie — le sue stringhe sono
    # storpiate, quindi non combaciano coi valori della mappa, né qui né in
    # _verify_residuals. Restava nel file anonimizzato, estraibile con un
    # copia-incolla e invisibile a ogni controllo. Questa è la porta comune a
    # tutte le redazioni di PDF (chat e progetti), quindi vale per tutte.
    pdf_bytes, _ghosts = pdf_ghost.strip_ghost_text(pdf_bytes)

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        raise PdfError("File PDF non valido o danneggiato.")
    if doc.needs_pass:
        doc.close()
        raise PdfError("PDF protetto da password: rimuovi la protezione e riprova.")

    # valori lunghi per primi: così "Rossi" non spezza la redazione di
    # "Mario Rossi" (i rect già coperti vengono saltati da _covered)
    items.sort(key=lambda kv: -len(kv[1]))

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

    by_ph = {ph: 0 for ph, _, _ in usable}
    patterns = [(pat, ph) for ph, _, pat in usable]   # per annot/widget/TOC

    def sub(s):
        return _sub_all(patterns, s)

    total = n_annots = n_widgets = n_sigs = n_wremoved = 0
    placed_boxes = {}          # {pagina: [{x0,y0,x1,y1,ph,occ}]}: TUTTE le redazioni

    # matching sul testo dell'INTERO documento (vedi _doc_char_index), poi
    # ogni match si ritaglia nelle sue pagine; taken/placed restano per
    # pagina. `occ` numera le occorrenze: i rettangoli di uno stesso match
    # (più righe, anche su due pagine) portano lo stesso numero, così il
    # frontend sa che sono un unico box logico
    doc_text, pages = _doc_char_index(doc)
    taken = [[] for _ in pages]
    placed = [[] for _ in pages]         # (rect, ph, fs, occ)
    occ = 0
    if doc_text.strip():
        for ph, val, pat in usable:
            for m in pat.finditer(doc_text):
                placed_any, labeled = False, False
                for pno, span in _by_page(pages, m):
                    boxes = pages[pno][1]
                    for r in _match_rects(boxes, span):
                        r = _clamp_line_bleed(r, span, boxes)
                        if _covered(r, taken[pno]):
                            continue
                        fs = 0 if labeled else _fit_fontsize(ph, r)
                        _add_redact_annot(doc[pno], r, fill)
                        taken[pno].append(fitz.Rect(r))
                        placed[pno].append((fitz.Rect(r), ph, fs, occ))
                        placed_any = True
                        labeled = labeled or bool(fs)
                if placed_any:
                    by_ph[ph] += 1
                    total += 1
                    occ += 1

    for pno, page in enumerate(doc):
        if placed[pno]:
            page.apply_redactions()   # rimozione VERA dal content stream
            # etichette disegnate DOPO la redazione, in proprio (_draw_label):
            # il testo sostitutivo delle redazioni MuPDF sparisce in silenzio
            # quando non ci sta. fs=0 = etichetta assente, MA il box resta in
            # placed_boxes: l'UI lo rende comunque interattivo (tooltip).
            for r, ph, fs, _o in placed[pno]:
                if fs:
                    _draw_label(page, r, ph, fs, text_color)
            _add_markers(page, [(r, ph, fs) for r, ph, fs, _o in placed[pno]])
            placed_boxes[pno] = [{"x0": r.x0, "y0": r.y0,
                                  "x1": r.x1, "y1": r.y1, "ph": ph, "occ": o}
                                 for r, ph, _fs, o in placed[pno]]
        # dopo le redazioni: annotazioni e campi modulo non sono content stream
        n_annots += _scrub_annots(page, sub)
        n_widgets += _scrub_widgets(page, sub)
        # firme e widget solo-aspetto: quello che mostrano non è riscrivibile,
        # l'unica pulizia possibile è toglierli
        sigs, removed = _strip_widgets(page, [r for r, _ph, _fs, _o in placed[pno]])
        n_sigs += sigs
        n_wremoved += removed

    if n_sigs:
        _drop_signature_flags(doc)
    n_toc = _scrub_toc(doc, sub)
    n_emb = _strip_embedded(doc)
    _scrub_metadata(doc)
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()

    checked = [(ph, v) for ph, v in items if ph in by_ph]
    return out, {
        "boxes": placed_boxes,
        "occurrences": total,
        "by_placeholder": by_ph,
        "not_found": [ph for ph, n in by_ph.items() if n == 0],
        "skipped": skipped,
        "residual": _verify_residuals(out, checked),
        "annots": n_annots,
        "widgets": n_widgets,
        "signatures": n_sigs,
        "widgets_removed": n_wremoved,
        "toc": n_toc,
        "embedded": n_emb,
    }


def _add_redact_annot(page, rect, fill):
    """add_redact_annot con cross_out disattivato dove il binding lo supporta
    (le X diagonali sporcano il placeholder); fallback per le versioni vecchie.
    Nessun testo sostitutivo: le etichette le disegna _draw_label a valle."""
    try:
        return page.add_redact_annot(rect, cross_out=False, fill=fill)
    except TypeError:
        return page.add_redact_annot(rect, fill=fill)


# Titolo delle annotazioni-marcatore della redazione (vedi _add_markers).
MARKER_TITLE = "GdnRedact"


def _add_markers(page, placed):
    """Un'annotazione-marcatore INVISIBILE per ogni casella redatta: porta il
    placeholder (subject) e il rettangolo ESATTO del box giallo. È ciò che
    rende il giallo rimovibile al ripristino (restore_pdf): il rect del
    marcatore copre il box per intero, quindi apply_redactions può toglierlo
    con LINE_ART_REMOVE_IF_COVERED senza sfiorare i bordi tabella o la grafica
    che lo attraversa soltanto. Nascosta (HIDDEN): nessun viewer la disegna;
    se uno strumento a valle la spoglia si ricade nel caso peggiore ammesso —
    il giallo resta nel file e viene dichiarato — mai in qualcosa di più
    grave."""
    for r, ph, _fs in placed:
        try:
            a = page.add_rect_annot(r)
            a.set_info(title=MARKER_TITLE, subject=ph)
            a.set_flags(fitz.PDF_ANNOT_IS_HIDDEN)
            a.update()
        except Exception:
            continue        # marcatore perso = giallo non rimovibile, non fatale


def _rect_key(r):
    """Chiave stabile di un rect di annotazione (i float vengono arrotondati
    alla scrittura nel PDF: il confronto esatto non regge)."""
    return tuple(round(v, 2) for v in (r.x0, r.y0, r.x1, r.y1))


def _page_markers(page):
    """[(annot, placeholder, rect)] dei marcatori di redazione sulla pagina."""
    out = []
    try:
        annots = list(page.annots())
    except Exception:
        return out
    for a in annots:
        try:
            info = a.info
            if info.get("title") == MARKER_TITLE and info.get("subject"):
                out.append((a, info["subject"], fitz.Rect(a.rect)))
        except Exception:
            continue
    return out


@mupdf_serialized
def text_to_pdf(text, margin=56.0, fontsize=10.5, leading=15.5):
    """Testo (già anonimizzato) -> PDF A4 solo testo, impaginato da zero.
    Nessun contenuto del documento originale finisce nell'output."""
    text = unicodedata.normalize("NFKC", text or "").translate(_TRANSLATE)
    # helv copre Latin-1: sostituzione esplicita dei caratteri fuori codifica
    text = text.encode("latin-1", "replace").decode("latin-1")
    if not text.strip():
        raise PdfError("Nessun testo da impaginare.")

    a4 = fitz.paper_rect("a4")
    width = a4.width - 2 * margin

    def tl(s):
        return fitz.get_text_length(s, fontname="helv", fontsize=fontsize)

    def hard_split(line):
        """Spezza le 'parole' più larghe della riga (IBAN, URL...)."""
        out = []
        while tl(line) > width and len(line) > 1:
            k = max(1, int(len(line) * width / tl(line)))
            while k > 1 and tl(line[:k]) > width:
                k -= 1
            while k < len(line) and tl(line[:k + 1]) <= width:
                k += 1
            out.append(line[:k])
            line = line[k:]
        out.append(line)
        return out

    def wrap(par):
        if not par:
            return [""]
        lines, cur = [], ""
        for w in par.split(" "):
            cand = (cur + " " + w) if cur else w
            if not cur or tl(cand) <= width:
                cur = cand
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        out = []
        for ln in lines:
            out.extend(hard_split(ln))
        return out

    doc = fitz.open()
    page = doc.new_page(width=a4.width, height=a4.height)
    y = margin + fontsize
    for par in text.split("\n"):
        for ln in wrap(par.rstrip()):
            if y > a4.height - margin:
                page = doc.new_page(width=a4.width, height=a4.height)
                y = margin + fontsize
            if ln:
                page.insert_text((margin, y), ln, fontname="helv",
                                 fontsize=fontsize)
            y += leading

    _scrub_metadata(doc)
    out = doc.tobytes(deflate=True)
    doc.close()
    return out


# --------------------------------------------------------------------------- #
# Pipeline INVERSA: segnaposto -> valori reali
# --------------------------------------------------------------------------- #
# Forma canonica di un segnaposto e variante che tollera l'a-capo in mezzo (un
# segnaposto lungo dentro una cella strettissima può essere spezzato da
# LibreOffice come qualsiasi altra parola).
PLACEHOLDER_RE = re.compile(r"\[[A-Z][A-Z0-9_]*_\d+\]")
_PH_BROKEN_RE = re.compile(
    r"\[" + _HYPHEN_BREAK + r"[A-Z](?:" + _HYPHEN_BREAK + r"[A-Z0-9_])*"
    + _HYPHEN_BREAK + r"_" + _HYPHEN_BREAK + r"\d(?:" + _HYPHEN_BREAK
    + r"\d)*" + _HYPHEN_BREAK + r"\]")
_BREAK_STRIP_RE = re.compile(r"[­‐-]?[ \t]*\n[ \t]*")

# Famiglie base-14 (regular, bold, italic, bold-italic). I font incorporati NON
# sono riusabili: LibreOffice li scrive come subset con un'encoding propria e
# senza cmap utile, quindi fitz.Font(fontbuffer=...) non sa mappare nemmeno "M"
# o le cifre (verificato) e scriverebbe glifi vuoti. I base-14 sono l'unica
# scelta che rende sempre, e per l'output LibreOffice sono vicini: Calibri e
# Carlito hanno le metriche di Arial (helv), Liberation Serif quelle di Times.
_BASE14 = {
    "sans": ("helv", "hebo", "heit", "hebi"),
    "serif": ("tiro", "tibo", "tiit", "tibi"),
    "mono": ("cour", "cobo", "coit", "cobi"),
}
_SERIF_HINTS = ("times", "serif", "georgia", "garamond", "roman", "cambria",
                "constantia", "book", "minion", "palatino", "century")
_MONO_HINTS = ("mono", "courier", "consol", "menlo")
# Corpo minimo tecnico: sotto il punto i lettori PDF non disegnano più nulla,
# e un valore invisibile sarebbe peggio di uno stretto.
_MIN_FONTSIZE = 1.0


def sub_placeholders(text, mapping, escape=False):
    """Sostituisce nel testo i segnaposto NOTI col valore reale.

    Quelli che non sono nel registro restano come sono e non vengono contati: il
    conteggio dice quanto è stato davvero ripristinato, non quanti segnaposto
    c'erano. `escape=True` per il testo che finisce dentro XML (OOXML).
    Ritorna (nuovo_testo, n)."""
    if not text or not mapping:
        return text, 0
    done = 0

    def one(match):
        nonlocal done
        value = mapping.get(match.group(0))
        if value is None:
            return match.group(0)
        done += 1
        value = str(value)
        if escape:
            value = (value.replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;"))
        return value
    return PLACEHOLDER_RE.sub(one, text), done


def _base14(span):
    """Il font base-14 più vicino allo span. La famiglia si decide dal NOME
    (il bit "serifed" dei flag MuPDF è inaffidabile: Calibri arriva marcato
    serif), corsivo e neretto dai flag con il nome come rinforzo."""
    name = str(span.get("font") or "").lower()
    flags = int(span.get("flags") or 0)
    bold = bool(flags & 16) or any(h in name for h in ("bold", "black", "heavy"))
    italic = bool(flags & 2) or any(h in name for h in ("italic", "oblique"))
    if any(h in name for h in _MONO_HINTS) or (not name and flags & 8):
        family = _BASE14["mono"]
    elif any(h in name for h in _SERIF_HINTS):
        family = _BASE14["serif"]
    else:
        family = _BASE14["sans"]
    return family[(1 if bold else 0) + (2 if italic else 0)]


def _latin1(value):
    """I base-14 coprono Latin-1. I caratteri fuori codifica si approssimano
    togliendo i segni diacritici (Ș -> S) prima di arrendersi a "?": un nome
    con una lettera sbagliata resta leggibile, un nome di soli "?" no.
    Ritorna (testo, degradato)."""
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_TRANSLATE)
    out, degraded = [], False
    for ch in text:
        try:
            ch.encode("latin-1")
        except UnicodeEncodeError:
            pass
        else:
            out.append(ch)
            continue
        alt = "".join(c for c in unicodedata.normalize("NFKD", ch)
                      if not unicodedata.combining(c))
        try:
            alt.encode("latin-1")
        except UnicodeEncodeError:
            alt = ""
        degraded = True
        out.append(alt or "?")
    return "".join(out), degraded


def _span_color(span):
    try:
        return fitz.sRGB_to_pdf(int(span.get("color") or 0))
    except Exception:
        return (0.0, 0.0, 0.0)


def _rotation(direction):
    """Rotazione da passare a insert_text per la direzione della riga (solo i
    quattro angoli retti: insert_text non sa fare altro)."""
    try:
        dx, dy = float(direction[0]), float(direction[1])
    except (TypeError, ValueError, IndexError):
        return 0
    if abs(dy) <= 0.01:
        return 0 if dx >= 0 else 180
    if abs(dx) <= 0.01:
        return 90 if dy < 0 else 270
    return 0


def _clamp_side_bleed(rect, m, boxes, eps=0.05):
    """Ritira il rect ORIZZONTALMENTE dai caratteri adiacenti che tocca di
    striscio (kerning: i bbox di due glifi contigui si sovrappongono di frazioni
    di punto, e apply_redactions cancella ogni glifo che il rettangolo
    interseca). Nella redazione il danno resta nascosto sotto la casella gialla;
    qui si vedrebbe — "Scrivere a mario.rossi@x.it ntro il 15/09" — quindi va
    evitato. Restringere di una frazione di punto non salva il segnaposto: i
    suoi glifi restano comunque intersecati, sono dentro il rettangolo."""
    r = fitz.Rect(rect)
    for i, b in enumerate(boxes):
        if b is None or b.is_empty or m.start() <= i < m.end():
            continue
        if b.y1 <= r.y0 or b.y0 >= r.y1:        # non è sulla stessa riga
            continue
        overlap = min(r.x1, b.x1) - max(r.x0, b.x0)
        if overlap <= 0 or overlap >= 0.4 * min(b.width, rect.width):
            continue                            # sovrapposizione vera, non kerning
        if b.x1 - rect.x0 < rect.x1 - b.x0:     # sporge da sinistra
            r.x0 = max(r.x0, b.x1 + eps)
        else:                                   # sporge da destra
            r.x1 = min(r.x1, b.x0 - eps)
    if r.is_empty or r.width < 0.5:
        return fitz.Rect(rect)
    return r


def _page_vlines(page):
    """[(x, y0, y1)] dei segmenti VERTICALI del disegno vettoriale: bordi di
    tabella e filetti. Limitano la scrittura di un valore lungo esattamente
    come un carattere — senza di loro il valore in una cella dell'ultima
    colonna scavalcherebbe il bordo, che è il caso più comune negli artifact
    (una tabella con una colonna di nomi)."""
    lines = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return lines
    for drawing in drawings:
        for item in drawing.get("items", ()):
            try:
                if item[0] == "l":
                    p1, p2 = item[1], item[2]
                    if abs(p1.x - p2.x) <= 0.6 and abs(p1.y - p2.y) > 1.0:
                        lines.append((min(p1.x, p2.x), min(p1.y, p2.y),
                                      max(p1.y, p2.y)))
                elif item[0] == "re":
                    r = fitz.Rect(item[1])
                    if r.height <= 1.0:
                        continue
                    if r.width <= 0.6:          # filetto disegnato come rettangolo
                        lines.append((r.x0, r.y0, r.y1))
                    else:                        # cella/riquadro: entrambi i bordi
                        lines.append((r.x0, r.y0, r.y1))
                        lines.append((r.x1, r.y0, r.y1))
            except (AttributeError, IndexError, TypeError):
                continue
    return lines


def _text_right_edge(page, boxes):
    """Bordo destro dell'area di testo della pagina: l'ultimo pixel scritto,
    che approssima il margine impostato dal documento."""
    margin = page.rect.x1 - 2.0
    edges = [b.x1 for b in boxes if b is not None and not b.is_empty]
    return min(max(edges), margin) if edges else margin


def _right_limit(rect, m, boxes, vlines, page_right, pad=0.5):
    """Fin dove si può scrivere senza toccare nulla: il primo carattere
    estraneo sulla stessa riga, il primo bordo verticale, o il margine del
    testo. Mai meno del segnaposto stesso, il cui spazio è comunque libero
    (i suoi glifi sono stati rimossi)."""
    limit = page_right
    band = max(rect.height, 1.0)
    for i, b in enumerate(boxes):
        if b is None or b.is_empty or m.start() <= i < m.end():
            continue
        if b.x0 < rect.x1 - 0.5:
            continue
        if min(rect.y1, b.y1) - max(rect.y0, b.y0) < 0.3 * band:
            continue                            # altra riga
        limit = min(limit, b.x0)
    for x, y0, y1 in vlines:
        if x < rect.x1 - 0.5:
            continue
        if min(rect.y1, y1) - max(rect.y0, y0) < 0.3 * band:
            continue
        limit = min(limit, x)
    return max(limit - pad, rect.x1)


def _plan_write(value, rect, style, limit):
    """Prepara la scrittura di un valore: (testo, punto, font, corpo, colore,
    rotazione, corpo_originale, degradato). None se non si può misurare (font
    sconosciuto): in quel caso il segnaposto NON viene rimosso, meglio un TAG
    dichiarato che un valore perduto.

    Il corpo si riduce quel tanto che serve a stare nello spazio disponibile:
    il PDF non sa rifluire il testo, e allargarsi sopra il carattere successivo
    renderebbe illeggibile la riga altrui invece della propria."""
    origin, span, direction = style if style else (None, {}, (1.0, 0.0))
    text, degraded = _latin1(value)
    if not text.strip():
        return None
    size = float(span.get("size") or 0.0) or max(rect.height / 1.35, 4.0)
    fontname = _base14(span)
    rotate = _rotation(direction)
    try:
        width = fitz.get_text_length(text, fontname=fontname, fontsize=size)
    except Exception:
        return None
    fontsize = size
    if rotate == 0 and width > 0:
        avail = max(limit - (origin[0] if origin else rect.x0), 0.0)
        if width > avail:
            # la lunghezza è lineare nel corpo: il fattore esatto è avail/width
            fontsize = max(math.floor(size * avail / width * 10) / 10,
                           _MIN_FONTSIZE)
    point = (origin[0], origin[1]) if origin else (rect.x0, rect.y1 - 0.22 * size)
    return (text, point, fontname, fontsize, _span_color(span), rotate, size,
            degraded)


@mupdf_serialized
def restore_pdf(pdf_bytes, mapping, media=None):
    """PDF con segnaposto -> PDF con i valori reali (pipeline inversa di
    redact_pdf).

    I box GIALLI della redazione vengono rimossi insieme al segnaposto: il
    loro rettangolo esatto arriva dal marcatore invisibile che redact_pdf
    lascia sulla pagina (_add_markers), e la rimozione usa
    LINE_ART_REMOVE_IF_COVERED, che porta via solo la grafica interamente
    coperta dal rect — il box, e nient'altro (a redigere, la grafica coperta
    era già stata rimossa). Un marcatore si onora solo se CORROBORATO
    dall'etichetta di testo del suo placeholder al suo interno: se il modello
    ha trasformato la pagina senza aggiornare le annotazioni, il rect non è
    più affidabile e si ricade sul comportamento prudente (solo testo,
    giallo lasciato e dichiarato).

    `media` (opzionale, duck-typed: .lookup(bytes, ext) -> bytes | None) è
    il pool delle immagini ripristinabili: ogni immagine dell'output che
    combacia con una versione redatta nota viene sostituita con l'originale.

    mapping: {"[FULLNAME_1]": "Mario Rossi", ...}, il registro della
    conversazione. I segnaposto che non sono nel registro NON vengono toccati:
    restano nel file e finiscono in report["remaining"], perché cancellarli
    senza sapere cosa scriverci significherebbe buttare via del contenuto.

    Ritorna (bytes, report) con report = {
        "restored":     occorrenze riscritte nel testo di pagina,
        "by_placeholder": {segnaposto: n},
        "remaining":    segnaposto ANCORA leggibili nell'output (registro
                        incompleto, oppure testo che non è un layer testuale),
        "lost":         segnaposto rimossi ma non riscritti (deve essere vuoto:
                        è l'unico esito peggiore del non fare niente),
        "shrunk":       [{"ph", "from", "to"}] valori scritti con corpo ridotto
                        per non invadere il testo accanto,
        "degraded":     valori con caratteri fuori Latin-1 approssimati,
        "annots":       sostituzioni nelle annotazioni,
        "widgets":      sostituzioni nei campi modulo,
        "toc":          sostituzioni nei segnalibri,
        "metadata":     sostituzioni nei metadati,
        "images":       immagini presenti nell'output: se il modello ha disegnato
                        un segnaposto DENTRO un raster (etichette di un grafico
                        salvato in png) quello non è testo e non è
                        ripristinabile né rilevabile. Il chiamante lo dichiara.
    }
    """
    if not isinstance(mapping, dict) or not mapping:
        raise PdfError("Nessuna mappatura: niente da ripristinare.")
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        raise PdfError("File PDF non valido o danneggiato.")
    if doc.needs_pass:
        doc.close()
        raise PdfError("PDF protetto da password: impossibile ripristinarlo.")

    def sub(s):
        return sub_placeholders(s, mapping)

    by_ph, shrunk, degraded, lost = {}, [], [], []
    images = {}                     # xref -> primo numero di pagina che lo mostra
    boxed_left = set()              # marcatori noti ma non corroborati dal testo
    total = n_annots = n_widgets = 0
    for pno, page in enumerate(doc):
        markers = _page_markers(page)
        text, boxes, meta = _page_char_index(page, styles=True)
        writes = []
        if "[" in text:
            vlines = None
            page_right = _text_right_edge(page, boxes)
            for m in _PH_BROKEN_RE.finditer(text):
                ph = _BREAK_STRIP_RE.sub("", m.group(0))
                value = mapping.get(ph)
                if value is None or not str(value).strip():
                    continue                    # sconosciuto: si lascia com'è
                rects = [_clamp_side_bleed(_clamp_line_bleed(r, m, boxes),
                                           m, boxes)
                         for r in _match_rects(boxes, m)]
                if not rects:
                    continue                    # nessun glifo: niente da fare
                style = next((meta[i] for i in range(m.start(), m.end())
                              if meta[i] is not None), None)
                if vlines is None:
                    vlines = _page_vlines(page)
                plan = _plan_write(value, rects[0], style,
                                   _right_limit(rects[0], m, boxes, vlines,
                                                page_right))
                if plan is not None:
                    writes.append((ph, rects, plan))
        claimed = []                # marcatori corroborati: box gialli da togliere
        if writes:
            # un marcatore si onora per PLACEHOLDER corroborato sulla pagina:
            # se l'etichetta di [X] cade dentro un suo marcatore, anche gli
            # altri marcatori di [X] sulla pagina sono veri (stesso passaggio
            # di redazione) — sono le strisce di continuazione di un valore a
            # cavallo di riga, che un'etichetta non ce l'hanno mai
            corroborated = set()
            for ph, rects, _plan in writes:
                if any(mk[1] == ph and mk[2].contains(
                        ((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
                       for r in rects for mk in markers):
                    corroborated.add(ph)
            claimed = [mk for mk in markers if mk[1] in corroborated]
            if claimed:
                # PRIMA la grafica, da sola: il rect del marcatore (gonfiato di
                # un pelo: alla scrittura nel PDF viene arrotondato, e la
                # copertura deve restare certa) porta via il box giallo con
                # REMOVE_IF_COVERED e NIENT'ALTRO — text=NONE, così il
                # margine in più non può mangiare i glifi delle righe accanto
                for _a, _ph, r in claimed:
                    grown = fitz.Rect(r.x0 - 0.5, r.y0 - 0.5,
                                      r.x1 + 0.5, r.y1 + 0.5)
                    try:
                        page.add_redact_annot(grown, fill=None, cross_out=False)
                    except TypeError:
                        page.add_redact_annot(grown, fill=None)
                page.apply_redactions(
                    images=fitz.PDF_REDACT_IMAGE_NONE,
                    graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                    text=fitz.PDF_REDACT_TEXT_NONE)
            # POI il testo, coi rect ESATTI dei glifi:
            # fill=None, nessuna casella disegnata, lo sfondo resta quello
            # del documento; immagini e linee intatte, o le redazioni
            # porterebbero via i bordi di tabella che sfiorano
            for _ph, rects, _plan in writes:
                for r in rects:
                    try:
                        page.add_redact_annot(r, fill=None, cross_out=False)
                    except TypeError:
                        page.add_redact_annot(r, fill=None)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                  text=fitz.PDF_REDACT_TEXT_REMOVE)
            for ph, _rects, plan in writes:
                (value, point, fontname, fontsize, color, rotate, size,
                 deg) = plan
                try:
                    page.insert_text(point, value, fontname=fontname,
                                     fontsize=fontsize, color=color,
                                     rotate=rotate)
                except Exception:
                    lost.append(ph)             # glifi rimossi e valore non scritto
                    continue
                by_ph[ph] = by_ph.get(ph, 0) + 1
                total += 1
                if fontsize < size - 0.05:
                    shrunk.append({"ph": ph, "from": round(size, 1),
                                   "to": round(fontsize, 1)})
                if deg and ph not in degraded:
                    degraded.append(ph)
        # via i marcatori consumati; quelli di un placeholder NOTO ma senza
        # etichetta corroborante restano (col loro giallo) e si dichiarano.
        # Si ri-enumera: dopo apply_redactions gli oggetti annot raccolti
        # prima possono essere stantii.
        spent = {(ph, _rect_key(r)) for _a, ph, r in claimed}
        if markers:
            for a, ph, r in _page_markers(page):
                if (ph, _rect_key(r)) in spent:
                    try:
                        page.delete_annot(a)
                    except Exception:
                        pass
                elif ph in mapping:
                    boxed_left.add(ph)
        n_annots += _scrub_annots(page, sub)
        n_widgets += _scrub_widgets(page, sub)
        try:
            # per xref: la stessa immagine ripetuta su più pagine (un logo) è
            # una, e il numero finisce in un avviso all'utente
            for img in page.get_images(full=True):
                images.setdefault(img[0], pno)
        except Exception:
            pass

    swapped = _swap_pdf_images(doc, images, media)
    n_toc = _scrub_toc(doc, sub)
    n_meta = _sub_metadata(doc, sub)
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    report = {
        "restored": total,
        "by_placeholder": by_ph,
        "remaining": sorted(set(_left_placeholders(out)) | boxed_left),
        "lost": sorted(set(lost)),
        "shrunk": shrunk,
        "degraded": degraded,
        "annots": n_annots,
        "widgets": n_widgets,
        "toc": n_toc,
        "metadata": n_meta,
        "images": len(images),
    }
    if swapped:
        report["images_swapped"] = swapped
    return out, report


def _swap_pdf_images(doc, images, media):
    """Le immagini dell'output che combaciano con una versione redatta nota
    (pool `media`) tornano quelle originali. `images` = {xref: pagina}."""
    if media is None or not images:
        return 0
    swapped = 0
    for xref, pno in images.items():
        try:
            info = doc.extract_image(xref)
            new = media.lookup(info["image"], "." + (info.get("ext") or "png"))
            if new is not None:
                doc[pno].replace_image(xref, stream=new)
                swapped += 1
        except Exception:
            continue
    return swapped


def _sub_metadata(doc, sub):
    """Titolo, autore, oggetto e parole chiave: LibreOffice li riempie col
    contenuto del documento, segnaposto compresi. Qui si SOSTITUISCE (non si
    azzera come nella redazione: siamo nella direzione opposta)."""
    try:
        meta = dict(doc.metadata or {})
    except Exception:
        return 0
    done, updated = 0, {}
    for key in ("title", "author", "subject", "keywords"):
        value = meta.get(key)
        if not value:
            continue
        new, hit = sub(value)
        if hit:
            updated[key] = new
            done += hit
    if updated:
        try:
            doc.set_metadata(updated)       # aggiorna SOLO le chiavi passate
        except Exception:
            return 0
    return done


@mupdf_serialized
def _left_placeholders(pdf_bytes):
    """Segnaposto ancora leggibili nell'output: pagine, annotazioni, campi
    modulo, segnalibri. NON vede il testo dentro le immagini raster (vale il
    conteggio report["images"])."""
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            text = _readable_text(doc)
    except Exception:
        return []
    return sorted({_BREAK_STRIP_RE.sub("", m.group(0))
                   for m in _PH_BROKEN_RE.finditer(text)})

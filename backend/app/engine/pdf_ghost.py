"""
Testo FANTASMA dei PDF: la trascrizione invisibile che accompagna le scansioni.

Una scansione "cercabile" (searchable PDF) porta lo STESSO contenuto due volte:
i pixel della pagina, e sopra di essi una trascrizione OCR disegnata in
**text rendering mode 3** — né riempita né contornata, quindi invisibile a
schermo ma presente nel layer testuale e nel copia-incolla. La producono tutti:
Acrobat, ABBYY, OCRmyPDF/Tesseract e le utility che accompagnano gli scanner
da tavolo (Canon IJ Scan Utility, Epson Scan e simili).

Per l'anonimizzazione quel doppione fa danni su tre livelli:

  1. la stessa frase viene analizzata due volte, con due letture diverse — la
     nostra OCR sui pixel e quella dello scanner. Il dedup di `core.analyze` è
     su (label, valore normalizzato), quindi 'Acme Forniture SRL' e
     'Acme Fornlture' NON collassano: due placeholder, due redazioni impilate
     sullo stesso punto, registro PII sporco di valori storpiati;
  2. il testo fantasma **sopravvive alla redazione**: `pdf_export` cerca i
     valori della mappa alla lettera, e le stringhe storpiate non combaciano.
     Restano nel file anonimizzato, estraibili con un copia-incolla, e nemmeno
     `_verify_residuals` le vede (cerca gli stessi valori esatti);
  3. con l'OCR spento è peggio: la redazione segue i box dei glifi storpiati e
     produce un PDF che SEMBRA anonimizzato mentre la scansione resta leggibile.

La regola qui è PER SPAN, non per pagina, e non ha soglie sulla quantità di
testo. Uno span è fantasma quando ricorrono tutte e tre:

  a. è disegnato INVISIBILE (`get_texttrace()` -> type == 3). Nessuno scrive
     testo invisibile perché un umano lo legga: esiste solo per trascrivere
     dei pixel;
  b. sta per almeno ON_IMAGE dentro il bbox di un'immagine: quei pixel ci sono,
     e l'OCR dell'app li legge comunque. Il fantasma è un doppione di quella
     lettura;
  c. non interseca NESSUNO span visibile: la rimozione è geometrica
     (apply_redactions porta via ogni glifo che il rettangolo tocca), quindi un
     fantasma appiccicato a del testo vero si lascia stare.

Il testo VISIBILE non viene toccato mai, in nessun caso: un PDF che va letto
come testo è per definizione un PDF il cui testo si vede, e non entra
nemmeno in gioco. Una pagina MISTA (documento di testo con dentro una
scansione incollata che si porta la propria trascrizione) è gestita
correttamente proprio perché la decisione è per regione: via il fantasma
sopra la scansione, intatto tutto il resto.

Sbagliando, si sbaglia rimuovendo TROPPO POCO: resta il doppione, che è il
male minore. Non si può sbagliare rimuovendo troppo.

Perché non una regola PER PAGINA con delle soglie ("almeno il 95% del testo
è invisibile", "le immagini coprono metà pagina"): su una scansione con
poco testo basta un numero di pagina o un timbro di protocollo stampati sopra
per far saltare la proporzione, e la regola non scatta. Le soglie sulla
QUANTITÀ di testo non tengono; "invisibile AND sopra pixel" sì.
"""

import fitz  # PyMuPDF

from .mupdf_lock import mupdf_serialized

# PDF text rendering mode 3: "neither fill nor stroke" (PDF 32000-1, 9.3.6)
INVISIBLE = 3

# quanto di uno span deve stare dentro l'immagine per dirlo "sopra i pixel":
# non 1.0 perché i bbox dei glifi sbordano di frazioni di punto dal bordo
# dell'immagine, non troppo basso perché una riga di testo vero che passa
# accanto a una figura non diventi un fantasma
ON_IMAGE = 0.6


def _spans(page):
    """(span invisibili, span visibili) come [(rect, n_caratteri)]."""
    inv, vis = [], []
    for sp in page.get_texttrace():
        r = fitz.Rect(sp["bbox"])
        if r.is_empty or r.is_infinite:
            continue
        n = len(sp.get("chars", ()))
        (inv if sp.get("type") == INVISIBLE else vis).append((r, n))
    return inv, vis


def _image_rects(page):
    out = []
    for i in page.get_image_info():
        r = fitz.Rect(i["bbox"])
        if not r.is_empty and not r.is_infinite:
            out.append(r)
    return out


def on_ghost(rect, ghosts):
    """`rect` cade dove è disegnato del testo fantasma?

    NON si usa pdf_export._covered: la sua soglia (85% dell'area dentro) è
    tarata su "questo valore è già stato redatto" e qui sbaglia, perché i
    due rettangoli arrivano da fonti con metriche diverse — i box di
    get_text("words") sono alti quanto la riga (ascender/discender del font),
    quelli di get_texttrace() quanto i glifi realmente disegnati. Si sfalsano
    di qualche punto in verticale e una parola interamente fantasma copriva
    ~79% del suo box: sotto soglia, quindi non riconosciuta.

    Il criterio giusto qui è il CENTRO dentro il rettangolo (immune allo
    sfasamento verticale), con la sovrapposizione a metà come seconda
    possibilità per le parole a cavallo. Allargare non è rischioso: i
    rettangoli fantasma, per costruzione, non intersecano NESSUNO span
    visibile (condizione c), quindi una parola visibile non può finirci
    dentro."""
    if not ghosts:
        return False
    cx, cy = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
    area = rect.get_area() or 1.0
    for g in ghosts:
        if g.contains(fitz.Point(cx, cy)):
            return True
        if (rect & g).get_area() >= 0.5 * area:
            return True
    return False


def ghost_rects(page):
    """(rettangoli del testo fantasma, n_caratteri) su una pagina APERTA.

    Il chiamante è già dentro il lock MuPDF (ha una pagina in mano).
    Come strip_ghost_text non solleva: su una pagina che MuPDF non riesce a
    interrogare si risponde "nessun fantasma" e il resto del percorso non
    cambia comportamento."""
    try:
        # gate a monte: senza immagini nessun fantasma è possibile (condizione
        # b), e get_texttrace su una pagina fitta di tracciati vettoriali costa
        # centinaia di ms — con questo controllo non la si paga mai a vuoto
        if not page.get_images():
            return [], 0
        return _ghost_rects(page)
    except Exception:
        return [], 0


def _ghost_rects(page):
    inv, vis = _spans(page)
    if not inv:
        return [], 0
    images = _image_rects(page)
    if not images:
        return [], 0
    out, n_chars = [], 0
    for r, n in inv:
        area = r.get_area() or 1.0
        if not any((r & im).get_area() >= ON_IMAGE * area for im in images):
            continue                                   # non sta sopra i pixel
        if any((r & vr).get_area() > 0 for vr, _ in vis):
            continue                                   # sfiora testo visibile
        out.append(r)
        n_chars += n
    return out, n_chars


@mupdf_serialized
def strip_ghost_text(pdf_bytes):
    """Toglie il testo fantasma. -> (bytes, {pagina: n_caratteri}).

    Se non ce n'è, ritorna l'OGGETTO bytes ricevuto, non una copia: i
    chiamanti possono usare `out is pdf_bytes` per sapere che il file non è
    stato toccato, e un PDF senza fantasmi non viene nemmeno riscritto.

    Si rimuove SOLO il testo: `PDF_REDACT_IMAGE_NONE` +
    `PDF_REDACT_LINE_ART_NONE` lasciano i pixel della scansione e i tracciati
    esattamente com'erano (stessa scelta di pdf_export per i bordi di
    tabella), quindi la pagina resa è identica prima e dopo.

    ATTENZIONE ALL'ORDINE: `tobytes(garbage=3)` può RINUMERARE gli xref, e la
    cache OCR è indicizzata per xref (`image_ocr.pdf_images`). Quando le due
    cose convivono lo strip va fatto DOPO la redazione delle immagini, mai
    prima — vedi `pdf.rebuild_pdf`.

    Non solleva MAI: è un passo di normalizzazione, e sta a monte di codice
    che i file illeggibili sa già raccontarli bene (PDF corrotto, protetto da
    password). Su qualunque intoppo ritorna i byte ricevuti e si fa da parte,
    così l'errore lo produce chi ha il messaggio giusto invece di un'eccezione
    grezza da qui.
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return pdf_bytes, {}
    touched = {}
    try:
        if doc.needs_pass:            # cifrato: le pagine non si leggono
            return pdf_bytes, {}
        for pno, page in enumerate(doc):
            rects, n = ghost_rects(page)
            if not rects:
                continue
            for r in rects:
                try:
                    page.add_redact_annot(r, fill=None, cross_out=False)
                except TypeError:                      # PyMuPDF più vecchi
                    page.add_redact_annot(r, fill=None)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                  text=fitz.PDF_REDACT_TEXT_REMOVE)
            touched[pno] = n
        if not touched:
            return pdf_bytes, {}
        return doc.tobytes(garbage=3, deflate=True), touched
    except Exception:
        return pdf_bytes, {}
    finally:
        doc.close()


@mupdf_serialized
def ghost_report(pdf_bytes):
    """Diagnosi senza modificare nulla: [{page, inv_chars, vis_chars,
    ghost_spans, ghost_chars}]. Serve ai test e alla diagnostica."""
    rows = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for pno, page in enumerate(doc):
            # qui _spans si chiama SEMPRE, senza il gate sulle immagini: è
            # diagnostica e deve dire la verità anche su una pagina di solo
            # testo (dove il conteggio dei visibili è l'informazione utile)
            inv, vis = _spans(page)
            rects, n = ghost_rects(page)
            rows.append({"page": pno,
                         "inv_chars": sum(c for _, c in inv),
                         "vis_chars": sum(c for _, c in vis),
                         "ghost_spans": len(rects),
                         "ghost_chars": n})
    return rows

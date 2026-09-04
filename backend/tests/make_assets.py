"""Genera i documenti di prova di backend/tests/assets/.

I file prodotti qui sono SINTETICI: ogni nome, indirizzo, email, IBAN e partita
IVA è inventato. Nessun documento reale entra nel repository — un'app che
anonimizza dati personali non può spedire dati personali nei propri test.

I codici che hanno un checksum (partita IVA, IBAN) sono calcolati, non scritti
a mano: i rilevatori li validano davvero, e un codice con checksum sbagliato
verrebbe scartato o marcato diversamente, cambiando ciò che i test misurano.

I file sono VERSIONATI: chi clona il repository non deve rigenerarli, né
installare le dipendenze di questo script. Serve solo a chi li rifà.

Uso:
    pip install -r backend/requirements-dev.txt
    python backend/tests/make_assets.py
"""

import io
import math
import shutil
import zipfile
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFont
from docx import Document
from docx.shared import Pt
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches, Pt as PptPt

ASSETS = Path(__file__).resolve().parent / "assets"

# --------------------------------------------------------------------------- #
# Identità inventate, usate in TUTTI i documenti: lo stesso valore va
# riconosciuto uguale in formati diversi, ed è quello che i test verificano.
# --------------------------------------------------------------------------- #
ORG = "Acme Forniture SRL"
ORG_SHORT = "Acme Forniture"
ORG_ALT = "Global Trade SPA"
PERSON = "Luigi Verdi"
PERSON_2 = "Mario Rossi"
PERSON_3 = "Anna Bianchi"
ADDRESS = "Via Roma 10, 20121 Milano (MI)"
EMAIL = "mario.rossi@esempio.it"
EMAIL_2 = "anna.bianchi@esempio.it"
PHONE = "+39 02 1234567"
MOBILE = "+39 335 1234567"
CF = "RSSMRA85M01H501Z"          # codice fiscale d'esempio, checksum valido


def _vat_it(base):
    """Partita IVA italiana: 10 cifre + cifra di controllo (algoritmo di Luhn
    a pesi 1/2 sulle posizioni pari, complemento a 10)."""
    assert len(base) == 10 and base.isdigit()
    total = 0
    for i, ch in enumerate(base):
        n = int(ch)
        if i % 2:                      # posizioni pari (1-based): raddoppia
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return base + str((10 - total % 10) % 10)


def _iban_it(bban):
    """IBAN italiano: IT + 2 cifre di controllo mod-97 + BBAN (23 caratteri)."""
    assert len(bban) == 23
    rearranged = bban + "IT00"
    digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    return "IT%02d%s" % (98 - int(digits) % 97, bban)


VAT = _vat_it("0123456789")
VAT_ALT = _vat_it("9876543210")
IBAN = _iban_it("X0542811101000000123456".replace("X", "A"))


# --------------------------------------------------------------------------- #
# Testi condivisi
# --------------------------------------------------------------------------- #
CONTRACT_HEAD = "CONTRATTO DI FORNITURA"
CONTRACT_INTRO = (
    f"Tra {ORG}, con sede in {ADDRESS}, partita IVA {VAT}, in persona del "
    f"legale rappresentante {PERSON}, di seguito «il Fornitore», e "
    f"{ORG_ALT}, partita IVA {VAT_ALT}, di seguito «il Cliente»."
)
CONTRACT_CLAUSES = [
    ("Oggetto", "Il Fornitore si impegna a fornire i beni indicati "
                "nell'allegato tecnico, alle condizioni qui stabilite."),
    ("Durata", "Il presente accordo entra in vigore alla data della firma e "
               "resta valido ventiquattro mesi, rinnovabile per iscritto."),
    ("Corrispettivo", f"Il corrispettivo è fissato in euro 48.500,00 annui, "
                      f"da versare sul conto {IBAN} intestato al Fornitore."),
    ("Riservatezza", "Le parti si obbligano a non divulgare a terzi le "
                     "informazioni tecniche e commerciali scambiate."),
    ("Referenti", f"Per il Fornitore: {PERSON}, {EMAIL}, tel. {PHONE}. "
                  f"Per il Cliente: {PERSON_2}, {EMAIL_2}, cell. {MOBILE}."),
    ("Foro competente", "Per ogni controversia è competente in via esclusiva "
                        "il foro di Milano."),
]

LETTER = f"""Milano, 12 marzo

Spett.le {ORG_ALT}
{ADDRESS}

Oggetto: conferma d'ordine e dati di fatturazione

Con la presente confermiamo l'ordine ricevuto in data odierna. La fatturazione
avverrà a nome di {ORG}, partita IVA {VAT}, codice fiscale del legale
rappresentante {CF}.

Il pagamento va disposto sul conto {IBAN}.

Per ogni chiarimento resta a disposizione il nostro referente {PERSON},
raggiungibile all'indirizzo {EMAIL} oppure al numero {PHONE}.

Distinti saluti
{PERSON}
{ORG}
"""

# righe della tabella clienti: (Nome, Email, Telefono, Città, PIVA, IBAN, Note)
CLIENTS = [
    (PERSON_2, EMAIL, PHONE, "Milano", VAT, IBAN, "cliente storico"),
    (PERSON_3, EMAIL_2, MOBILE, "Torino", VAT_ALT, IBAN, "nuovo contratto"),
    (PERSON, "luigi.verdi@esempio.it", "+39 06 7654321", "Roma", VAT,
     IBAN, "referente tecnico"),
    ("Giulia Neri", "giulia.neri@esempio.it", "+39 011 2233445", "Torino",
     VAT_ALT, IBAN, ""),
    ("Paolo Gialli", "paolo.gialli@esempio.it", "+39 051 998877", "Bologna",
     VAT, IBAN, "fatturazione differita"),
    ("Chiara Blu", "chiara.blu@esempio.it", "+39 081 445566", "Napoli",
     VAT_ALT, IBAN, ""),
    ("Marco Grigi", "marco.grigi@esempio.it", "+39 045 112233", "Verona",
     VAT, IBAN, "sconto quantità"),
    ("Sara Viola", "sara.viola@esempio.it", "+39 055 667788", "Firenze",
     VAT_ALT, IBAN, ""),
    ("Davide Rosa", "davide.rosa@esempio.it", "+39 010 334455", "Genova",
     VAT, IBAN, "pagamento anticipato"),
    ("Elena Verde", "elena.verde@esempio.it", "+39 049 556677", "Padova",
     VAT_ALT, IBAN, "referente acquisti"),
]
# 10 clienti + la riga di intestazione = 11 righe nel foglio "Clienti"



# --------------------------------------------------------------------------- #
# docProps realistici
#
# python-docx, openpyxl e python-pptx scrivono metadati minimi: nessun
# <Company>, nessun <Manager>, nessun titolo di parte, date al momento della
# generazione. Sono esattamente le superfici che lo scrubber deve svuotare, e
# una fixture che non le ha non mette alla prova niente. Qui i docProps
# vengono riscritti nella forma che scrive Office in un'installazione
# aziendale, con date FISSE (i file sono versionati: rigenerarli non deve
# produrre un diff).
# --------------------------------------------------------------------------- #
CREATED = "2024-01-15T09:30:00Z"
MODIFIED = "2024-03-02T16:45:00Z"

_CORE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<cp:coreProperties '
    'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
    'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:dcterms="http://purl.org/dc/terms/" '
    'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<dc:title>%(title)s</dc:title>'
    '<dc:subject>Fornitura</dc:subject>'
    '<dc:creator>%(author)s</dc:creator>'
    '<cp:keywords>%(keywords)s</cp:keywords>'
    '<dc:description>Bozza rivista da %(reviewer)s</dc:description>'
    '<cp:lastModifiedBy>%(reviewer)s</cp:lastModifiedBy>'
    '<cp:revision>4</cp:revision>'
    '<dcterms:created xsi:type="dcterms:W3CDTF">' + CREATED + '</dcterms:created>'
    '<dcterms:modified xsi:type="dcterms:W3CDTF">' + MODIFIED + '</dcterms:modified>'
    '<cp:category>%(category)s</cp:category>'
    '</cp:coreProperties>'
)

# Word: <TitlesOfParts> e <Company> ADIACENTI, come li scrive Word, e nessun
# <Manager> (Word lo omette quando è vuoto).
_APP_WORD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/'
    '2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/'
    'officeDocument/2006/docPropsVTypes">'
    '<Template>Normal.dotm</Template><TotalTime>37</TotalTime>'
    '<Pages>4</Pages><Words>1180</Words><Characters>6890</Characters>'
    '<Application>Microsoft Office Word</Application>'
    '<DocSecurity>0</DocSecurity><Lines>57</Lines><Paragraphs>16</Paragraphs>'
    '<ScaleCrop>false</ScaleCrop>'
    '<HeadingPairs><vt:vector size="2" baseType="variant">'
    '<vt:variant><vt:lpstr>Titolo</vt:lpstr></vt:variant>'
    '<vt:variant><vt:i4>1</vt:i4></vt:variant>'
    '</vt:vector></HeadingPairs>'
    '<TitlesOfParts><vt:vector size="1" baseType="lpstr">'
    '<vt:lpstr>%(title)s</vt:lpstr></vt:vector></TitlesOfParts>'
    '<Company></Company>'
    '<LinksUpToDate>false</LinksUpToDate>'
    '<CharactersWithSpaces>8050</CharactersWithSpaces>'
    '<SharedDoc>false</SharedDoc><HyperlinksChanged>false</HyperlinksChanged>'
    '<AppVersion>16.0000</AppVersion></Properties>'
)

_APP_PPT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/'
    '2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/'
    'officeDocument/2006/docPropsVTypes">'
    '<TotalTime>18</TotalTime><Words>142</Words>'
    '<Application>Microsoft Office PowerPoint</Application>'
    '<PresentationFormat>Widescreen</PresentationFormat>'
    '<Paragraphs>21</Paragraphs><Slides>3</Slides><Notes>2</Notes>'
    '<HiddenSlides>0</HiddenSlides>'
    '<TitlesOfParts><vt:vector size="3" baseType="lpstr">'
    '<vt:lpstr>Offerta commerciale</vt:lpstr>'
    '<vt:lpstr>Condizioni economiche</vt:lpstr>'
    '<vt:lpstr>Contatti</vt:lpstr></vt:vector></TitlesOfParts>'
    '<Company></Company>'
    '<LinksUpToDate>false</LinksUpToDate>'
    '<SharedDoc>false</SharedDoc><HyperlinksChanged>false</HyperlinksChanged>'
    '<AppVersion>16.0000</AppVersion></Properties>'
)

_APP_XL = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/'
    '2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/'
    'officeDocument/2006/docPropsVTypes">'
    '<Application>Microsoft Excel</Application>'
    '<DocSecurity>0</DocSecurity><ScaleCrop>false</ScaleCrop>'
    '<Company></Company>'
    '<LinksUpToDate>false</LinksUpToDate><SharedDoc>false</SharedDoc>'
    '<HyperlinksChanged>false</HyperlinksChanged>'
    '<AppVersion>16.0300</AppVersion></Properties>'
)

_APP_BY_EXT = {".docx": _APP_WORD, ".pptx": _APP_PPT, ".xlsx": _APP_XL}


def patch_docprops(path, title):
    """Riscrive docProps/core.xml e docProps/app.xml del pacchetto OOXML."""
    fields = {"title": title, "author": PERSON, "reviewer": PERSON_2,
              "keywords": f"{VAT}, {EMAIL}", "category": PERSON_3}
    core = _CORE % fields
    app = _APP_BY_EXT[path.suffix.lower()] % fields
    src = zipfile.ZipFile(path)
    items = [(i, src.read(i.filename)) for i in src.infolist()]
    src.close()
    seen = set()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in items:
            if info.filename == "docProps/core.xml":
                data = core.encode("utf-8")
            elif info.filename == "docProps/app.xml":
                data = app.encode("utf-8")
            seen.add(info.filename)
            out.writestr(info, data)
        for name, body in (("docProps/core.xml", core),
                          ("docProps/app.xml", app)):
            if name not in seen:
                out.writestr(name, body)
    print(f"  {path.name}: docProps riscritti (autore {PERSON}, "
          f"creato {CREATED[:10]})")


# --------------------------------------------------------------------------- #
# Word
# --------------------------------------------------------------------------- #
def make_docx(path, n_paragraphs=138):
    """Contratto con esattamente `n_paragraphs` paragrafi: il numero è ciò
    che la scheda dell'allegato dichiara al modello, quindi è fissato."""
    doc = Document()
    doc.core_properties.author = PERSON
    doc.core_properties.company = ORG
    doc.core_properties.title = CONTRACT_HEAD
    doc.core_properties.comments = f"Bozza rivista da {PERSON_2}"

    doc.add_heading(CONTRACT_HEAD, level=0)          # 1
    doc.add_paragraph(CONTRACT_INTRO)                # 2
    written = 2
    i = 0
    while written < n_paragraphs:
        title, body = CONTRACT_CLAUSES[i % len(CONTRACT_CLAUSES)]
        n = i // len(CONTRACT_CLAUSES) + 1
        if written + 2 > n_paragraphs:
            doc.add_paragraph(body)
            written += 1
            break
        doc.add_heading(f"Art. {n} — {title}", level=1)
        doc.add_paragraph(body)
        written += 2
        i += 1

    # intestazione e piè di pagina: superfici che la redazione deve coprire
    sec = doc.sections[0]
    sec.header.paragraphs[0].text = f"{ORG} — {ADDRESS}"
    sec.footer.paragraphs[0].text = f"P.IVA {VAT} · {EMAIL}"
    for p in doc.paragraphs:
        for run in p.runs:
            if run.font.size is None:
                run.font.size = Pt(11)
    doc.save(str(path))

    got = len(Document(str(path)).paragraphs)
    print(f"  {path.name}: {got} paragrafi")
    patch_docprops(path, CONTRACT_HEAD)
    return got


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #
def make_xlsx_multisheet(path):
    """Cinque fogli di natura diversa: la scheda dell'allegato li elenca, e la
    redazione deve attraversare celle, note e testi non tabellari."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Clienti"
    ws.append(["Nome", "Email", "Telefono", "Città", "P.IVA", "IBAN", "Note"])
    for row in CLIENTS:
        ws.append(list(row))

    ws2 = wb.create_sheet("Fornitori")
    ws2.append(["Ragione sociale", "Referente", "Email", "P.IVA"])
    for name, ref, mail in ((ORG, PERSON, EMAIL),
                            (ORG_ALT, PERSON_2, EMAIL_2),
                            ("Beta Servizi SNC", PERSON_3, "info@esempio.it")):
        ws2.append([name, ref, mail, VAT])

    ws3 = wb.create_sheet("Fatture")
    ws3.append(["Numero", "Data", "Cliente", "Importo", "Stato"])
    for n in range(1, 26):
        ws3.append([f"2026/{n:04d}", f"2026-01-{(n % 28) + 1:02d}",
                    CLIENTS[n % len(CLIENTS)][0], 1200.50 + n * 37, "emessa"])

    ws4 = wb.create_sheet("Riepilogo")
    ws4["A1"] = f"Riepilogo annuale di {ORG}"
    ws4["A3"] = "Totale fatturato"
    ws4["B3"] = "=SUM(Fatture!D2:D26)"
    ws4["A5"] = f"Referente: {PERSON} ({EMAIL})"

    ws5 = wb.create_sheet("Note")
    ws5["A1"] = "Foglio non tabellare"
    ws5["A3"] = (f"Il conto {IBAN} è intestato a {ORG}. Per variazioni "
                 f"scrivere a {EMAIL}.")
    wb.save(str(path))
    print(f"  {path.name}: {len(wb.sheetnames)} fogli {wb.sheetnames}")
    patch_docprops(path, "Anagrafiche")


def make_xlsx_table(path, n_rows, n_cols=22):
    """Tabella regolare larga e alta: serve a due cose diverse — la scheda che
    dichiara le dimensioni senza leggere tutto il foglio, e l'anonimizzazione
    per COLONNA (una colonna di codici che il modello non riconoscerebbe)."""
    # NON write_only: quella modalità non scrive <dimension> nel foglio, e
    # la scheda dell'allegato dichiarerebbe "almeno N righe" invece delle
    # dimensioni esatte — che è proprio ciò che si vuole poter leggere
    # senza scorrere il foglio.
    wb = Workbook()
    ws = wb.active
    ws.title = "Dati"
    head = (["Codice", "Cliente", "Email", "Telefono", "P.IVA", "IBAN",
             "Città", "Indirizzo", "Referente"] +
            [f"Valore {i}" for i in range(1, n_cols - 8)])
    ws.append(head[:n_cols])
    for r in range(n_rows):
        c = CLIENTS[r % len(CLIENTS)]
        row = ([f"CLI-{r + 1:06d}", c[0], c[1], c[2], c[4], c[5], c[3],
                ADDRESS, PERSON] +
               [round(100 + r * 1.5 + i, 2) for i in range(n_cols - 9)])
        ws.append(row[:n_cols])
    wb.save(str(path))
    print(f"  {path.name}: {n_rows} righe x {min(len(head), n_cols)} colonne")
    patch_docprops(path, "Estrazione clienti")


def make_xlsx_listino(path):
    """Listino con una ETICHETTA di colonna che il modello tagga come PII
    ("Marchio" letto come cognome sulla riga di intestazione nuda) e, nelle
    celle sotto, il dato vero da coprire. È il caso delle intestazioni da
    tenere in chiaro: redigere l'etichetta romperebbe il foglio, redigere le
    celle è obbligatorio."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Listino"
    ws.append(["Codice a barre", "SKU", "Marchio", "Modello"])
    brands = ("Vistaclear", "Nordluce", "Vistaclear", "Aurea", "Nordluce")
    for i, brand in enumerate(brands, 1):
        ws.append([f"80{i:011d}", f"SKU-{i:04d}", brand, f"Serie {i}0"])
    wb.save(str(path))
    print(f"  {path.name}: intestazione {[c.value for c in ws[1]]}")
    patch_docprops(path, "Listino")


# --------------------------------------------------------------------------- #
# PowerPoint
# --------------------------------------------------------------------------- #
def make_pptx(path):
    """Tre diapositive, con note del relatore: le note sono una superficie che
    si dimentica facilmente e che contiene PII quanto le slide."""
    prs = Presentation()
    prs.core_properties.author = PERSON
    prs.core_properties.company = ORG

    s1 = prs.slides.add_slide(prs.slide_layouts[0])
    s1.shapes.title.text = "Offerta commerciale"
    s1.placeholders[1].text = f"{ORG} — {PERSON}"
    s1.notes_slide.notes_text_frame.text = (
        f"Presenta {PERSON}, referente {EMAIL}, tel. {PHONE}.")

    s2 = prs.slides.add_slide(prs.slide_layouts[1])
    s2.shapes.title.text = "Condizioni economiche"
    body = s2.placeholders[1].text_frame
    body.text = "Corrispettivo annuo: euro 48.500,00"
    for line in (f"Pagamento sul conto {IBAN}",
                 f"Fatturazione a {ORG_ALT}, P.IVA {VAT_ALT}",
                 "Durata: ventiquattro mesi"):
        body.add_paragraph().text = line
    s2.notes_slide.notes_text_frame.text = (
        f"Se chiedono uno sconto, decide {PERSON_2} ({EMAIL_2}).")

    s3 = prs.slides.add_slide(prs.slide_layouts[5])
    s3.shapes.title.text = "Contatti"
    box = s3.shapes.add_textbox(Inches(1), Inches(2.5), Inches(8), Inches(2))
    tf = box.text_frame
    tf.text = f"{PERSON} — {EMAIL} — {PHONE}"
    p = tf.add_paragraph()
    p.text = f"{ORG}, {ADDRESS}"
    p.font.size = PptPt(16)

    prs.save(str(path))
    print(f"  {path.name}: {len(prs.slides._sldIdLst)} diapositive")
    patch_docprops(path, "Offerta commerciale")
    _add_comment_authors(path)



# Autori dei commenti: due parti che PowerPoint scrive e che python-pptx non
# sa produrre. Sono superfici di metadati con dentro nomi e indirizzi, cioè
# esattamente ciò che lo scrubber deve ripulire — un pptx senza queste parti
# non mette alla prova niente.
_AUTHORS_MODERN = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p188:authorLst xmlns:p188="http://schemas.microsoft.com/office/'
    'powerpoint/2018/8/main">'
    '<p188:author id="{6E2A8B41-0000-4000-8000-000000000001}" '
    'name="%s" initials="AB" userId="S::%s::" providerId="AD"/>'
    '</p188:authorLst>' % (PERSON_3, EMAIL_2)
)
_AUTHORS_LEGACY = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:cmAuthorLst xmlns:p="http://schemas.openxmlformats.org/'
    'presentationml/2006/main">'
    '<p:cmAuthor id="1" name="%s" initials="MR" lastIdx="1" clrIdx="0"/>'
    '</p:cmAuthorLst>' % PERSON_2
)
_CT_MODERN = ('<Override PartName="/ppt/authors.xml" ContentType='
              '"application/vnd.ms-powerpoint.authors+xml"/>')
_CT_LEGACY = ('<Override PartName="/ppt/commentAuthors.xml" ContentType='
              '"application/vnd.openxmlformats-officedocument.presentationml.'
              'commentAuthors+xml"/>')
_REL_MODERN = ('<Relationship Id="rIdAuthors" Type="http://schemas.microsoft.'
               'com/office/2018/10/relationships/authors" '
               'Target="authors.xml"/>')
_REL_LEGACY = ('<Relationship Id="rIdCmAuthors" Type="http://schemas.'
               'openxmlformats.org/officeDocument/2006/relationships/'
               'commentAuthors" Target="commentAuthors.xml"/>')


def _add_comment_authors(path):
    """Riscrive il .pptx aggiungendo ppt/authors.xml e ppt/commentAuthors.xml,
    con le rispettive dichiarazioni in [Content_Types].xml e la relazione da
    presentation.xml.rels."""
    src = zipfile.ZipFile(path)
    items = [(i, src.read(i.filename)) for i in src.infolist()]
    src.close()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in items:
            name = info.filename
            if name == "[Content_Types].xml":
                text = data.decode("utf-8")
                data = text.replace("</Types>", _CT_MODERN + _CT_LEGACY +
                                    "</Types>").encode("utf-8")
            elif name == "ppt/_rels/presentation.xml.rels":
                text = data.decode("utf-8")
                data = text.replace("</Relationships>",
                                    _REL_MODERN + _REL_LEGACY +
                                    "</Relationships>").encode("utf-8")
            out.writestr(info, data)
        out.writestr("ppt/authors.xml", _AUTHORS_MODERN)
        out.writestr("ppt/commentAuthors.xml", _AUTHORS_LEGACY)
    print(f"  {path.name}: aggiunti gli autori dei commenti "
          f"(authors.xml + commentAuthors.xml)")


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def _contract_text():
    parts = [CONTRACT_HEAD.upper(), "", CONTRACT_INTRO, ""]
    for n, (title, body) in enumerate(CONTRACT_CLAUSES, 1):
        parts += [f"Art. {n} - {title}", body, ""]
    return "\n".join(parts)


def make_pdf_text(path, pages=2):
    """PDF nativo: il testo è testo, la redazione lo toglierà dal content
    stream. Prima riga in maiuscolo per la scheda dell'allegato."""
    doc = fitz.open()
    body = _contract_text()
    for i in range(pages):
        page = doc.new_page()
        head = f"{ORG.upper()}\n\n" if i == 0 else f"{ORG.upper()} - pag. {i + 1}\n\n"
        page.insert_textbox(fitz.Rect(50, 50, 545, 780), head + body,
                            fontsize=10.5)
    doc.set_metadata({"title": CONTRACT_HEAD, "author": PERSON,
                      "subject": f"Fornitura {ORG_ALT}",
                      "keywords": f"{VAT}, {EMAIL}"})
    doc.save(str(path), **_SAVE)
    doc.close()
    print(f"  {path.name}: {pages} pagine")


def make_pdf_metadata(path):
    """PDF con i metadati pieni: è il caso della marcatura AI e della pulizia
    dei metadati, dove il Subject sopravvive al testo se nessuno lo tocca."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(50, 50, 545, 400),
                        f"Nota interna\n\n{PERSON} - {EMAIL}\n{ORG}\n"
                        f"P.IVA {VAT}", fontsize=12)
    doc.set_metadata({"title": "Nota interna", "author": PERSON,
                      "subject": f"Riservato - {ORG}",
                      "keywords": f"{PERSON}, {EMAIL}",
                      "creator": "Generatore di prova",
                      "producer": "Generatore di prova"})
    doc.save(str(path), **_SAVE)
    doc.close()
    print(f"  {path.name}: metadati completi")


def _page_png(text, dpi=150, size=(595, 842)):
    """Una pagina RESA A PIXEL: è la materia prima delle finte scansioni.
    In scala di grigio come una scansione vera — e un decimo dei byte."""
    doc = fitz.open()
    page = doc.new_page(width=size[0], height=size[1])
    page.insert_textbox(fitz.Rect(40, 40, size[0] - 40, size[1] - 40), text,
                        fontsize=11)
    png = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY).tobytes("png")
    doc.close()
    return png


def _ink(draw, x0, y0, w, h, seed, width):
    """Una firma DISEGNATA: seni sovrapposti, un occhiello e uno svolazzo.
    Non è la grafia di nessuno — nel repository non entrano firme vere — ma ha
    la forma che il detector di firme/timbri cerca: tratto continuo, curvo,
    non allineato a una riga di testo."""
    pts = []
    for i in range(260):
        t = i / 259
        pts.append((x0 + t * w,
                    y0 + h / 2
                    + math.sin(t * 7.5 + seed) * h * 0.34
                    + math.sin(t * 19 + seed * 2) * h * 0.10
                    - t * h * 0.12))
    draw.line(pts, fill=40, width=width, joint="curve")
    loop = [(x0 + w * 0.07 + math.cos(a / 89 * 2 * math.pi) * w * 0.05,
             y0 + h * 0.55 + math.sin(a / 89 * 2 * math.pi) * h * 0.20)
            for a in range(90)]
    draw.line(loop, fill=40, width=width, joint="curve")
    draw.line([(x0 + w * 0.75, y0 + h * 0.62), (x0 + w, y0 + h * 0.15),
               (x0 + w * 0.62, y0 + h * 0.30)], fill=40, width=width)


def _illegible(img, box, text):
    """Una riga che l'OCR vede ma NON riesce a leggere: il testo è schiacciato
    in orizzontale come in una fotocopia stirata. Serve al placeholder
    [UNREADABLE_n], la politica per cui una riga illeggibile si copre per
    intero invece di dichiararla priva di PII. Lo schiacciamento degrada la
    confidenza in modo graduale (~0.65 contro una soglia di 0.80): una
    sfocatura, invece, passa in pochi punti da «letto benissimo» a «non letto
    affatto», e un asset appeso a quel crinale si romperebbe al primo
    aggiornamento del motore OCR."""
    x, y, w, h = box
    patch = Image.new("L", (w, h), 255)
    try:
        f = ImageFont.truetype("arial.ttf", int(h * 0.62))
    except OSError:
        f = ImageFont.load_default()
    ImageDraw.Draw(patch).text((4, int(h * 0.15)), text, fill=0, font=f)
    patch = patch.resize((max(1, int(w * 0.32)), h), Image.BICUBIC)
    img.paste(patch, (x, y))


def _signature_page_png(text, size=(495, 620), dpi=150):
    """Come _page_png, ma la pagina porta anche DUE firme disegnate e una riga
    illeggibile. È la materia prima del foglio firme: il detector deve trovare
    le due grafie, e la riga schiacciata deve finire sotto [UNREADABLE_n]."""
    base = _page_png(text, dpi=dpi, size=size)
    img = Image.open(io.BytesIO(base)).convert("L")
    d = ImageDraw.Draw(img)
    w, h = img.size
    # tutto SOTTO il blocco di testo (che finisce intorno a 0.47 dell'altezza):
    # una firma sopra una riga di testo ne abbassa la lettura, e qui i nomi e
    # gli indirizzi devono restare leggibili — sono loro le entità da coprire.
    _ink(d, w * 0.10, h * 0.53, w * 0.34, h * 0.10, 0.4, max(2, w // 340))
    _ink(d, w * 0.10, h * 0.67, w * 0.31, h * 0.09, 2.1, max(3, w // 260))
    _illegible(img, (int(w * 0.10), int(h * 0.82), int(w * 0.72),
                     int(h * 0.045)), "Prot. 4471/B del 05/05/2020 rif. interno")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


# I PDF con immagini vanno salvati COMPRESSI: senza deflate PyMuPDF scrive gli
# XObject immagine non compressi e un file di 3 KB diventa di 300.
_SAVE = {"deflate": True, "deflate_images": True, "garbage": 4}


def make_pdf_with_images(path):
    """PDF con testo nativo E immagini che contengono testo leggibile: è il
    caso dell'OCR. L'ultima pagina imita un foglio firme — nomi e numeri che
    esistono SOLO nei pixel, invisibili a qualunque estrazione di testo — e
    porta due firme disegnate più una riga illeggibile, cioè le due
    superfici che non sono testo: [SIGNATURE_n] e [UNREADABLE_n]."""
    doc = fitz.open()

    p0 = doc.new_page()
    p0.insert_textbox(fitz.Rect(50, 50, 545, 300),
                      f"{CONTRACT_HEAD}\n\n{CONTRACT_INTRO}", fontsize=11)
    p0.insert_image(fitz.Rect(50, 320, 545, 700),
                    stream=_page_png(_contract_text()))

    p1 = doc.new_page()
    p1.insert_image(p1.rect, stream=_page_png(
        "ALLEGATO TECNICO\n\n" + _contract_text()))

    p2 = doc.new_page()
    p2.insert_textbox(fitz.Rect(50, 50, 545, 120), "Foglio firme",
                      fontsize=14)
    firme = (f"Per il Fornitore\n\n{PERSON}\n{EMAIL}\n{PHONE}\n\n\n"
             f"Per il Cliente\n\n{PERSON_2}\n{EMAIL_2}\n{MOBILE}\n\n\n"
             f"{ORG} - P.IVA {VAT}\n{ADDRESS}")
    p2.insert_image(fitz.Rect(50, 140, 545, 760),
                    stream=_signature_page_png(firme, size=(495, 620)))
    doc.save(str(path), **_SAVE)
    doc.close()
    print(f"  {path.name}: 3 pagine, immagini con testo nei pixel, "
          f"2 firme disegnate e una riga illeggibile")


def make_pdf_searchable_scan(path):
    """Scansione CERCABILE: i pixel della pagina più una trascrizione OCR
    disegnata invisibile (render mode 3) sopra di essi, storpiata come quella
    vera di uno scanner. È il caso del testo fantasma (engine/pdf_ghost.py):
    la stessa frase due volte, con due letture diverse."""
    clear = (f"Contratto di fornitura tra {ORG}, con sede in {ADDRESS}, "
             f"e {ORG_ALT}. Il corrispettivo va versato sul conto {IBAN}. "
             f"Referente: {PERSON}, {EMAIL}. ") * 4
    garbled = (f"Contratto di fornitura tra Acme Fornlture SRL, con sede in "
               f"Via Roma l0, 2012l Miiano (MI), e Global Trade SPA. "
               f"Referente: Luigl Verdi.")

    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=_page_png(clear))
    writer = fitz.TextWriter(page.rect)
    writer.fill_textbox(fitz.Rect(45, 45, 550, 800), garbled, fontsize=11)
    writer.write_text(page, render_mode=3)          # né riempito né contornato
    doc.save(str(path), **_SAVE)
    doc.close()
    print(f"  {path.name}: pixel + trascrizione invisibile")


def make_pdf_bare_scan(path):
    """Scansione NUDA: solo pixel, nessun layer testuale. Senza OCR non c'è
    niente da anonimizzare, e l'app deve dirlo invece di consegnare un file
    che sembra pulito."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=_page_png(
        f"{CONTRACT_HEAD}\n\n{CONTRACT_INTRO}\n\n" + _contract_text()))
    doc.save(str(path), **_SAVE)
    doc.close()
    print(f"  {path.name}: solo pixel")


# --------------------------------------------------------------------------- #
# Testo
# --------------------------------------------------------------------------- #
def make_txt(path):
    path.write_text(LETTER, encoding="utf-8")
    print(f"  {path.name}: {len(LETTER)} caratteri utf-8")


def make_csv(path):
    rows = ["Nome;Email;Telefono;P.IVA;IBAN"]
    rows += [";".join((c[0], c[1], c[2], c[4], c[5])) for c in CLIENTS]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"  {path.name}: {len(CLIENTS)} righe")


# --------------------------------------------------------------------------- #
# Formati binari legacy, via LibreOffice
# --------------------------------------------------------------------------- #
def make_legacy(src, ext):
    """Converte un OOXML nel corrispondente binario Office 97-2003 con
    LibreOffice. Senza LibreOffice il file non si genera: i test che lo usano
    lo dichiarano saltato invece di fallire."""
    import subprocess
    soffice = shutil.which("soffice") or \
        r"C:\Program Files\LibreOffice\program\soffice.exe"
    if not Path(soffice).exists() and not shutil.which("soffice"):
        print(f"  (salto {src.stem}{ext}: LibreOffice non trovato)")
        return None
    out = src.with_suffix(ext)
    profile = ASSETS / "_lo_profile"
    try:
        subprocess.run(
            [soffice, f"-env:UserInstallation=file:///{profile.as_posix()}",
             "--headless", "--convert-to", ext.lstrip("."),
             "--outdir", str(ASSETS), str(src)],
            check=True, capture_output=True, timeout=180)
    except Exception as e:                                  # noqa: BLE001
        print(f"  (salto {out.name}: conversione fallita — {e})")
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if out.exists():
        print(f"  {out.name}: convertito da {src.name}")
        return out
    print(f"  (salto {out.name}: LibreOffice non ha prodotto il file)")
    return None


# --------------------------------------------------------------------------- #
def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    print(f"Documenti di prova in {ASSETS}")
    print(f"  P.IVA generate: {VAT}, {VAT_ALT}   IBAN: {IBAN}")

    make_docx(ASSETS / "contratto.docx")
    make_xlsx_multisheet(ASSETS / "cartella_multifoglio.xlsx")
    make_xlsx_table(ASSETS / "tabella_grande.xlsx", n_rows=4000)
    make_xlsx_table(ASSETS / "tabella_piccola.xlsx", n_rows=120, n_cols=9)
    make_xlsx_listino(ASSETS / "listino_marchi.xlsx")
    make_pptx(ASSETS / "presentazione.pptx")
    make_pdf_text(ASSETS / "contratto.pdf", pages=2)
    make_pdf_metadata(ASSETS / "pdf_con_metadati.pdf")
    make_pdf_with_images(ASSETS / "scansione_con_immagini.pdf")
    make_pdf_searchable_scan(ASSETS / "scansione_cercabile.pdf")
    make_pdf_bare_scan(ASSETS / "scansione_nuda.pdf")
    make_txt(ASSETS / "lettera.txt")
    make_csv(ASSETS / "clienti.csv")
    make_legacy(ASSETS / "cartella_multifoglio.xlsx", ".xls")
    make_legacy(ASSETS / "contratto.docx", ".doc")
    print("fatto.")


if __name__ == "__main__":
    main()

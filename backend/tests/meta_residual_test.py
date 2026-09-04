"""Verifica dei residui sulle part di METADATI di docx, pptx e xlsx.

La rete di sicurezza (report["residual"]) rilegge il file redatto e cerca i
valori mappati anche fuori dal corpo: core.xml, app.xml, autori dei commenti,
target dei .rels. Leggendo l'XML GREZZO di quelle part però finiva per
guardare anche la scaffolding del formato — dichiarazioni xmlns, URI di
schema, nome dell'applicazione, date del salvataggio — dove i nomi propri
abbondano: un valore legittimo come "Microsoft" (etichettato ORG in una slide)
o un anno etichettato CAP faceva fallire la verifica e il file veniva
RIFIUTATO in blocco, senza che nessuna redazione potesse mai farlo passare.

Qui si prova che:
  1. _meta_surfaces tiene le superfici che possono contenere PII e butta solo
     la scaffolding;
  2. i due falsi positivi reali (Microsoft in un pptx, un anno in un docx) non
     bloccano più il file;
  3. la rete resta TESA: un valore che sopravvive davvero in una part di
     metadati viene ancora segnalato.

Uso:
    python backend/tests/meta_residual_test.py
"""
import io
import re
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

from app.engine.docx import _meta_surfaces, redact_docx                # noqa: E402
from app.engine.pdf_export import _value_pattern                       # noqa: E402
from app.engine.pptx import redact_pptx                                # noqa: E402
from app.engine.xlsx import redact_xlsx                                # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "assets"
DOCX = FIXTURES / "contratto.docx"
PPTX = FIXTURES / "presentazione.pptx"
XLSX = FIXTURES / "listino_marchi.xlsx"

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


def patch_part(data, part, replace):
    """Copia dello ZIP con `part` modificata da `replace(xml_str) -> xml_str`."""
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in src.infolist():
            blob = src.read(info.filename)
            if info.filename == part:
                blob = replace(blob.decode("utf-8")).encode("utf-8")
            zout.writestr(info.filename, blob)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 1. _meta_surfaces: cosa entra e cosa esce
# --------------------------------------------------------------------------- #
def test_surfaces():
    print("\n--- _meta_surfaces ---")

    app_xml = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
<TotalTime>8223</TotalTime><Words>1095</Words>
<Application>Microsoft Office PowerPoint</Application>
<AppVersion>16.0000</AppVersion><PresentationFormat>Widescreen</PresentationFormat>
<Company>Microsoft Italia</Company><Manager>Mario Rossi</Manager>
</Properties>"""
    s = _meta_surfaces(app_xml)
    check("app.xml: Application escluso",
          "Microsoft Office PowerPoint" not in s)
    check("app.xml: AppVersion e contatori esclusi",
          "16.0000" not in s and "8223" not in s and "1095" not in s)
    check("app.xml: Company resta sotto controllo", "Microsoft Italia" in s)
    check("app.xml: Manager resta sotto controllo", "Mario Rossi" in s)

    authors = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p188:authorLst xmlns:p188="http://schemas.microsoft.com/office/powerpoint/2018/8/main">
<p188:author id="{1}" name="Mario Rossi" initials="MR" userId="mario@example.com"
 providerId="AD"/></p188:authorLst>"""
    s = _meta_surfaces(authors)
    check("autori: la dichiarazione xmlns non è una superficie",
          "schemas.microsoft.com" not in s and "microsoft" not in s.lower())
    check("autori: nome, iniziali e account restano sotto controllo",
          "Mario Rossi" in s and "MR" in s and "mario@example.com" in s)

    rels = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Target="../media/image1.wdp"
 Type="http://schemas.microsoft.com/office/2007/relationships/hdphoto"/>
<Relationship Id="rId2" TargetMode="External" Target="https://www.mariorossi.it/cv"
 Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"/>
</Relationships>"""
    s = _meta_surfaces(rels)
    check("rels: gli URI di schema non sono superfici",
          "schemas.microsoft.com" not in s
          and "schemas.openxmlformats.org" not in s)
    check("rels: i Target restano sotto controllo",
          "https://www.mariorossi.it/cv" in s and "../media/image1.wdp" in s)

    core = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties
 xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:creator>Mario Rossi</dc:creator><cp:category>Anna Ferrari</cp:category>
<cp:revision>2023</cp:revision>
<dcterms:created xsi:type="dcterms:W3CDTF">2022-01-19T14:02:00Z</dcterms:created>
<dcterms:modified xsi:type="dcterms:W3CDTF">2023-05-17T07:31:00Z</dcterms:modified>
</cp:coreProperties>"""
    s = _meta_surfaces(core)
    check("core.xml: date e revisione del salvataggio escluse",
          "2022-01-19T14:02:00Z" not in s and "2023-05-17T07:31:00Z" not in s
          and not re.search(r"(?<!\w)2023(?!\w)", s))
    check("core.xml: autore e campi liberi restano sotto controllo",
          "Mario Rossi" in s and "Anna Ferrari" in s)

    broken = b"<Properties><Company>Mario Rossi</Company>"
    check("part illeggibile: si ricade sul controllo grezzo",
          "Mario Rossi" in _meta_surfaces(broken))


# --------------------------------------------------------------------------- #
# 2. I falsi positivi reali non bloccano più il file
# --------------------------------------------------------------------------- #
def test_false_positives():
    print("\n--- falsi positivi (il file deve passare) ---")

    pptx = PPTX.read_bytes()
    # il caso segnalato: "Microsoft" è testo di slide legittimo (ORG) e
    # compare in docProps/app.xml (<Application>) e nella dichiarazione xmlns
    # di ppt/authors.xml
    raw = "\n".join(
        zipfile.ZipFile(io.BytesIO(pptx)).read(n).decode("utf-8", "replace")
        for n in ("docProps/app.xml", "ppt/authors.xml"))
    check("pptx: la fixture riproduce le condizioni del bug",
          "Microsoft Office PowerPoint" in raw
          and "schemas.microsoft.com" in raw)
    _out, report = redact_pptx(pptx, {"[ORG_1]": "Microsoft",
                                      "[FULLNAME_1]": "Luigi Verdi"})
    check("pptx: ORG 'Microsoft' non è un residuo",
          report["residual"] == [], f"residual={report['residual']}")
    check("pptx: la redazione del corpo funziona ancora",
          report["by_placeholder"]["[FULLNAME_1]"] > 0,
          f"occorrenze={report['occurrences']}")

    docx = DOCX.read_bytes()
    core = zipfile.ZipFile(io.BytesIO(docx)).read("docProps/core.xml").decode()
    check("docx: la fixture ha le date in core.xml",
          "<dcterms:created" in core and "2024" in core)
    # un anno etichettato CAP/ZIPCODE (succede davvero) collideva con
    # dcterms:created
    _out, report = redact_docx(docx, {"[ZIPCODE_1]": "2024",
                                      "[CITY_1]": "Milano"})
    check("docx: l'anno '2024' non è un residuo",
          report["residual"] == [], f"residual={report['residual']}")
    check("docx: la redazione del corpo funziona ancora",
          report["by_placeholder"]["[CITY_1]"] > 0,
          f"occorrenze={report['occurrences']}")

    # lo scrub dei metadati non è cambiato: l'autore sparisce davvero
    _out, report = redact_docx(docx, {"[FULLNAME_1]": "Luigi Verdi"})
    check("docx: l'autore in core.xml resta scrubbato (nessun residuo)",
          report["residual"] == [], f"residual={report['residual']}")

    xlsx = XLSX.read_bytes()
    app = zipfile.ZipFile(io.BytesIO(xlsx)).read("docProps/app.xml").decode()
    check("xlsx: la fixture ha <Application>Microsoft Excel</Application>",
          "Microsoft Excel" in app)
    _out, report = redact_xlsx(xlsx, {"[ORG_1]": "Microsoft",
                                      "[ORG_2]": "Vistaclear"})
    check("xlsx: ORG 'Microsoft' non è un residuo",
          report["residual"] == [], f"residual={report['residual']}")
    check("xlsx: la redazione delle celle funziona ancora",
          report["by_placeholder"]["[ORG_2]"] > 0,
          f"occorrenze={report['occurrences']}")


# --------------------------------------------------------------------------- #
# 3. I due metri sullo stesso insieme: ciò che il verificatore controlla lo
#    scrubber lo svuota (core.xml), e dove nessuno scrubber arriva per scelta
#    la rete resta tesa (Target dei .rels interni)
# --------------------------------------------------------------------------- #
def test_true_positives():
    print("\n--- scrubber e verificatore allineati, rete ancora tesa ---")

    # cp:category ERA la falla di questa suite: _scrub_core_props lavorava su
    # un elenco POSITIVO che non la conteneva, mentre _meta_surfaces la legge.
    # Un valore mappato lì dentro non usciva dal server — usciva di peggio: il
    # file veniva dichiarato non protetto e nessuna redazione poteva più farlo
    # passare (irrecuperabile). Ora l'elenco è negativo e legato a
    # _META_BOILERPLATE: tutto ciò che il verificatore controlla, lo scrubber
    # lo svuota. Il caso resta qui a presidiare quel legame.
    docx = patch_part(
        DOCX.read_bytes(), "docProps/core.xml",
        lambda s: s.replace("<cp:revision>",
                            "<cp:category>Anna Bianchi</cp:category>"
                            "<cp:revision>"))
    out, report = redact_docx(docx, {"[FULLNAME_1]": "Anna Bianchi"})
    core = zipfile.ZipFile(io.BytesIO(out)).read("docProps/core.xml").decode()
    check("docx: cp:category svuotata, nessun residuo",
          report["residual"] == [] and "Anna Bianchi" not in core,
          f"residual={report['residual']}")

    # target INTERNO di un .rels: _scrub_rels tocca solo gli External
    docx = patch_part(
        DOCX.read_bytes(), "word/_rels/document.xml.rels",
        lambda s: s.replace(
            "</Relationships>",
            '<Relationship Id="rIdX" Target="../embeddings/Anna Ferrari.xlsx"'
            ' Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/package"/></Relationships>'))
    _out, report = redact_docx(docx, {"[FULLNAME_2]": "Anna Ferrari"})
    check("docx: valore nel Target di un rels interno -> residuo",
          report["residual"] == ["[FULLNAME_2]"],
          f"residual={report['residual']}")

    xlsx = patch_part(
        XLSX.read_bytes(), "docProps/core.xml",
        lambda s: s.replace("</cp:coreProperties>",
                            "<cp:category>Anna Ferrari</cp:category>"
                            "</cp:coreProperties>"))
    out, report = redact_xlsx(xlsx, {"[FULLNAME_4]": "Anna Ferrari"})
    core = zipfile.ZipFile(io.BytesIO(out)).read("docProps/core.xml").decode()
    check("xlsx: cp:category svuotata, nessun residuo",
          report["residual"] == [] and "Anna Ferrari" not in core,
          f"residual={report['residual']}")

    pptx = patch_part(
        PPTX.read_bytes(), "docProps/core.xml",
        lambda s: s.replace("</cp:coreProperties>",
                            "<cp:category>Giulia Esposito</cp:category>"
                            "</cp:coreProperties>"))
    out, report = redact_pptx(pptx, {"[FULLNAME_3]": "Giulia Esposito"})
    core = zipfile.ZipFile(io.BytesIO(out)).read("docProps/core.xml").decode()
    check("pptx: cp:category svuotata, nessun residuo",
          report["residual"] == [] and "Giulia Esposito" not in core,
          f"residual={report['residual']}")

    # La rete deve restare tesa anche per xlsx e pptx, non solo per il docx:
    # serve una superficie che NESSUNO scrubber pulisce per scelta. Il Target
    # di un .rels INTERNO lo è — _scrub_rels riscrive solo i TargetMode
    # External — ed è il caso che il docx qui sopra prova già.
    for name, data, part, redact, ph, value in (
            ("xlsx", XLSX.read_bytes(), "xl/_rels/workbook.xml.rels",
             redact_xlsx, "[FULLNAME_5]", "Sofia Marino"),
            ("pptx", PPTX.read_bytes(), "ppt/_rels/presentation.xml.rels",
             redact_pptx, "[FULLNAME_6]", "Paolo Greco")):
        patched = patch_part(data, part, lambda s, v=value: s.replace(
            "</Relationships>",
            f'<Relationship Id="rIdX" Target="../embeddings/{v}.bin"'
            ' Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/package"/></Relationships>'))
        _out, report = redact(patched, {ph: value})
        check(f"{name}: valore nel Target di un rels interno -> residuo",
              ph in report["residual"], f"residual={report['residual']}")


# --------------------------------------------------------------------------- #
# 4. docProps/app.xml del docx: titolo, Company e Manager vanno via
# --------------------------------------------------------------------------- #
APP_PII = ("<TitlesOfParts><vt:vector size=\"1\" baseType=\"lpstr\">"
           "<vt:lpstr>Contratto con Mario Rossi</vt:lpstr></vt:vector>"
           "</TitlesOfParts><Company>Studio Bianchi &amp; Associati</Company>"
           "<Manager>Anna Ferrari</Manager>")


def with_app_pii(data):
    """La fixture con app.xml riempito come lo riempie Word in un'installazione
    aziendale (titolo del documento + Company + Manager)."""
    return patch_part(data, "docProps/app.xml",
                      lambda s: re.sub(r"<TitlesOfParts>.*?</TitlesOfParts>"
                                       r"<Company></Company>", APP_PII, s,
                                       flags=re.S))


def test_docx_app_props():
    print("\n--- docx: docProps/app.xml ---")
    docx = with_app_pii(DOCX.read_bytes())
    app = zipfile.ZipFile(io.BytesIO(docx)).read("docProps/app.xml").decode()
    check("fixture preparata: app.xml con titolo, Company e Manager",
          "Contratto con Mario Rossi" in app and "Studio Bianchi" in app
          and "Anna Ferrari" in app)

    # mappa mista: valori che stanno SOLO in app.xml (titolo, Company,
    # Manager) e valori del CORPO, così l'artefatto scritto in fondo prova
    # con Word vero sia lo scrub sia i run gialli
    out, report = redact_docx(docx, {"[FULLNAME_1]": "Mario Rossi",
                                     "[ORG_1]": "Studio Bianchi & Associati",
                                     "[FULLNAME_2]": "Anna Ferrari",
                                     "[FULLNAME_3]": "Luigi Verdi",
                                     "[CITY_1]": "Milano"})
    check("il corpo è stato redatto",
          report["by_placeholder"]["[FULLNAME_3]"] > 0
          and report["by_placeholder"]["[CITY_1]"] > 0,
          f"occorrenze={report['occurrences']}")
    app_out = zipfile.ZipFile(io.BytesIO(out)).read("docProps/app.xml").decode()
    check("titolo del documento svuotato",
          "Contratto con Mario Rossi" not in app_out)
    check("Company svuotata", "Studio Bianchi" not in app_out)
    check("Manager svuotato", "Anna Ferrari" not in app_out)
    check("struttura di app.xml intatta",
          "<TitlesOfParts>" in app_out and 'size="1"' in app_out
          and "<Application>Microsoft Office Word</Application>" in app_out)
    check("app.xml è nella verifica dei residui (nessun residuo)",
          report["residual"] == [], f"residual={report['residual']}")

    # senza lo scrub i tre valori uscirebbero intatti: la verifica li deve vedere
    # (dopo lo scrub ElementTree serializza i campi vuoti come <Company />)
    unscrubbed = patch_part(
        out, "docProps/app.xml",
        lambda s: re.sub(r"<Company\s*/>|<Company></Company>",
                         "<Company>Studio Bianchi &amp; Associati</Company>",
                         s))
    from app.engine.docx import _verify_residuals
    check("un valore rimesso in app.xml viene segnalato",
          _verify_residuals(unscrubbed,
                            [("[ORG_1]", "Studio Bianchi & Associati")])
          == ["[ORG_1]"])

    (HERE / "data").mkdir(exist_ok=True)
    (HERE / "data" / "meta_residual_word.docx").write_bytes(out)
    print("   (docx redatto scritto in data/meta_residual_word.docx per la "
          "prova con Word)")


# --------------------------------------------------------------------------- #
# 5. Hyperlink esterni: lo scrubber deve coprire tutto ciò che il
#    verificatore sa vedere (altrimenti il file è irrecuperabile)
# --------------------------------------------------------------------------- #
def test_rels_scrub():
    print("\n--- .rels: scrubber e verifica devono usare lo stesso metro ---")
    from app.engine.docx import _scrub_rels

    rels = ("""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" TargetMode="External" Target="https://www.instagram.com/acmeanalytics/" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"/>
<Relationship Id="rId2" TargetMode="External" Target="https://www.acme-analytics.example" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"/>
<Relationship Id="rId3" TargetMode="External" Target="https://rossini.com/catalogo" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"/>
<Relationship Id="rId4" TargetMode="External" Target="https://www.example.com/prodotti" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"/>
<Relationship Id="rId5" Target="../media/image1.png" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"/>
</Relationships>""").encode("utf-8")

    # il caso reale: termine fisso con lo spazio, URL senza spazio
    out, n = _scrub_rels(rels, ["Acme Analytics"])
    s = out.decode("utf-8") if isinstance(out, bytes) else out
    check("valore con spazi trovato nell'URL attaccato (instagram)",
          "instagram.com/acmeanalytics" not in s, f"scrubbati={n}")
    check("e nel dominio senza spazi", "www.acmeanalytics.com" not in s)
    check("gli hyperlink innocui restano",
          "https://www.example.com/prodotti" in s)
    check("i target interni non si toccano", "../media/image1.png" in s)

    # il criterio letterale non deve regredire: 'Rossi' dentro 'rossini.com'
    out, n = _scrub_rels(rels, ["Rossini"])
    s = out.decode("utf-8") if isinstance(out, bytes) else out
    check("frammento dentro un'altra parola ancora coperto",
          "rossini.com" not in s, f"scrubbati={n}")

    # e ciò che lo scrubber lascia, il verificatore non lo deve trovare
    out, _n = _scrub_rels(rels, ["Acme Analytics"])
    check("nessun residuo dopo lo scrub",
          not _value_pattern("Acme Analytics").search(_meta_surfaces(out)))


def main():
    for f in (DOCX, PPTX, XLSX):
        if not f.is_file():
            print(f"fixture mancante: {f}")
            return 1
    test_surfaces()
    test_false_positives()
    test_true_positives()
    test_docx_app_props()
    test_rels_scrub()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

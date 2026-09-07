"""Synthetic spreadsheet preview regressions, including real LibreOffice export.

Run in the backend container: python /app/tests/xlsx_preview_layout_test.py
"""
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_tmp = tempfile.TemporaryDirectory(prefix="xlsx-preview-test-")
os.environ["BLOCKINGBEAR_DATA_DIR"] = _tmp.name
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf as fitz
from app.engine import convert, xlsx

X = xlsx.X


def workbook(sheets=1, rows=3, columns=14):
    """An intentionally poor print layout, typical of converted legacy XLS."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package = "http://schemas.openxmlformats.org/package/2006/relationships"
    contents = [f'<Override PartName="/xl/worksheets/sheet{n}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for n in range(1, sheets + 1)]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   + ''.join(contents) + '</Types>')
        z.writestr("_rels/.rels", f'<Relationships xmlns="{package}"><Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
                   + ''.join(f'<sheet name="Sheet {n}" sheetId="{n}" r:id="rId{n}"/>' for n in range(1, sheets + 1))
                   + '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{package}">'
                   + ''.join(f'<Relationship Id="rId{n}" Type="{rel}/worksheet" Target="worksheets/sheet{n}.xml"/>' for n in range(1, sheets + 1))
                   + '</Relationships>')
        body = ''.join(f'<row r="{r}">'
                       + ''.join(f'<c r="{chr(65+c)}{r}" t="inlineStr"><is><t>Cell {r} {c}</t></is></c>' for c in range(columns))
                       + '</row>' for r in range(1, rows + 1))
        sheet = (f'<worksheet xmlns="{main}"><sheetPr><pageSetUpPr fitToPage="0"/></sheetPr>'
                 f'<dimension ref="A1:{chr(64+columns)}{rows}"/>'
                 f'<cols><col min="1" max="{columns}" width="20" customWidth="1"/></cols>'
                 f'<sheetData>{body}</sheetData>'
                 '<pageSetup paperSize="9" orientation="portrait" scale="100" fitToWidth="1" fitToHeight="1"/>'
                 '<rowBreaks count="1" manualBreakCount="1"><brk id="1" max="16383" man="1"/></rowBreaks>'
                 '<colBreaks count="1" manualBreakCount="1"><brk id="2" max="1048575" man="1"/></colBreaks>'
                 '</worksheet>')
        for n in range(1, sheets + 1):
            z.writestr(f"xl/worksheets/sheet{n}.xml", sheet)
    return out.getvalue()


def sheet_root(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return ET.fromstring(z.read("xl/worksheets/sheet1.xml"))


class XlsxPreviewLayoutTest(unittest.TestCase):
    def test_explicit_print_setup_is_preserved_for_readability(self):
        original = workbook()
        preview, truncated = xlsx.truncate_for_preview(original)
        root = sheet_root(preview)
        setup = root.find(X + "pageSetup")
        self.assertEqual(setup.get("fitToWidth"), "1")
        self.assertEqual(setup.get("fitToHeight"), "1")
        self.assertEqual(setup.get("orientation"), "portrait")
        self.assertEqual(setup.get("scale"), "100")
        self.assertEqual(root.find(f'{X}sheetPr/{X}pageSetUpPr').get("fitToPage"), "0")
        self.assertIsNotNone(root.find(X + "rowBreaks"))
        self.assertIsNotNone(root.find(X + "colBreaks"))
        self.assertFalse(truncated)
        self.assertEqual(sheet_root(original).find(X + "pageSetup").get("scale"), "100")

    def test_row_limit_does_not_limit_redaction_or_export(self):
        original = workbook(rows=180, columns=1)
        redacted, report = xlsx.redact_xlsx(original, {"[CUSTOM_1]": "Cell 180 0"})
        preview, truncated = xlsx.truncate_for_preview(redacted, max_rows=150)
        self.assertTrue(truncated)
        self.assertEqual(len(sheet_root(preview).find(X + "sheetData")), 150)
        full = sheet_root(redacted)
        self.assertEqual(len(full.find(X + "sheetData")), 180)
        self.assertIn("[CUSTOM_1]", ''.join(full.itertext()))
        self.assertNotIn("Cell 180 0", ''.join(full.itertext()))
        self.assertEqual(full.find(X + "pageSetup").get("orientation"), "portrait")
        self.assertIsNotNone(full.find(X + "colBreaks"))
        self.assertEqual(report["residual"], [])

    def test_missing_setup_uses_the_same_bounded_width(self):
        root = ET.fromstring(f'<worksheet xmlns="{X[1:-1]}"><sheetData/></worksheet>')
        xlsx._spreadsheet_look(root)
        self.assertEqual(root.find(X + "pageSetup").get("fitToWidth"), "1")
        self.assertEqual(root.find(f'{X}sheetPr/{X}pageSetUpPr').get("fitToPage"), "1")
        self.assertEqual(list(root)[0].tag, X + "sheetPr")

    def test_many_sheet_preview_remains_complete_and_readable(self):
        original = workbook(sheets=8)
        preview, _ = xlsx.truncate_for_preview(original)
        new_pdf = convert.to_pdf(preview, suffix=".xlsx")
        with fitz.open(stream=new_pdf, filetype="pdf") as new:
            # Wide sheets legitimately need horizontal pages. The viewer must
            # load these on demand, rather than shrinking cells to tiny text.
            self.assertGreater(len(new), 8)
            text = '\n'.join(page.get_text() for page in new)
            for n in range(1, 9):
                self.assertIn(f"Sheet {n}", text)
            self.assertEqual(text.count("Cell 3 13"), 8)
            sizes = [span["size"] for page in new
                     for block in page.get_text("dict")["blocks"] if "lines" in block
                     for line in block["lines"] for span in line["spans"]
                     if span["text"].startswith("Cell")]
            self.assertTrue(sizes)
            self.assertGreaterEqual(min(sizes), 8)


if __name__ == "__main__":
    unittest.main()

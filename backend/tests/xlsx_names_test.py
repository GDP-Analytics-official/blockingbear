"""Complete-workbook privacy, reference preservation, restoration and egress.

Runs offline with synthetic OOXML and an isolated in-memory registry.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

_temp = tempfile.TemporaryDirectory(prefix="bb-xlsx-names-")
os.environ["BLOCKINGBEAR_DATA_DIR"] = _temp.name
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app import db, chat_anonymization as ca, project_files
from app.engine import xlsx
from app.engine.core import PiiEngine
from app.engine.xlsx_matching import ValueIndex
from app.engine.xlsx_names import WorkbookNameError, _rewrite_formula
from app.openrouter import briefing
from app.routes.chat_routes import _egress_check

X = xlsx.X
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
C = "http://schemas.openxmlformats.org/package/2006/content-types"


def pack(parts):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return out.getvalue()


def unpack(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {name: z.read(name) for name in z.namelist()}


def make_book(names, value="Safe", formulas=None, definitions=(), hidden=False, extras=None):
    for prefix, ns in (("x", xlsx.X_NS), ("r", R), ("p", P), ("ct", C)):
        ET.register_namespace(prefix, ns)
    wb = ET.Element(X + "workbook")
    sheets = ET.SubElement(wb, X + "sheets")
    rels = ET.Element("{" + P + "}Relationships")
    ct = ET.Element("{" + C + "}Types")
    for ext, mime in (("rels", "application/vnd.openxmlformats-package.relationships+xml"), ("xml", "application/xml")):
        ET.SubElement(ct, "{" + C + "}Default", Extension=ext, ContentType=mime)
    ET.SubElement(ct, "{" + C + "}Override", PartName="/xl/workbook.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml")
    parts = {}
    for i, name in enumerate(names, 1):
        attrs = {"name": name, "sheetId": str(i), "{" + R + "}id": f"rId{i}"}
        if hidden and i == len(names):
            attrs["state"] = "hidden"
        ET.SubElement(sheets, X + "sheet", attrs)
        part = f"xl/worksheets/sheet{i}.xml"
        ET.SubElement(rels, "{" + P + "}Relationship", Id=f"rId{i}", Type=R + "/worksheet", Target=f"worksheets/sheet{i}.xml")
        ET.SubElement(ct, "{" + C + "}Override", PartName="/" + part, ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
        root = ET.Element(X + "worksheet")
        ET.SubElement(root, X + "dimension", ref="A1:B2")
        data = ET.SubElement(root, X + "sheetData")
        row = ET.SubElement(data, X + "row", r="1")
        c = ET.SubElement(row, X + "c", r="A1", t="inlineStr")
        ET.SubElement(ET.SubElement(c, X + "is"), X + "t").text = value
        row = ET.SubElement(data, X + "row", r="2")
        c = ET.SubElement(row, X + "c", r="A2")
        ET.SubElement(c, X + "v").text = "7"
        if formulas and i - 1 in formulas:
            c = ET.SubElement(row, X + "c", r="B2")
            ET.SubElement(c, X + "f").text = formulas[i - 1]
            ET.SubElement(c, X + "v").text = "7"
        parts[part] = ET.tostring(root)
    if definitions:
        dn = ET.SubElement(wb, X + "definedNames")
        for name, value, scope in definitions:
            attrs = {"name": name}
            if scope is not None:
                attrs["localSheetId"] = str(scope)
            ET.SubElement(dn, X + "definedName", attrs).text = value
    parts["xl/workbook.xml"] = ET.tostring(wb)
    parts["xl/_rels/workbook.xml.rels"] = ET.tostring(rels)
    parts["[Content_Types].xml"] = ET.tostring(ct)
    parts["_rels/.rels"] = ('<Relationships xmlns="' + P + '"><Relationship Id="rId1" Type="' + R + '/officeDocument" Target="xl/workbook.xml"/></Relationships>').encode()
    parts.update(extras or {})
    return pack(parts)


class WorkbookPrivacyTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add(db.Conversation(id="chat", owner_id=1, anonymized=1))
        self.session.flush()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def register(self, value, ph="[FULLNAME_1]", excluded=0):
        ent = db.ConversationEntity(conv_id="chat", placeholder=ph, label=ph[1:].rsplit("_", 1)[0], canonical_value=value, mapping_version=1, excluded=excluded)
        self.session.add(ent)
        self.session.flush()
        self.session.add(db.ConversationEntityAlias(entity_id=ent.id, original_surface=value, normalized_key=value, mapping_version=1))
        self.session.flush()
        return ca.conversation_mapping(self.session, "chat", include_aliases=True)

    def attachment(self, data):
        path = Path(_temp.name) / "protected.xlsx"
        path.write_bytes(data)
        return db.Attachment(id="file", conv_id="chat", filename="input.xlsx", protected_path=str(path), original_path=str(path) + ".original", anonymization_status="protected", model_briefing_json=json.dumps(briefing.describe(str(path), "input.xlsx")))

    def restore(self, data, mapping):
        path = Path(_temp.name) / "restore.xlsx"
        path.write_bytes(data)
        out, n, left, extra = ca.restore_artifact(path, mapping)
        self.assertIsNotNone(out, extra)
        self.assertGreater(n, 0)
        self.assertEqual(left, [])
        return out

    def test_all_sheet_positions_and_hidden_names(self):
        mapping = self.register("Mario Rossi")
        for position in (1, 8, 9, 12):
            for hidden in (False, True):
                with self.subTest(position=position, hidden=hidden):
                    names = [f"Sheet{i}" for i in range(position - 1)] + ["Mario Rossi"]
                    data = make_book(names, value="Mario Rossi", hidden=hidden)
                    out, report = xlsx.redact_xlsx(data, mapping)
                    self.assertEqual(report["residual"], [])
                    self.assertFalse(xlsx.known_xlsx_leaks(out, mapping))
                    self.assertNotIn(b"Mario Rossi", b"".join(unpack(out).values()))
                    _egress_check(self.session, "chat", "Summarize.", [self.attachment(out)])
                    restored = self.restore(out, mapping)
                    self.assertEqual(xlsx.sheet_names(restored), names)

    def test_final_guard_checks_the_file_even_if_briefing_is_empty(self):
        self.register("Mario Rossi")
        data = make_book([f"Sheet{i}" for i in range(8)] + ["Mario Rossi"])
        att = self.attachment(data)
        att.model_briefing_json = "{}"
        with self.assertRaises(ca.TurnAnonymizationError):
            _egress_check(self.session, "chat", "Summarize.", [att])
        Path(att.protected_path).write_bytes(b"invalid")
        with self.assertRaises(ca.TurnAnonymizationError):
            _egress_check(self.session, "chat", "Summarize.", [att])

    def test_policy_exclusion_is_respected(self):
        self.register("Mario Rossi", excluded=1)
        _egress_check(self.session, "chat", "Summarize.", [self.attachment(make_book(["Mario Rossi"]))])

    def test_formula_and_named_range_roundtrip(self):
        value = "O'Brien"
        mapping = {"[FULLNAME_1]": value, "[CUSTOM_1]": "PrivateTotal"}
        formula = "SUM('O''Brien'!A2,PrivateTotal)"
        data = make_book([value, "Report"], formulas={1: formula},
                         definitions=[("PrivateTotal", "'O''Brien'!$A$2", None)])
        out, report = xlsx.redact_xlsx(data, mapping)
        self.assertEqual(report["residual"], [])
        self.assertEqual(report["frozen_formulas"], 0)
        f = ET.fromstring(unpack(out)["xl/worksheets/sheet2.xml"]).find(".//" + X + "f").text
        self.assertIn("BB_S_", f)
        self.assertIn("BB_N_", f)
        restored = self.restore(out, mapping)
        root = ET.fromstring(unpack(restored)["xl/worksheets/sheet2.xml"])
        self.assertEqual(root.find(".//" + X + "f").text, formula)
        self.assertEqual(root.find(".//" + X + "c[@r='B2']/" + X + "v").text, "7")
        self.assertNotIn(b"_BB_RESTORE_", unpack(restored)["xl/workbook.xml"])

    def test_named_constants_are_detected_redacted_and_counted(self):
        data = make_book(["Safe"], definitions=[("Contact", '"Mario Rossi"', None)])
        self.assertIn("Mario Rossi", xlsx.extract_text(data))
        result = xlsx.rebuild_xlsx(data, {"[FULLNAME_1]": "Mario Rossi"}, make_preview=False)
        self.assertGreater(result["report"]["occurrences"], 0)
        self.assertNotIn("Mario Rossi", xlsx.extract_text(result["file"], full=True))

    def test_detection_sees_name_only_pii(self):
        engine = PiiEngine("unused")
        engine.detect_model = lambda *args, **kwargs: []
        result = xlsx.anonymize_xlsx(make_book(["Mario Rossi"]), engine,
            custom_terms=[{"text": "Mario Rossi", "tag": "FULLNAME"}], make_preview=False)
        self.assertNotIn("Mario Rossi", xlsx.sheet_names(result["file"]))

    def test_local_names_remain_scoped(self):
        data = make_book(["First", "Second"], formulas={0: "SecretName", 1: "SecretName"},
                         definitions=[("SecretName", "First!$A$2", 0), ("SecretName", "Second!$A$2", 1)])
        mapping = {"[CUSTOM_1]": "SecretName"}
        out, _ = xlsx.redact_xlsx(data, mapping)
        parts = unpack(out)
        first = ET.fromstring(parts["xl/worksheets/sheet1.xml"]).find(".//" + X + "f").text
        second = ET.fromstring(parts["xl/worksheets/sheet2.xml"]).find(".//" + X + "f").text
        self.assertNotEqual(first, second)
        restored = self.restore(out, mapping)
        for i in (1, 2):
            self.assertEqual(ET.fromstring(unpack(restored)[f"xl/worksheets/sheet{i}.xml"]).find(".//" + X + "f").text, "SecretName")

    def test_references_quoted_3d_strings_external_and_structured(self):
        renames = {"first": "BB_S_one", "last": "BB_S_two"}
        for formula in ("SUM(First:Last!A1)", "SUM('First:Last'!A1)"):
            self.assertEqual(_rewrite_formula(formula, renames, {}, {}), "SUM('BB_S_one:BB_S_two'!A1)")
        text = '"First!A1"+[1]First!A1+Table1[First]'
        self.assertEqual(_rewrite_formula(text, renames, {}, {}), text)

    def test_dynamic_references_reject_unsafe_rename(self):
        data = make_book(["Mario Rossi"], formulas={0: 'INDIRECT(A1&"!A2")'})
        with self.assertRaisesRegex(WorkbookNameError, "INDIRECT"):
            xlsx.redact_xlsx(data, {"[FULLNAME_1]": "Mario Rossi"})

    def test_unsupported_workbook_field_fails_with_location(self):
        parts = unpack(make_book(["Safe"]))
        root = ET.fromstring(parts["xl/workbook.xml"])
        ET.SubElement(root, X + "fileSharing", userName="Mario Rossi")
        parts["xl/workbook.xml"] = ET.tostring(root)
        with self.assertRaisesRegex(WorkbookNameError, "fileSharing/userName"):
            xlsx.redact_xlsx(pack(parts), {"[FULLNAME_1]": "Mario Rossi"})

    def test_reference_mode_is_structural_but_matching_cell_data_is_protected(self):
        for mode in ("A1", "R1C1"):
            with self.subTest(mode=mode):
                parts = unpack(make_book(["Safe"], value=mode))
                root = ET.fromstring(parts["xl/workbook.xml"])
                ET.SubElement(root, X + "calcPr", refMode=mode)
                parts["xl/workbook.xml"] = ET.tostring(root)
                mapping = {"[ID_DOC_1]": mode}
                out, report = xlsx.redact_xlsx(pack(parts), mapping)
                self.assertEqual(report["residual"], [])
                self.assertFalse(xlsx.known_xlsx_leaks(out, mapping))
                self.assertIn("[ID_DOC_1]", xlsx.extract_text(out))
                self.assertEqual(ET.fromstring(unpack(out)["xl/workbook.xml"])
                                 .find(X + "calcPr").get("refMode"), mode)

    def test_unknown_reference_mode_is_still_checked(self):
        parts = unpack(make_book(["Safe"]))
        root = ET.fromstring(parts["xl/workbook.xml"])
        ET.SubElement(root, X + "calcPr", refMode="PrivateName")
        parts["xl/workbook.xml"] = ET.tostring(root)
        with self.assertRaisesRegex(WorkbookNameError, "calcPr/refMode"):
            xlsx.redact_xlsx(pack(parts), {"[CUSTOM_1]": "PrivateName"})

    def test_named_literal_restores_excel_quotes(self):
        value = 'Mario "Rossi"'
        data = make_book(["Safe"], definitions=[("Contact", '"Mario ""Rossi"""', None)])
        mapping = {"[FULLNAME_1]": value}
        out, report = xlsx.redact_xlsx(data, mapping)
        self.assertEqual(report["residual"], [])
        restored = self.restore(out, mapping)
        root = ET.fromstring(unpack(restored)["xl/workbook.xml"])
        self.assertEqual(root.find(".//" + X + "definedName").text, '"Mario ""Rossi"""')

    def test_pivot_and_hyperlink_references_follow_sheet_rename(self):
        pivot = ('<pivotCacheDefinition xmlns="' + xlsx.X_NS + '"><cacheSource type="worksheet"><worksheetSource sheet="Mario Rossi" ref="A1:A2"/></cacheSource></pivotCacheDefinition>').encode()
        data = make_book(["Mario Rossi"], extras={"xl/pivotCache/pivotCacheDefinition1.xml": pivot})
        parts = unpack(data)
        root = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
        links = ET.SubElement(root, X + "hyperlinks")
        ET.SubElement(links, X + "hyperlink", ref="A1", location="'Mario Rossi'!A2")
        parts["xl/worksheets/sheet1.xml"] = ET.tostring(root)
        mapping = {"[FULLNAME_1]": "Mario Rossi"}
        out, report = xlsx.redact_xlsx(pack(parts), mapping)
        self.assertEqual(report["residual"], [])
        parts = unpack(out)
        neutral = xlsx.sheet_names(out)[0]
        source = ET.fromstring(parts["xl/pivotCache/pivotCacheDefinition1.xml"]).find(".//" + X + "worksheetSource")
        self.assertEqual(source.get("sheet"), neutral)
        link = ET.fromstring(parts["xl/worksheets/sheet1.xml"]).find(".//" + X + "hyperlink")
        self.assertEqual(link.get("location"), f"'{neutral}'!A2")
        restored = self.restore(out, mapping)
        self.assertIn(b'Mario Rossi', unpack(restored)["xl/pivotCache/pivotCacheDefinition1.xml"])

    def test_local_name_shadows_workbook_name(self):
        names = {(None, "secretname"): "BB_N_global", (0, "secretname"): "SECRETNAME"}
        self.assertEqual(_rewrite_formula("SecretName", {}, names, {}, 0), "SECRETNAME")
        self.assertEqual(_rewrite_formula("SecretName", {}, names, {}, 1), "BB_N_global")

    def test_residual_check_covers_external_names_and_chart_references(self):
        mapping = self.register("Mario Rossi")
        for part, xml in (
                ("xl/externalLinks/externalLink1.xml", '<externalLink><sheetNames><sheetName val="Mario Rossi"/></sheetNames></externalLink>'),
                ("xl/charts/chart1.xml", '<chart><f>\'Mario Rossi\'!A1</f></chart>')):
            with self.subTest(part=part):
                data = make_book(["Safe"], extras={part: xml.encode()})
                self.assertTrue(xlsx.known_xlsx_leaks(data, mapping))
                self.assertIn("[FULLNAME_1]", xlsx._verify_residuals(data, list(mapping.items())))
                with self.assertRaises(ca.TurnAnonymizationError):
                    _egress_check(self.session, "chat", "Summarize.", [self.attachment(data)])

    def test_table_values_embedded_in_longer_cells_and_metadata(self):
        mapping = self.register("T0011", "[CF_78]")
        data = make_book(["Report T0011"], value="Ref T0011")
        out, report = xlsx.redact_xlsx(data, mapping, exact_phs={"[CF_78]"})
        self.assertEqual(report["residual"], [])
        self.assertIn("Ref [CF_78]", xlsx.extract_text(out))
        self.assertEqual(xlsx.sheet_name_aliases(data, out)[xlsx.sheet_names(out)[0]], "Report T0011")
        _egress_check(self.session, "chat", "Summarize.", [self.attachment(out)])

    def test_candidate_index_unicode_boundaries_and_large_mapping(self):
        items = [(f"[CUSTOM_{i}]", f"CODE{i:06d}") for i in range(40000)]
        index = ValueIndex(items + [("[FULLNAME_1]", "José Straße")])
        self.assertEqual(index.matches("Ref CODE039999 / Jose\u0301 STRASSE"),
                         [("[FULLNAME_1]", "José Straße"), ("[CUSTOM_39999]", "CODE039999")])
        self.assertEqual(index.matches("XCODE039999Y"), [])

    def test_project_confirmation_uses_complete_workbook(self):
        self.register("Mario Rossi")
        project = db.Project(id="chat", owner_id=1, anonymized=1, mapping_version=1)
        pf = db.ProjectFile(id="projectfile", project_id="chat", filename="book.xlsx", confirmed=0, mapping_version=1)
        path = project_files.protected_path(pf)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(make_book([f"Sheet{i}" for i in range(8)] + ["Mario Rossi"]))
        with self.assertRaises(project_files.ProjectFileError):
            project_files.confirm(self.session, project, pf)
        self.assertEqual(pf.confirmed, 0)
        mapping = ca.conversation_mapping(self.session, "chat", include_aliases=True)
        path.write_bytes(xlsx.redact_xlsx(path.read_bytes(), mapping)[0])
        project_files.confirm(self.session, project, pf)
        self.assertEqual(pf.confirmed, 1)


if __name__ == "__main__":
    unittest.main()

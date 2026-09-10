"""Synthetic regressions for source boundaries, registry and residual checks."""
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from xlsx_names_test import make_book, pack, unpack, WorkbookPrivacyTest
from app import chat_anonymization as ca
from app.engine import xlsx, image_ocr
from app.engine.core import PiiEngine
from app.engine.source_text import SourceText
from app.engine.text_patterns import contains_literal


def book(values, separate_rows=False):
    parts = unpack(make_book(["Sheet"], value="Safe"))
    root = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
    data = root.find(xlsx.X + "sheetData")
    data.clear()
    row = ET.SubElement(data, xlsx.X + "row", r="1")
    for i, value in enumerate(values):
        if separate_rows and i:
            row = ET.SubElement(data, xlsx.X + "row", r=str(i+1))
        ref = f'A{i+1}' if separate_rows else f'{chr(65+i)}1'
        cell = ET.SubElement(row, xlsx.X + "c", r=ref, t="inlineStr")
        ET.SubElement(ET.SubElement(cell, xlsx.X + "is"), xlsx.X + "t").text = value
    parts["xl/worksheets/sheet1.xml"] = ET.tostring(root)
    return pack(parts)


def engine_for(value, label="FULLNAME"):
    engine = PiiEngine("unused")
    def detect(text, ctl=None):
        start = text.find(value)
        return [] if start < 0 else [{"start": start, "end": start+len(value),
                                     "label": label, "score": 0.99,
                                     "source": "modello", "validated": False}]
    engine.detect_model = detect
    return engine


class SourceBoundariesTest(unittest.TestCase):
    def test_cross_cell_model_span_is_projected_and_redacted(self):
        for rows, separator in ((False, " | "), (True, "\n")):
            with self.subTest(rows=rows):
                data = book(["Mario", "Rossi"], rows)
                result = xlsx.anonymize_xlsx(data, engine_for("Mario"+separator+"Rossi"),
                                             make_preview=False)
                mapping = result["analysis"]["mapping"]
                self.assertEqual(set(mapping.values()), {"Mario", "Rossi"})
                self.assertEqual(result["report"]["residual"], [])
                self.assertFalse(xlsx.known_xlsx_leaks(result["file"], mapping))
                self.assertNotIn("Mario", xlsx.extract_text(result["file"]))
                self.assertNotIn("Rossi", xlsx.extract_text(result["file"]))

    def test_cross_cell_phone_cannot_enter_mapping(self):
        # Each source value is below the model phone post-check's digit minimum.
        with self.assertRaisesRegex(xlsx.XlsxError, "Nessuna PII trovata"):
            xlsx.anonymize_xlsx(book(["731", "842"]),
                    engine_for("731 | 842", "TELEPHONENUM"), make_preview=False)

    def test_accepted_cross_cell_phones_keep_separate_mappings(self):
        result = xlsx.anonymize_xlsx(book(["7777777", "8888888"]),
                    engine_for("7777777 | 8888888", "TELEPHONENUM"), make_preview=False)
        self.assertEqual(set(result["analysis"]["mapping"].values()), {"7777777", "8888888"})
        self.assertEqual(result["report"]["residual"], [])

    def test_actual_pipes_and_newlines_inside_cells_remain_data(self):
        for value in ("Mario | Rossi", "Mario\nRossi"):
            with self.subTest(value=value):
                mapping = {"[FULLNAME_1]": value}
                data = book([value])
                self.assertTrue(xlsx.known_xlsx_leaks(data, mapping))
                self.assertEqual(xlsx._verify_residuals(data, list(mapping.items())), list(mapping))
                result = xlsx.anonymize_xlsx(data, engine_for(value), make_preview=False)
                self.assertEqual(list(result["analysis"]["mapping"].values()), [value])
                self.assertFalse(xlsx.known_xlsx_leaks(result["file"], mapping))

    def test_residual_checks_do_not_join_independent_cells(self):
        for rows, value in ((False, "Mario | Rossi"), (True, "Mario Rossi")):
            with self.subTest(rows=rows):
                data = book(["Mario", "Rossi"], rows)
                mapping = {"[FULLNAME_1]": value}
                self.assertFalse(xlsx.known_xlsx_leaks(data, mapping))
                self.assertEqual(xlsx._verify_residuals(data, list(mapping.items())), [])
                self.assertFalse(contains_literal(xlsx.residual_text(data), value, "[FULLNAME_1]"))

    def test_context_labels_are_not_allocated(self):
        text = SourceText.join(" | ", [SourceText.labeled("Referente", "Mario Rossi"), "Safe"])
        result = engine_for("Referente: Mario Rossi").analyze(text)
        self.assertEqual(list(result["mapping"].values()), ["Mario Rossi"])
        self.assertEqual(result["entities"][0]["start"], len("Referente: "))

    def test_custom_terms_respect_boundaries_without_losing_fragments(self):
        text = SourceText.join(" | ", ["Mario", "Rossi"])
        result = engine_for("absent").analyze(text, custom_terms=[{"text": "Mario | Rossi", "tag": "CUSTOM"}])
        self.assertEqual(set(result["mapping"].values()), {"Mario", "Rossi"})

    def test_plain_text_keeps_multiline_entity(self):
        value = "Mario\nRossi"
        result = engine_for(value).analyze(value)
        self.assertEqual(list(result["mapping"].values()), [value])

    def test_ocr_append_preserves_cell_and_ocr_offsets(self):
        text = SourceText.join(" | ", ["Mario", "Rossi"])
        engine = engine_for("Mario | Rossi\n\nLuigi")
        with patch.object(image_ocr, "corpus", return_value=("Luigi", [(0, 5, 0, 0)])), \
             patch.object(image_ocr, "plan_from_entities") as plan, \
             patch.object(image_ocr, "allocate_unreadable", return_value=0), \
             patch.object(image_ocr, "allocate_signatures", return_value=0):
            result = image_ocr.analyze_with_corpus(engine, text, {"images": []})
        self.assertEqual(set(result["mapping"].values()), {"Mario", "Rossi", "Luigi"})
        self.assertEqual(plan.call_args.args[2], len(text)+2)
        self.assertEqual(result["entities"][-1]["start"], len(text)+2)


class RegistryBoundariesTest(unittest.TestCase):
    setUp = WorkbookPrivacyTest.setUp
    tearDown = WorkbookPrivacyTest.tearDown
    register = WorkbookPrivacyTest.register

    def test_cross_cell_entity_does_not_pollute_registry(self):
        conv = self.session.get(ca.Conversation, "chat")
        engine = ca.ConversationEngine(engine_for("Mario | Rossi"), self.session, conv)
        result = xlsx.anonymize_xlsx(book(["Mario", "Rossi"]), engine, make_preview=False)
        mapping = ca.conversation_mapping(self.session, "chat", include_aliases=True)
        self.assertNotIn("Mario | Rossi", [v for _, v in mapping.items()])
        self.assertEqual(result["report"]["residual"], [])

    def test_existing_registry_value_is_not_forced_across_cells(self):
        self.register("Mario | Rossi")
        conv = self.session.get(ca.Conversation, "chat")
        engine = ca.ConversationEngine(engine_for("absent"), self.session, conv)
        result = engine.analyze(SourceText.join(" | ", ["Mario", "Rossi"]))
        self.assertEqual(result["entities"], [])
        result = engine.analyze(SourceText("Mario | Rossi"))
        self.assertEqual(len(result["entities"]), 1)


# Imported helper class is not part of this suite.
del WorkbookPrivacyTest

if __name__ == "__main__":
    unittest.main()

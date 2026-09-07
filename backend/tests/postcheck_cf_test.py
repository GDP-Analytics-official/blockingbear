"""Model CF validation: false codes, international IDs, OCR and explicit terms."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.engine.core import PiiEngine, post_check


def candidate(text, value):
    start = text.index(value)
    return {"label": "CF", "start": start, "end": start + len(value),
            "score": .99, "validated": False, "source": "modello"}


class CfPostcheckTest(unittest.TestCase):
    def test_technical_codes_rejected(self):
        for value in ("T0011", "T0166", "T0554", "ABC123", "2024", "12345", "Italia"):
            with self.subTest(value=value):
                self.assertEqual(post_check([candidate(value, value)], value), [])

    def test_italian_shape_and_checksum(self):
        for value, valid in (("RSSMRA80A01H501U", True), ("RSSMRA80A01H501A", False)):
            text = "Codice fiscale: " + value
            out = post_check([candidate(text, value)], text)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["validated"], valid)

    def test_partial_span_expanded(self):
        text = "Codice fiscale: RSSMRA80A01H501U"
        out = post_check([candidate(text, "MRA80A01")], text)
        self.assertEqual(text[out[0]["start"]:out[0]["end"]], "RSSMRA80A01H501U")

    def test_international_forms(self):
        for text, value in (("DNI 12345678Z", "12345678Z"),
                            ("SSN: 123-45-6789", "123-45-6789"),
                            ("NINO: AB123456C", "AB123456C"),
                            ("Steuer-ID 12345678901", "12345678901"),
                            ("NIR 1 80 01 75 001 001 01", "1 80 01 75 001 001 01")):
            with self.subTest(text=text):
                self.assertTrue(post_check([candidate(text, value)], text))

    def test_custom_terms_and_regex_remain_authoritative(self):
        engine = PiiEngine("unused")
        engine.detect_model = lambda text, **kw: [candidate(text, "T0011")]
        self.assertEqual(engine.analyze("T0011")["mapping"], {})
        result = engine.analyze("T0011", custom_terms=[{"text": "T0011", "tag": "CF"}])
        self.assertEqual(result["anonymized_text"], "[CF_1]")
        engine.detect_model = lambda *a, **kw: []
        self.assertIn("RSSMRA80A01H501U", engine.analyze("RSSMRA80A01H501U")["mapping"].values())


if __name__ == "__main__":
    unittest.main()

"""Phone post-check regressions using synthetic model output, without weights."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.engine.core import PiiEngine, post_check
from app.engine.source_text import SourceText


def candidate(text, value):
    start = text.index(value)
    return {"label": "TELEPHONENUM", "start": start, "end": start + len(value),
            "score": .99, "validated": False, "source": "modello"}


class PhonePostcheckTest(unittest.TestCase):
    def check_phone(self, text, value=None, expected=None):
        value = text if value is None else value
        out = post_check([candidate(text, value)], text)
        self.assertEqual([text[e["start"]:e["end"]] for e in out],
                         [] if expected is None else [expected])
        for entity in out:
            self.assertFalse(entity["validated"])
            self.assertEqual(entity["source"], "modello")

    def test_digit_limits_inclusive(self):
        for size in (0, 4, 6, 7, 9, 11, 15, 16, 30):
            value = "7" * size or "letters"
            with self.subTest(size=size):
                self.check_phone(value, expected=value if 7 <= size <= 15 else None)
        self.check_phone("Telefono interno: 1234", "1234")

    def test_printed_separators_and_international_prefixes(self):
        for value in ("02 1234567", "39 333 1234567", "0039 333 1234567",
                      "02.123.4567", "02/1234567", "02) 1234567",
                      "333\u00a0123\u202f4567", "333-123-4567"):
            with self.subTest(value=value):
                self.check_phone("Telefono: +" + value, value, value)
        self.check_phone("+123 456 789 012 3456")

    def test_complete_number_recovered_from_model_fragment(self):
        for text, fragment in (("Telefono: 02 1234567", "1234567"),
                               ("Telefono: 3331234567", "1234"),
                               ("Telefono: +39 333 1234567", "333 123")):
            with self.subTest(text=text):
                expected = text.split(": ")[1].lstrip("+")
                self.check_phone(text, fragment, expected)
        self.check_phone("1234567890123456", "456789012")
        self.check_phone("123 456 789 012 3456", "456 789")
        self.check_phone("123   4567890123456", "4567890123456")

    def test_extension_counts_towards_total(self):
        self.check_phone("+39 333 1234567 interno 123", "333 1234567",
                         "39 333 1234567 interno 123")
        for extension in ("interno 1234", "ext. 1234", "poste 1234"):
            value = "+39 333 1234567 " + extension
            with self.subTest(extension=extension):
                self.check_phone(value)
                self.check_phone(value, "333 1234567")

    def test_explicit_calendar_dates(self):
        for cue in ("Data:", "Data di nascita:", "nato il", "Data fattura:",
                    "Date of birth:", "Geburtsdatum:", "Fecha de nacimiento:"):
            with self.subTest(cue=cue):
                self.check_phone(cue + " 12/03/2024", "12/03/2024")
                self.check_phone(cue + " 12/03/2024", "03/2024")
        self.check_phone("Data: 2024-03-12", "2024-03-12")
        self.check_phone("Data: 31/02/2024", "31/02/2024", "31/02/2024")
        self.check_phone("Telefono: 12/03/2024", "12/03/2024", "12/03/2024")
        self.check_phone("12/03/2024", expected="12/03/2024")

    def test_currency_amounts_including_partial_model_spans(self):
        for text, value in (("Totale: € 1.234.567,89", "1.234.567,89"),
                            ("Totale: 1234567 EUR", "1234567"),
                            ("USD 1234567.89", "1234567.89"),
                            ("Totale: € 1.234.567,89", "234.567")):
            with self.subTest(text=text, value=value):
                self.check_phone(text, value)

    def test_cap_and_unrelated_context(self):
        self.check_phone("CAP: 20121", "20121")
        self.check_phone("CAP: 20121; telefono: 123456789", "123456789", "123456789")
        self.check_phone("Data: 12/03/2024; telefono: 123456789", "123456789", "123456789")
        self.check_phone("Totale € 1234567; telefono: 7654321", "7654321", "7654321")

    def test_neighbouring_phone_is_not_discarded_with_a_date(self):
        value = "12/03/2024 tel. 1234567"
        self.check_phone("Data: " + value, value, value)

    def test_source_cells_are_checked_individually(self):
        for separator in (" | ", " ", "\n"):
            text = SourceText.join(separator, ["1234", "5678"])
            with self.subTest(separator=separator):
                self.check_phone(text)
        text = SourceText.join(" | ", ["1234567", "1234"])
        self.check_phone(text, expected="1234567")
        text = SourceText.join(" | ", ["1234567", "7654321"])
        out = post_check([candidate(text, text)], text)
        self.assertEqual([text[e["start"]:e["end"]] for e in out], ["1234567", "7654321"])

    def test_source_labels_and_context_boundaries(self):
        text = SourceText.labeled("Data di nascita", "12/03/2024")
        self.check_phone(text, "12/03/2024")
        text = SourceText.join(" ", ["Data:", "12/03/2024"])
        self.check_phone(text, "12/03/2024", "12/03/2024")
        text = SourceText.join(" ", ["1234", "5678901"])
        self.check_phone(text, "5678901", "5678901")

    def test_other_labels_and_custom_terms_are_preserved(self):
        engine = PiiEngine("unused")
        engine.detect_model = lambda text, **kw: [candidate(text, "1234")]
        self.assertEqual(engine.analyze("1234")["mapping"], {})
        result = engine.analyze("1234", custom_terms=[{"text": "1234", "tag": "TELEPHONENUM"}])
        self.assertEqual(result["anonymized_text"], "[TELEPHONENUM_1]")
        entity = dict(candidate("1234", "1234"), label="DOCID")
        self.assertEqual(post_check([entity], "1234"), [entity])

    def test_regex_phone_path_is_not_filtered(self):
        engine = PiiEngine("unused")
        engine.detect_model = lambda text, **kw: [candidate(text, text)]
        text = "1234"
        regex_entity = dict(candidate(text, text), source="regex")
        with patch("app.engine.core.detect_regex", return_value=[regex_entity]):
            self.assertEqual(engine.analyze(text)["entities"][0]["source"], "regex")

    def test_date_and_amount_keep_their_own_categories(self):
        engine = PiiEngine("unused")
        for text, value, label in (("Data: 12/03/2024", "12/03/2024", "DATE"),
                                   ("€ 1234567", "1234567", "AMOUNT")):
            with self.subTest(label=label):
                engine.detect_model = lambda text, **kw: [candidate(text, value)]
                result = engine.analyze(text)
                self.assertNotIn("TELEPHONENUM", result["by_label"])
                self.assertIn(label, result["by_label"])
                self.assertEqual(engine.analyze(text, excluded=[label])["mapping"], {})


if __name__ == "__main__":
    unittest.main()

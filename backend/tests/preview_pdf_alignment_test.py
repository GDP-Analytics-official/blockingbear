"""PDF preview regression: OCR/native occurrences must exist on both sides.

Synthetic documents only; no model inference, external services or database.
Run inside the backend image with app/ and tests/ mounted under /app:
    python /app/tests/preview_pdf_alignment_test.py
"""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf as fitz
from app import chat_anonymization as ca, chat_staging


class PdfPreviewAlignmentTest(unittest.TestCase):
    def test_mixed_ocr_and_native_occurrences(self):
        value, ph = 'Alice Example', '[FULLNAME_1]'
        mapping = {ph: value}
        with fitz.open() as raster:
            page = raster.new_page(width=400, height=100)
            page.insert_text((20, 60), value, fontsize=24)
            image = page.get_pixmap().tobytes('png')
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=1000)
            xref = page.insert_image(fitz.Rect(50, 450, 450, 550), stream=image)
            for _ in range(3):
                page = doc.new_page(width=600, height=1000)
                page.insert_text((50, 500), value, fontsize=24)
            original = doc.tobytes()
        cache = {'v': 1, 'images': [{
            'key': str(xref), 'page': 0, 'ext': 'png', 'w': 400, 'h': 100,
            'lines': [{'t': value, 's': 0.99, 'b': [20, 34, 190, 67]}],
            'regions': [], 'plan': [],
        }]}
        redacted, report = ca._redact_plain(original, '.pdf', mapping, ocr_cache=cache)
        before = copy.deepcopy(report['boxes'])
        orig, anon, info = chat_staging.build_preview(
            '.pdf', original, redacted, list(mapping.items()), mapping, [],
            report['boxes'], cache)
        self.assertEqual(orig, original)
        self.assertEqual(anon, redacted)
        self.assertEqual(info['page_sizes']['original'], info['page_sizes']['anonymized'])
        self.assertEqual(set(info['original_boxes']), {0, 1, 2, 3})
        self.assertEqual(info['original_boxes'][0], info['anonymized_boxes'][0])
        self.assertIsNot(info['original_boxes'][0][0], info['anonymized_boxes'][0][0])
        self.assertEqual(sum(len(b) for b in info['original_boxes'].values()), 4)
        self.assertEqual(report['boxes'][0][0]['x0'], before[0][0]['x0'])
        self.assertEqual(len(report['boxes'][0]), len(before[0]))

    def test_local_ocr_labels_are_included_but_seals_are_not(self):
        with fitz.open() as doc:
            doc.new_page(width=600, height=1000)
            data = doc.tobytes()
        box = {'x0': 10, 'y0': 10, 'x1': 100, 'y1': 30,
               'ph': '[UNREADABLE_1]', 'ocr': True}
        report = {0: [box, {**box, 'ph': '[SEALED_1]', 'sealed': 1}]}
        _, _, info = chat_staging.build_preview('.pdf', data, data, [], {}, [], report)
        self.assertEqual(len(info['original_boxes'][0]), 1)
        self.assertEqual(info['original_boxes'][0][0]['ph'], '[UNREADABLE_1]')
        self.assertEqual(len(info['anonymized_boxes'][0]), 2)

    def test_legacy_pdf_without_redaction_report_still_opens(self):
        with fitz.open() as doc:
            doc.new_page(width=600, height=1000)
            data = doc.tobytes()
        _, _, info = chat_staging.build_preview('.pdf', data, data, [], {}, [])
        self.assertEqual(info['original_boxes'], {})
        self.assertEqual(info['anonymized_boxes'], {})

    def test_json_report_keys_merge_into_existing_native_page(self):
        mapping = {'[FULLNAME_1]': 'Alice Example'}
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=1000)
            page.insert_text((50, 500), 'Alice Example', fontsize=24)
            data = doc.tobytes()
        redacted, report = ca._redact_plain(data, '.pdf', mapping)
        report['boxes'][0].append({'x0': 50, 'y0': 100, 'x1': 150, 'y1': 120,
                                   'ph': '[FULLNAME_1]', 'ocr': True})
        # Chat staging loads the redaction report from JSON: page keys are
        # strings while _value_boxes returns integer keys for the same page.
        saved_boxes = json.loads(json.dumps(report['boxes']))
        _, _, info = chat_staging.build_preview(
            '.pdf', data, redacted, list(mapping.items()), mapping, [], saved_boxes)
        self.assertEqual(set(info['original_boxes']), {0})
        self.assertEqual(len(info['original_boxes'][0]), 2)
        serialized = json.loads(json.dumps(info))
        self.assertEqual(len(serialized['original_boxes']['0']), 2)


if __name__ == '__main__':
    unittest.main()

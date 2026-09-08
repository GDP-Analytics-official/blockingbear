"""PDF signature OR, deduplication and destructive redaction regressions.

Synthetic documents stay in memory. Model outputs are controlled here to test
geometry and pipeline behavior independently of recognition quality.
Run in an isolated backend container: python tests/pdf_signatures_test.py
"""

import io
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz
import numpy as np
from PIL import Image, ImageDraw

from app.engine import image_ocr, pdf, pdf_signatures
from app.engine.progress import JobCanceled, JobControl


def document(rotation=0, crop=False, repeated=False, vector=False):
    with fitz.open() as doc:
        image = Image.new('RGB', (200, 100), 'white')
        ImageDraw.Draw(image).line([(20, 50), (65, 20), (110, 70), (170, 40)],
                                   fill=(0, 0, 255), width=5)
        buf = io.BytesIO()
        image.save(buf, format='PNG')
        for _ in range(2 if repeated else 1):
            page = doc.new_page(width=400, height=600)
            page.insert_text((35, 45), 'Native header stays selectable.')
            page.insert_text((35, 550), 'Native footer stays selectable.')
            if vector:
                page.draw_polyline([(120, 350), (165, 320), (210, 370), (270, 340)],
                                   color=(0, 0, 1), width=3)
            else:
                page.insert_image(fitz.Rect(100, 300, 300, 400), stream=buf.getvalue())
            if crop:
                page.set_cropbox(fitz.Rect(20, 20, 380, 580))
            page.set_rotation(rotation)
        return doc.tobytes()


class NoOCR:
    def __init__(self):
        self.calls = 0

    def __call__(self, array):
        self.calls += 1
        return types.SimpleNamespace(boxes=None, txts=None, scores=None)


class NoPII:
    def analyze(self, text, **kw):
        return {'mapping': {}, 'entities': [], 'by_label': {}}


def cache_for(data, image_regions=True, page_regions=True, second=False):
    with fitz.open(stream=data, filetype='pdf') as doc:
        page = doc[0]
        unrotated = fitz.Rect(105, 305, 285, 395)
        # Account for the fixture's CropBox offset in unrotated page space.
        offset = page.cropbox_position
        unrotated += (-offset.x, -offset.y, -offset.x, -offset.y)
        visual = unrotated * page.rotation_matrix
        visual_size = page.rect

    def detector(im):
        if im.size == (200, 100):
            return [{'b': [10, 10, 180, 90], 's': .8}] if image_regions else []
        if not page_regions:
            return []
        scale = fitz.Matrix(im.width / visual_size.width, im.height / visual_size.height)
        boxes = [{'b': list(visual * scale), 's': .9}]
        if second:
            boxes.append({'b': list(fitz.Rect(40, 150, 100, 190) * scale), 's': .7})
        return boxes

    ocr = NoOCR()
    with patch.object(image_ocr, '_get_ocr', return_value=ocr), \
            patch.object(image_ocr, 'detect_regions', side_effect=detector) as det:
        cache = image_ocr.build_pdf_cache(data)
    return cache, ocr.calls, det.call_count


def planned(cache):
    result = image_ocr.analyze_with_corpus(NoPII(), '', cache)
    return result['mapping']


class SignatureTests(unittest.TestCase):
    def test_either_pass_is_sufficient_and_both_allocate_once(self):
        for first, second, count in [(False, False, 0), (True, False, 1),
                                      (False, True, 1), (True, True, 1)]:
            with self.subTest(image=first, page=second):
                cache, ocr_calls, detector_calls = cache_for(document(), first, second)
                mapping = planned(cache)
                self.assertEqual(len(mapping), count)
                self.assertEqual(ocr_calls, 1, 'The page must never receive text OCR')
                self.assertEqual(detector_calls, 2, 'Both passes always run')

    def test_duplicate_boxes_keep_the_union_and_highest_score(self):
        cache, _, _ = cache_for(document())
        entry = next(im for im in cache['images'] if im.get('pdf_signature'))
        self.assertEqual(len(entry['regions']), 1)
        box = entry['regions'][0]
        np.testing.assert_allclose(box['b'], [105, 305, 285, 395], atol=.001)
        self.assertEqual(box['s'], .9)

    def test_page_pass_finds_another_signature_after_image_success(self):
        cache, _, calls = cache_for(document(), second=True)
        self.assertEqual(len(planned(cache)), 2)
        self.assertEqual(calls, 2)

    def test_contained_detections_merge_without_joining_neighboring_signatures(self):
        regions = [{'b': [10, 10, 90, 90], 's': .8},
                   {'b': [30, 30, 60, 60], 's': .9},
                   {'b': [95, 10, 130, 90], 's': .7}]
        merged = pdf_signatures._merge(regions, image_ocr.SIG_IOU)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0], {'b': [10., 10., 90., 90.], 's': .9})

    def test_tiled_image_and_page_detections_share_one_placeholder(self):
        from pdf_image_regions_test import document as tiled_document
        data = tiled_document(rows=24)
        images = image_ocr.pdf_images(data)
        region = next(im for im in images if im.get('pdf_region'))
        rect = fitz.Rect(region['pdf_region']['rect'])

        def detector(im):
            if im.width < 1000:
                return [{'b': [0, 0, im.width, im.height], 's': .8}]
            scale = fitz.Matrix(im.width / 600, im.height / 820)
            return [{'b': list(rect * scale), 's': .9}]

        with patch.object(image_ocr, '_get_ocr', return_value=NoOCR()), \
                patch.object(image_ocr, 'detect_regions', side_effect=detector):
            cache = image_ocr.build_pdf_cache(data, images=images)
        self.assertEqual(len(planned(cache)), 1)

    def test_anonymize_pdf_uses_page_pass_for_native_text_with_vector_signature(self):
        data = document(vector=True)

        def detector(im):
            scale = fitz.Matrix(im.width / 400, im.height / 600)
            return [{'b': list(fitz.Rect(105, 305, 285, 395) * scale), 's': .9}]

        with patch.object(image_ocr, 'detect_regions', side_effect=detector), \
                patch.object(image_ocr, '_get_ocr', side_effect=AssertionError('Text OCR')):
            result = pdf.anonymize_pdf(data, NoPII(), ocr=True)
        self.assertEqual(list(result['analysis']['mapping']), ['[SIGNATURE_1]'])
        self.assertEqual(result['report']['image_occurrences'], 1)

    def test_original_and_exported_image_pixels_are_actually_removed(self):
        data = document()
        cache, _, _ = cache_for(data)
        mapping = planned(cache)
        result = pdf.rebuild_pdf(data, mapping, ocr_cache=cache)
        self.assertEqual(result['report']['image_occurrences'], 1)
        self.assertEqual(len(result['original_boxes'][0]), 1)
        self.assertEqual(len(result['anonymized_boxes'][0]), 1)
        with fitz.open(stream=result['pdf'], filetype='pdf') as doc:
            text = doc[0].get_text()
            self.assertIn('Native header stays selectable.', text)
            self.assertIn('Native footer stays selectable.', text)
            self.assertIn('[SIGNATURE_1]', text)
            # Check every surviving image resource, not just the visible overlay.
            for item in doc[0].get_images(full=True):
                image = Image.open(io.BytesIO(doc.extract_image(item[0])['image'])).convert('RGB')
                arr = np.asarray(image)
                blue = (arr[:, :, 2] > 180) & (arr[:, :, 0] < 80) & (arr[:, :, 1] < 80)
                self.assertFalse(blue.any(), 'Signature pixels survived in a PDF image')

    def test_rotation_and_crop_preserve_page_and_cover_signature(self):
        for rotation in (0, 90, 180, 270):
            for crop in (False, True):
                with self.subTest(rotation=rotation, crop=crop):
                    data = document(rotation=rotation, crop=crop)
                    cache, _, _ = cache_for(data)
                    mapping = planned(cache)
                    self.assertEqual(len(mapping), 1)
                    out, boxes, _ = image_ocr.redact_pdf_images(data, cache, mapping)
                    with fitz.open(stream=data, filetype='pdf') as before, \
                            fitz.open(stream=out, filetype='pdf') as after:
                        self.assertEqual(before[0].rotation, after[0].rotation)
                        self.assertEqual(before[0].cropbox, after[0].cropbox)
                        self.assertIn('Native header stays selectable.', after[0].get_text())
                        pix = after[0].get_pixmap(colorspace=fitz.csRGB)
                        a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)
                        blue = (a[:, :, 2] > 180) & (a[:, :, 0] < 80) & (a[:, :, 1] < 80)
                        self.assertFalse(blue.any())
                        self.assertEqual(len(boxes[0]), 1)

    def test_vector_signature_next_to_native_text_is_not_rasterized(self):
        data = document(vector=True)
        cache, ocr_calls, detector_calls = cache_for(data, image_regions=False)
        self.assertEqual(ocr_calls, 0)
        self.assertEqual(detector_calls, 1)
        out = pdf.rebuild_pdf(data, planned(cache), ocr_cache=cache)['pdf']
        with fitz.open(stream=out, filetype='pdf') as doc:
            self.assertEqual(doc[0].get_images(), [])
            self.assertIn('Native header stays selectable.', doc[0].get_text())
            self.assertFalse(any(d['color'] == (0, 0, 1) for d in doc[0].get_drawings()))

    def test_shared_image_is_covered_on_every_page(self):
        data = document(repeated=True)
        cache, ocr_calls, detector_calls = cache_for(data)
        self.assertEqual(ocr_calls, 1)
        self.assertEqual(detector_calls, 3)
        mapping = planned(cache)
        self.assertEqual(len(mapping), 2)
        out, boxes, _ = image_ocr.redact_pdf_images(data, cache, mapping)
        self.assertEqual({p: len(b) for p, b in boxes.items()}, {0: 1, 1: 1})
        with fitz.open(stream=out, filetype='pdf') as doc:
            for page in doc:
                self.assertIn('Native footer stays selectable.', page.get_text())

    def test_signature_crossing_page_edge_loses_hidden_pixels_too(self):
        with fitz.open(stream=document(), filetype='pdf') as doc:
            doc[0].set_cropbox(fitz.Rect(0, 0, 200, 600))
            data = doc.tobytes()
        cache, _, _ = cache_for(data, image_regions=True, page_regions=False)
        out, _, _ = image_ocr.redact_pdf_images(data, cache, planned(cache))
        with fitz.open(stream=out, filetype='pdf') as doc:
            for item in doc[0].get_images(full=True):
                im = Image.open(io.BytesIO(doc.extract_image(item[0])['image'])).convert('RGB')
                a = np.asarray(im)
                blue = (a[:, :, 2] > 180) & (a[:, :, 0] < 80) & (a[:, :, 1] < 80)
                self.assertFalse(blue.any(), 'Cropped-away signature pixels remain extractable')

    def test_restoring_one_repeated_signature_keeps_other_page_protected(self):
        data = document(repeated=True)
        cache, _, _ = cache_for(data)
        mapping = planned(cache)
        restored_ph = next(im['plan'][0]['ph'] for im in cache['images']
                           if im.get('pdf_signature') and im['page'] == 1)
        mapping.pop(restored_ph)
        out, _, _ = image_ocr.redact_pdf_images(data, cache, mapping)
        with fitz.open(stream=out, filetype='pdf') as doc:
            has_ink = []
            for page in doc:
                pix = page.get_pixmap(colorspace=fitz.csRGB)
                a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)
                has_ink.append(bool(((a[:, :, 2] > 180) & (a[:, :, 0] < 80) & (a[:, :, 1] < 80)).any()))
            self.assertEqual(has_ink, [False, True])

    def test_cache_roundtrip_restoration_and_no_new_inference(self):
        data = document()
        cache, _, _ = cache_for(data)
        mapping = planned(cache)
        cache = json.loads(json.dumps(cache))
        with patch.object(image_ocr, 'detect_regions', side_effect=AssertionError('New inference')):
            redacted, boxes, _ = image_ocr.redact_pdf_images(data, cache, mapping)
            restored, no_boxes, _ = image_ocr.redact_pdf_images(data, cache, {})
        self.assertNotEqual(redacted, data)
        self.assertTrue(boxes)
        self.assertEqual(restored, data)
        self.assertEqual(no_boxes, {})

    def test_old_xref_signature_cache_still_redacts(self):
        data = document()
        im = image_ocr.pdf_images(data)[0]
        im.pop('data')
        im.update(w=200, h=100, lines=[], regions=[{'b': [10, 10, 180, 90], 's': .8}], plan=[])
        cache = {'v': 1, 'images': [im]}
        result = pdf.rebuild_pdf(data, planned(cache), ocr_cache=cache)
        self.assertEqual(result['report']['image_occurrences'], 1)

    def test_page_entries_do_not_hide_manual_ocr_selection_fallback(self):
        data = document(vector=True)
        cache, _, _ = cache_for(data, image_regions=False)
        self.assertIsNone(image_ocr.text_in_rect_cache(data, 0, [100, 300, 300, 400], cache))

    def test_cancellation_propagates_from_page_pass(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(JobCanceled):
            image_ocr.build_pdf_cache(document(vector=True), ctl=JobControl(event))

    def test_ocr_disabled_does_not_run_either_detector(self):
        with patch.object(image_ocr, 'build_pdf_cache', side_effect=AssertionError('OCR disabled')):
            with self.assertRaises(pdf.PdfError):
                pdf.anonymize_pdf(document(), NoPII(), ocr=False)


if __name__ == '__main__':
    unittest.main()

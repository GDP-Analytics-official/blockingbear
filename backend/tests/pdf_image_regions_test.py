"""Synthetic PDF region OCR regressions; no real documents or live services.

Run in the backend container: python tests/pdf_image_regions_test.py
Fixtures are generated in memory. Tests cover image composition, actual raster
removal, native text, cache selection/rebuilding, resource reuse and real OCR.
"""

import io
import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.engine import image_ocr, pdf, pdf_image_regions, pdf_ghost


def png(im):
    out = io.BytesIO()
    im.save(out, format='PNG')
    return out.getvalue()


def card(vertical=False, alpha=False):
    im = Image.new('RGBA' if alpha else 'RGB', (800, 480),
                   (245, 247, 249, 190) if alpha else (245, 247, 249))
    d = ImageDraw.Draw(im)
    d.rectangle((12, 12, 788, 468), outline=(20, 60, 130), width=8)
    d.rectangle((35, 350, 200, 425), fill=(200, 40, 60))
    font = ImageFont.truetype('DejaVuSans.ttf', 48)
    d.text((45, 70), 'MARIO ROSSI', font=font, fill=(0, 0, 0))
    d.text((45, 160), 'CODICE ZX123456', font=font, fill=(0, 0, 0))
    d.text((45, 255), 'DOCUMENTO DI PROVA',
           font=ImageFont.truetype('DejaVuSans.ttf', 34), fill=(0, 0, 0))
    return im.transpose(Image.Transpose.ROTATE_270) if vertical else im


def insert_tiles(page, im, rect, rows=12, cols=1, shuffle=False, gap=0):
    rect = fitz.Rect(rect)
    tiles = [(r, c) for r in range(rows) for c in range(cols)]
    if shuffle:
        random.Random(481).shuffle(tiles)
    for r, c in tiles:
        x0, x1 = round(im.width*c/cols), round(im.width*(c+1)/cols)
        y0, y1 = round(im.height*r/rows), round(im.height*(r+1)/rows)
        box = fitz.Rect(rect.x0+rect.width*x0/im.width + c*gap,
                        rect.y0+rect.height*y0/im.height + r*gap,
                        rect.x0+rect.width*x1/im.width + c*gap,
                        rect.y0+rect.height*y1/im.height + r*gap)
        page.insert_image(box, stream=png(im.crop((x0, y0, x1, y1))),
                          keep_proportion=False)


def document(rows=12, cols=1, vertical=False, shuffle=False, rotation=0,
             crop=False, alpha=False, overlay=False, gap=0, logo=False):
    im = card(vertical=vertical, alpha=alpha)
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=820)
        page.insert_text((45, 45), 'Native header stays selectable.', fontsize=12)
        page.insert_text((45, 770), 'Native footer stays selectable.', fontsize=12)
        rect = fitz.Rect(100, 160, 100+im.width/2, 160+im.height/2)
        insert_tiles(page, im, rect, rows, cols, shuffle, gap)
        if overlay:
            page.insert_text((130, 220), 'NATIVE OVERLAY', fontsize=14)
            page.draw_line((100, 250), (350, 250), color=(0, 1, 0), width=3)
        if logo:
            page.insert_image(fitz.Rect(440, 55, 540, 95),
                              stream=png(Image.new('RGB', (200, 80), (40, 110, 170))))
        if crop:
            page.set_cropbox(fitz.Rect(30, 50, 570, 800))
        page.set_rotation(rotation)
        return doc.tobytes()


def regions(data, render=True):
    return [i for i in image_ocr.pdf_images(data, render_pages=render)
            if i.get('pdf_region')]


def manual_cache(data):
    entries = regions(data)
    for im in entries:
        pil = Image.open(io.BytesIO(im.pop('data')))
        im.update(w=pil.width, h=pil.height, regions=[], plan=[])
        im['lines'] = [{'t': 'MARIO ROSSI', 's': .99,
                        'b': [round(pil.width*.15), round(pil.height*.15),
                              round(pil.width*.85), round(pil.height*.85)]}]
    return {'v': 1, 'images': entries}


def pixels(data, pno=0):
    with fitz.open(stream=data, filetype='pdf') as doc:
        p = doc[pno]
        p.set_rotation(0)
        pix = p.get_pixmap(alpha=False)
        return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3).copy()


class CompositionTests(unittest.TestCase):
    def test_horizontal_vertical_grid_and_shuffled_tiles(self):
        for vertical in (False, True):
            for rows, cols in ((12, 1), (1, 12), (6, 4), (24, 1)):
                for shuffle in (False, True):
                    with self.subTest(vertical=vertical, rows=rows, cols=cols, shuffle=shuffle):
                        data = document(rows, cols, vertical, shuffle)
                        found = regions(data)
                        self.assertEqual(len(found), 1)
                        self.assertEqual(len(found[0]['pdf_region']['members']), rows*cols)
                        got = Image.open(io.BytesIO(found[0]['data'])).convert('RGB')
                        expected = card(vertical).resize(got.size)
                        error = np.abs(np.asarray(got).astype(float)-np.asarray(expected))
                        self.assertLess(error.mean(), 2.5)

    def test_page_rotation_and_cropbox(self):
        for rotate in (0, 90, 180, 270):
            for crop in (False, True):
                with self.subTest(rotation=rotate, crop=crop):
                    data = document(rotation=rotate, crop=crop, vertical=True)
                    found = regions(data)
                    self.assertEqual(len(found), 1)
                    got = Image.open(io.BytesIO(found[0]['data'])).convert('RGB')
                    expected = card(True).resize(got.size)
                    self.assertLess(np.abs(np.asarray(got).astype(float)-np.asarray(expected)).mean(), 2.5)

    def test_native_overlay_and_vectors_excluded_from_ocr(self):
        clean = regions(document())[0]['data']
        overlaid = regions(document(overlay=True))[0]['data']
        self.assertEqual(clean, overlaid)

    def test_logo_does_not_rasterize_page(self):
        data = document(rows=1, logo=True)
        found = image_ocr.pdf_images(data)
        self.assertEqual(len(found), 2)
        self.assertTrue(all(i['key'].isdigit() for i in found))
        with fitz.open(stream=data, filetype='pdf') as doc:
            self.assertIn('Native header', doc[0].get_text())

    def test_logo_separate_from_reconstructed_card(self):
        data = document(logo=True)
        found = image_ocr.pdf_images(data)
        self.assertEqual(len(found), 2)
        self.assertEqual(sum(bool(i.get('pdf_region')) for i in found), 1)
        self.assertEqual(sum(i['key'].isdigit() for i in found), 1)

    def test_small_tiled_scan_without_native_text_is_still_a_region(self):
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=820)
            insert_tiles(page, card(True), (100, 150, 280, 450), rows=48)
            data = doc.tobytes()
        found = image_ocr.pdf_images(data)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].get('pdf_region'))

    def test_shared_tile_with_isolated_placement_is_not_skipped(self):
        with fitz.open(stream=document(), filetype='pdf') as doc:
            member = doc[0].get_images()[3]
            doc[0].insert_image(fitz.Rect(100, 620, 500, 640), xref=member[0])
            data = doc.tobytes()
        found = image_ocr.pdf_images(data)
        self.assertEqual(len(found), 2)
        self.assertTrue(any(i['key']==str(member[0]) for i in found))

    def test_count_has_same_regions_without_rendering(self):
        data = document(logo=True)
        full = image_ocr.pdf_images(data)
        light = image_ocr.pdf_images(data, render_pages=False)
        self.assertEqual([i['key'] for i in full], [i['key'] for i in light])
        self.assertEqual(image_ocr.count_images(data, '.pdf'), 2)
        self.assertTrue(all('data' not in i for i in light if i.get('pdf_region')))

    def test_thin_tiles_below_standalone_filter(self):
        data = document(rows=120, vertical=True)
        found = regions(data)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0]['pdf_region']['members']), 120)

    def test_separated_images_are_not_joined(self):
        self.assertEqual(regions(document(rows=2, gap=8)), [])

    def test_subpoint_gap_is_tolerated(self):
        self.assertEqual(len(regions(document(rows=12, gap=.15))), 1)

    def test_overlapping_layers_are_not_tiles(self):
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((30, 30), 'Native text')
            page.insert_image(fitz.Rect(80, 100, 400, 300), stream=png(card()))
            page.insert_image(fitz.Rect(100, 120, 420, 320), stream=png(card(True)))
            self.assertEqual(regions(doc.tobytes()), [])

    def test_two_groups_remain_separate(self):
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((30, 30), 'Native text')
            insert_tiles(page, card(), (30, 80, 430, 320))
            insert_tiles(page, card(), (30, 360, 430, 600))
            self.assertEqual(len(regions(doc.tobytes())), 2)

    def test_transparency_matches_pdf_render(self):
        data = document(alpha=True)
        entry = regions(data)[0]
        region = fitz.Rect(entry['pdf_region']['rect'])
        got = Image.open(io.BytesIO(entry['data'])).convert('RGB')
        with fitz.open(stream=data, filetype='pdf') as doc:
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(got.width/region.width, got.height/region.height),
                                    clip=region, alpha=False)
        expected = Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB')
        self.assertEqual(got.size, expected.size)
        self.assertLess(np.abs(np.asarray(got).astype(float)-np.asarray(expected)).mean(), 1)

    def test_form_xobject_rotation(self):
        source = fitz.open(stream=document(), filetype='pdf')
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation), fitz.open() as doc:
                page = doc.new_page(width=820, height=820)
                page.show_pdf_page(fitz.Rect(0, 0, 820, 820), source, 0, rotate=rotation)
                data = doc.tobytes()
                self.assertEqual(len(regions(data)), 1)
                cache = manual_cache(data)
                out, overlays, _ = image_ocr.redact_pdf_images(data, cache, {'[FULLNAME_1]':'MARIO ROSSI'})
                self.assertTrue(overlays[0])
                self.assertGreater(np.any(pixels(out) != pixels(data), axis=2).sum(), 1000)
        source.close()


class RedactionTests(unittest.TestCase):
    def test_hidden_ocr_across_tiles_is_not_a_second_text_source(self):
        with fitz.open(stream=document(cols=4), filetype='pdf') as doc:
            doc[0].insert_text((110, 195), 'SHADOW-ORIGINAL-VALUE-MARIO-ROSSI',
                               fontsize=14, render_mode=3)
            data = doc.tobytes()
        text, _ = pdf.extract_text(data)
        self.assertNotIn('SHADOW', text)
        self.assertIn('Native header', text)
        stripped, touched = pdf_ghost.strip_ghost_text(data)
        self.assertTrue(touched)
        self.assertTrue(np.array_equal(pixels(stripped), pixels(data)))
        result = pdf.rebuild_pdf(data, {'[FULLNAME_1]':'MARIO ROSSI'},
                                 ocr_cache=manual_cache(data))
        with fitz.open(stream=result['pdf'], filetype='pdf') as out:
            self.assertNotIn('SHADOW', out[0].get_text())

    def test_overlapping_images_do_not_inflate_hidden_text_coverage(self):
        with fitz.open() as doc:
            p = doc.new_page()
            for i in range(5):
                p.insert_image(fitz.Rect(100, 100, 125, 125),
                               stream=png(Image.new('RGB', (60,60), (i*30,40,50))))
            p.insert_text((100,118),'UNRELATED INVISIBLE TEXT ACROSS THE PAGE',
                          fontsize=14,render_mode=3)
            data = doc.tobytes()
        self.assertIn('UNRELATED', pdf.extract_text(data)[0])

    def test_pixels_removed_native_text_retained_and_cache_selection(self):
        for rotation in (0, 90, 180, 270):
            for crop in (False, True):
                with self.subTest(rotation=rotation, crop=crop):
                    data = document(rotation=rotation, crop=crop, vertical=True, overlay=True)
                    cache = json.loads(json.dumps(manual_cache(data)))
                    result = pdf.rebuild_pdf(data, {'[FULLNAME_1]':'MARIO ROSSI'}, ocr_cache=cache)
                    with fitz.open(stream=data, filetype='pdf') as before, fitz.open(stream=result['pdf'], filetype='pdf') as after:
                        self.assertEqual(before[0].get_text(), after[0].get_text())
                        self.assertEqual(before[0].rotation, after[0].rotation)
                        self.assertEqual(before[0].cropbox, after[0].cropbox)
                        self.assertEqual(len(before[0].get_drawings()), len(after[0].get_drawings()))
                    old, new = pixels(data), pixels(result['pdf'])
                    changed = np.any(old != new, axis=2)
                    yellow = (new[:,:,0] > 240) & (new[:,:,1] > 220) & (new[:,:,2] < 100)
                    self.assertGreater(yellow.sum(), 1000)
                    rect = fitz.Rect(cache['images'][0]['pdf_region']['rect'])
                    mask = changed.copy()
                    mask[max(0,int(rect.y0)-2):int(rect.y1)+3,
                         max(0,int(rect.x0)-2):int(rect.x1)+3] = False
                    self.assertFalse(mask.any(), 'Changes outside the image region')
                    for output in (data, result['pdf']):
                        selected = image_ocr.text_in_rect_cache(output, 0, list(rect), cache)
                        self.assertEqual(selected, 'MARIO ROSSI')

    def test_cache_redaction_is_deterministic_and_reversible(self):
        data = document(vertical=True)
        cache = manual_cache(data)
        mapping = {'[FULLNAME_1]':'MARIO ROSSI'}
        a = image_ocr.redact_pdf_images(data, cache, mapping)[0]
        b = image_ocr.redact_pdf_images(data, json.loads(json.dumps(cache)), mapping)[0]
        self.assertTrue(np.array_equal(pixels(a), pixels(b)))
        restored, boxes, counts = image_ocr.redact_pdf_images(data, cache, {})
        self.assertEqual(restored, data)
        self.assertEqual(boxes, {})
        self.assertEqual(counts, {})

    def test_every_touched_source_image_has_masked_pixels(self):
        data = document(rows=24, vertical=True)
        cache = manual_cache(data)
        expected = pdf_image_regions.image_boxes(cache['images'][0],
                    image_ocr.boxes_for(cache, {'[FULLNAME_1]':'MARIO ROSSI'})[cache['images'][0]['key']])
        out, _, _ = image_ocr.redact_pdf_images(data, cache, {'[FULLNAME_1]':'MARIO ROSSI'})
        with fitz.open(stream=out, filetype='pdf') as doc:
            # Save/garbage collection changes xrefs; match by placement transform.
            placements = doc[0].get_image_info(xrefs=True)
            for member in cache['images'][0]['pdf_region']['members']:
                if str(member['xref']) not in expected:
                    continue
                placed = next(i for i in placements if np.allclose(i['transform'], member['transform']))
                arr = np.asarray(Image.open(io.BytesIO(doc.extract_image(placed['xref'])['image'])).convert('RGB'))
                self.assertTrue(((arr[:,:,0] > 240) & (arr[:,:,1] > 220) & (arr[:,:,2] < 100)).any())

    def test_shared_xrefs_union_masks_from_multiple_regions(self):
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=820)
            page.insert_text((30, 30), 'Native text')
            insert_tiles(page, card(), (20, 80, 420, 320))
            insert_tiles(page, card(), (20, 370, 420, 610))
            data = doc.tobytes()
        cache = manual_cache(data)
        for i, entry in enumerate(cache['images']):
            w,h = entry['w'],entry['h']
            entry['lines'][0]['b'] = [w*(.1 if i==0 else .6), h*.2,
                                      w*(.4 if i==0 else .9), h*.7]
        out, overlays, _ = image_ocr.redact_pdf_images(
            data, cache, {'[FULLNAME_1]':'MARIO ROSSI'}, text=image_ocr.YELLOW)
        self.assertTrue(overlays)
        arr = pixels(out)
        for y in (170, 460):
            for x in (100, 320):
                self.assertTrue(np.allclose(arr[y,x], image_ocr.YELLOW, atol=2), (x,y,arr[y,x]))

    def test_legacy_xref_cache_still_works(self):
        data = document(rows=1)
        entry = image_ocr.pdf_images(data)[0]
        im = Image.open(io.BytesIO(entry.pop('data')))
        entry.update(w=im.width,h=im.height,regions=[],plan=[],
                     lines=[{'t':'MARIO ROSSI','s':.99,'b':[50,50,500,120]}])
        out, boxes, _ = image_ocr.redact_pdf_images(data, {'v':1,'images':[entry]}, {'[FULLNAME_1]':'MARIO ROSSI'})
        self.assertTrue(boxes)
        self.assertFalse(np.array_equal(pixels(data),pixels(out)))


class RealOcrTests(unittest.TestCase):
    def test_complete_names_recovered_from_tiled_cards(self):
        for vertical in (False, True):
            for rows,cols in ((1,1),(24,1),(6,4)):
                with self.subTest(vertical=vertical,rows=rows,cols=cols):
                    data=document(rows,cols,vertical,shuffle=True,logo=True)
                    cache=image_ocr.build_cache(image_ocr.pdf_images(data))
                    text=image_ocr.corpus(cache)[0].upper()
                    self.assertIn('MARIO ROSSI',text)
                    self.assertIn('ZX123456',text)
                    self.assertNotIn('NATIVE HEADER',text)
                    self.assertNotIn('NATIVE FOOTER',text)
                    if rows*cols>1:
                        self.assertTrue(any(i.get('pdf_region') for i in cache['images']))
                    out=pdf.rebuild_pdf(data,{'[FULLNAME_1]':'MARIO ROSSI'},ocr_cache=cache)
                    self.assertTrue(out['anonymized_boxes'])
                    # Independent OCR of the exported image region must lose the name.
                    reread=image_ocr.build_cache(image_ocr.pdf_images(out['pdf']))
                    self.assertNotIn('MARIO ROSSI',image_ocr.corpus(reread)[0].upper())


if __name__ == '__main__':
    unittest.main(verbosity=2)

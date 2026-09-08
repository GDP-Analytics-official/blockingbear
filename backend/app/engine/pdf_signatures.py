"""Combine image and full-page signature detections in PDF coordinates.

The page pass runs only the existing signature model, never text OCR. New
signature cache entries use unrotated PDF points and are redacted in the
original page content, without rasterizing the exported page.
"""

import io

import fitz
from PIL import Image

from .mupdf_lock import MUPDF_LOCK
from .progress import NULL


def _merge(regions, iou):
    """Union duplicate detections; keep the extent accepted by either pass."""
    groups = []
    for region in sorted(regions, key=lambda r: -r['s']):
        box, score = fitz.Rect(region['b']), region['s']
        if box.is_empty:
            continue
        remaining = list(groups)
        groups = []
        while remaining:
            other = remaining.pop(0)
            rect = fitz.Rect(other['b'])
            intersection = (box & rect).get_area()
            union = box.get_area() + rect.get_area() - intersection
            contained = intersection / max(min(box.get_area(), rect.get_area()), 1e-9)
            if intersection / max(union, 1e-9) >= iou or contained >= .8:
                box |= rect
                score = max(score, other['s'])
                # The expanded union can now overlap an earlier group.
                remaining.extend(groups)
                groups = []
            else:
                groups.append(other)
        groups.append({'b': list(box), 's': score})
    return sorted(groups, key=lambda r: (r['b'][1], r['b'][0]))


def add_page_pass(pdf_bytes, cache, ctl=None):
    """Keep detections from either view, allocating each PDF signature once.

    Existing image regions remain available to the unreadable-text policy
    as covered_signatures; only the combined page entry owns them. Old caches
    and vector-page OCR entries retain their existing interpretation.
    """
    from . import image_ocr

    ctl = ctl or NULL
    cache = cache or {'v': 1, 'images': []}
    images = list(cache['images'])
    transferred = {id(im): set() for im in images}
    vector_pages = {im['page'] for im in images
                    if im['key'].startswith(image_ocr.PAGE_KEY)}
    with MUPDF_LOCK:
        doc = fitz.open(stream=pdf_bytes, filetype='pdf')
        total = doc.page_count
    try:
        ctl.tick(0, total)
        for pno in range(total):
            ctl.check()
            if pno in vector_pages:
                # This image already is the complete rendered page.
                ctl.tick(pno + 1, total)
                continue
            with MUPDF_LOCK:
                page = doc[pno]
                bounds = page.rect * page.derotation_matrix
                pix = page.get_pixmap(dpi=image_ocr.PAGE_DPI,
                                      colorspace=fitz.csRGB, alpha=False)
                png = pix.tobytes('png')
                to_page = (fitz.Matrix(page.rect.width / pix.width,
                                       page.rect.height / pix.height)
                           * page.derotation_matrix)
                placements = page.get_image_info(xrefs=True)
                regions = []
                for im in images:
                    if im.get('pdf_region'):
                        if im['page'] != pno:
                            continue
                        rect = fitz.Rect(im['pdf_region']['rect'])
                        transforms = [fitz.Matrix(rect.width, 0, 0, rect.height,
                                                  rect.x0, rect.y0)]
                    else:
                        transforms = [fitz.Matrix(*info['transform'])
                                      for info in placements
                                      if str(info['xref']) == im['key']]
                    for matrix in transforms:
                        for ri, reg in enumerate(im['regions']):
                            rect = (fitz.Rect(reg['b'])
                                    * fitz.Matrix(1 / im['w'], 1 / im['h'])
                                    * matrix)
                            if rect.intersects(bounds):
                                # Keep the full source extent: cropped-away pixels
                                # must be removed from the embedded image too.
                                regions.append({'b': list(rect), 's': reg['s']})
                                transferred[id(im)].add(ri)
            # Model inference must not hold the global MuPDF lock.
            pil = Image.open(io.BytesIO(png)).convert('RGB')
            for reg in image_ocr.detect_regions(pil):
                rect = (fitz.Rect(reg['b']) * to_page) & bounds
                if not rect.is_empty:
                    regions.append({'b': list(rect), 's': reg['s']})
            regions = _merge(regions, image_ocr.SIG_IOU)
            if regions:
                cache['images'].append({
                    'key': f'signature:{pno}', 'page': pno, 'ext': 'png',
                    'w': bounds.width, 'h': bounds.height, 'pdf_signature': True,
                    'lines': [], 'regions': regions, 'plan': []})
            ctl.tick(pno + 1, total)
    finally:
        with MUPDF_LOCK:
            doc.close()
    for im in images:
        moved = transferred[id(im)]
        if moved:
            im['covered_signatures'] = [r for i, r in enumerate(im['regions']) if i in moved]
            im['regions'] = [r for i, r in enumerate(im['regions']) if i not in moved]
    return cache if any(im['lines'] or im['regions'] for im in cache['images']) else None


def redact(doc, entries, fill, text):
    """Destructively redact combined signatures while preserving native pages.

    Caller holds the MuPDF lock and has already applied source-image masks.
    Boxes use unrotated page coordinates, as do the existing PDF overlays.
    """
    from . import pdf_export

    overlay, by_ph = {}, {}
    fill = tuple(v / 255 for v in fill)
    text = tuple(v / 255 for v in text)
    for pno, boxes in entries.items():
        page = doc[pno]
        bounds = page.rect * page.derotation_matrix
        items = [(fitz.Rect(box), ph) for box, ph in boxes]
        items = [(rect, ph) for rect, ph in items if rect.intersects(bounds)]
        for rect, _ph in items:
            pdf_export._add_redact_annot(page, rect, fill)
        if not items:
            continue
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        for rect, ph in items:
            rect = rect & bounds
            size = pdf_export._fit_fontsize(ph, rect)
            if size:
                pdf_export._draw_label(page, rect, ph, size, text)
            overlay.setdefault(pno, []).append({
                'x0': rect.x0, 'y0': rect.y0, 'x1': rect.x1, 'y1': rect.y1,
                'ph': ph, 'ocr': True})
            by_ph[ph] = by_ph.get(ph, 0) + 1
    return overlay, by_ph

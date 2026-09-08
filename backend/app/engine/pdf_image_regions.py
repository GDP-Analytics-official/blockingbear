"""Compose adjacent PDF image tiles for OCR without rasterizing native text.

Geometry and rendering only; callers hold the application's MuPDF lock. The
cache records the rendered region and each original image transform so OCR
redactions can be projected back into the original raster pixels.
"""

import math

import fitz

REGION_KEY = "region:"
_EDGE_TOLERANCE = 0.75  # PDF points: scanner tiles can overlap by a fraction.


def _axis_aligned(transform):
    a, b, c, d, _e, _f = transform
    scale = max(abs(a), abs(b), abs(c), abs(d), 1)
    eps = scale * 1e-5
    return ((abs(b) < eps and abs(c) < eps)
            or (abs(a) < eps and abs(d) < eps))


def _adjacent(a, b):
    """An almost complete shared edge, not mere bbox overlap or proximity."""
    tol = _EDGE_TOLERANCE
    vertical = (abs(a[0] - b[0]) <= tol and abs(a[2] - b[2]) <= tol
                and min(abs(a[3] - b[1]), abs(b[3] - a[1])) <= tol)
    horizontal = (abs(a[1] - b[1]) <= tol and abs(a[3] - b[3]) <= tol
                  and min(abs(a[2] - b[0]), abs(b[2] - a[0])) <= tol)
    # Coincident images / substantial overlaps are layers, not adjacent tiles.
    overlap_w = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    overlap_h = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min((a[2]-a[0]) * (a[3]-a[1]),
                  (b[2]-b[0]) * (b[3]-b[1]))
    return (vertical or horizontal) and overlap_w * overlap_h < 0.2 * smaller


def tile_groups(page):
    """Connected groups of at least two visible, edge-aligned image tiles.

    Group before the standalone MIN_SIDE filter: a valid scan may consist of
    strips only a handful of pixels high. Standalone logos remain separate.
    Image resource order is unrelated to their geometric or painting order.
    """
    infos = page.get_image_info(xrefs=True)
    visible = page.rect * page.derotation_matrix
    candidates = []
    for index, info in enumerate(infos):
        rect = fitz.Rect(info['bbox'])
        if (not info.get('xref') or not info.get('width') or not info.get('height')
                or rect.is_empty or rect.is_infinite or not rect.intersects(visible)
                or not _axis_aligned(info['transform'])):
            continue
        candidates.append(dict(info, index=index))
    parents = list(range(len(candidates)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    ordered = sorted(range(len(candidates)), key=lambda i: candidates[i]['bbox'][1])
    for pos, i in enumerate(ordered):
        a = candidates[i]['bbox']
        for following in range(pos + 1, len(ordered)):
            j = ordered[following]
            b = candidates[j]['bbox']
            if b[1] > a[3] + _EDGE_TOLERANCE:
                break
            if _adjacent(a, b):
                parents[root(j)] = root(i)
    groups = {}
    for i, item in enumerate(candidates):
        groups.setdefault(root(i), []).append(item)
    return sorted((g for g in groups.values() if len(g) > 1),
                  key=lambda g: (min(i['bbox'][1] for i in g),
                                 min(i['bbox'][0] for i in g)))


def render_groups(page, groups, dpi, max_side, render=True):
    """Render only group image resources, excluding native text and vectors.

    Copying the PDF page preserves source encodings and soft masks. A temporary
    content stream paints just the selected image placements; the source PDF
    is untouched. All placements and cache rectangles use unrotated page space.
    """
    if not groups:
        return []
    out = []
    with fitz.open() as scratch:
        if render:
            scratch.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
            target = scratch[0]
            target.set_rotation(0)
            copied = target.get_image_info(xrefs=True)
            content = scratch.get_new_xref()
            scratch.update_object(content, '<<>>')
            scratch.update_stream(content, b'')
            target.set_contents(content)
        for number, group in enumerate(groups):
            clip = fitz.Rect()
            density = dpi / 72.0
            for member in group:
                clip |= fitz.Rect(member['bbox'])
                a, b, c, d, _e, _f = member['transform']
                density = max(density, member['width'] / max(math.hypot(a, b), 1e-6),
                              member['height'] / max(math.hypot(c, d), 1e-6))
            clip &= page.rect * page.derotation_matrix
            scale = min(density, max_side / max(clip.width, clip.height))
            # Match MuPDF's outward pixel rounding exactly, including CropBox offsets.
            pixels = (clip * fitz.Matrix(scale, scale)).irect
            rect = [pixels.x0 / scale, pixels.y0 / scale,
                    pixels.x1 / scale, pixels.y1 / scale]
            region = {'rect': rect, 'members': [
                {'xref': i['xref'], 'w': i['width'], 'h': i['height'],
                 'transform': list(i['transform'])} for i in group]}
            entry = {'key': f'{REGION_KEY}{page.number}:{number}',
                     'page': page.number, 'ext': 'png', 'pdf_region': region}
            if render:
                resources, commands = [], []
                for i, member in enumerate(sorted(group, key=lambda m: m['index'])):
                    xref = copied[member['index']]['xref']
                    resources.append(f'/Tile{i} {xref} 0 R')
                    # PDF image space is y-up; PyMuPDF image transforms are y-down.
                    matrix = (fitz.Matrix(1, 0, 0, -1, 0, 1)
                              * fitz.Matrix(*member['transform'])
                              * ~target.transformation_matrix)
                    values = ' '.join(f'{v:.10g}' for v in matrix)
                    commands.append(f'q {values} cm /Tile{i} Do Q')
                scratch.xref_set_key(target.xref, 'Resources',
                                     '<< /XObject << ' + ' '.join(resources) + ' >> >>')
                scratch.update_stream(content, '\n'.join(commands).encode('ascii'))
                pix = target.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip,
                                        colorspace=fitz.csRGB, alpha=False, annots=False)
                region['rect'] = [pix.x / scale, pix.y / scale,
                                  (pix.x + pix.width) / scale,
                                  (pix.y + pix.height) / scale]
                entry['data'] = pix.tobytes('png')
            out.append(entry)
    return out


def image_boxes(img, boxes):
    """Project region OCR boxes to every intersecting original image's pixels."""
    region = img['pdf_region']
    rect = fitz.Rect(region['rect'])
    out = {}
    for member in region['members']:
        matrix = fitz.Matrix(*member['transform'])
        if abs(matrix.a * matrix.d - matrix.b * matrix.c) < 1e-10:
            continue
        inverse = ~matrix
        for (x0, y0, x1, y1), ph in boxes:
            page_box = fitz.Rect(rect.x0 + x0 * rect.width / img['w'],
                                 rect.y0 + y0 * rect.height / img['h'],
                                 rect.x0 + x1 * rect.width / img['w'],
                                 rect.y0 + y1 * rect.height / img['h'])
            unit = (page_box * inverse) & fitz.Rect(0, 0, 1, 1)
            if unit.is_empty:
                continue
            pixel_box = [unit.x0 * member['w'], unit.y0 * member['h'],
                         unit.x1 * member['w'], unit.y1 * member['h']]
            out.setdefault(str(member['xref']), []).append((pixel_box, ph))
    return out

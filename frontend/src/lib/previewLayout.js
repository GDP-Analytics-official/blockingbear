// Only native PDF/image previews preserve page geometry after redaction.
// Converted Office/text documents can reflow even when page counts match.
const FIXED_EXT = /^\.(pdf|png|jpe?g|bmp|gif|tiff?|webp)$/i

export function hasFixedLayout(doc) {
  const ext = doc.ext || doc.filename?.match(/\.[^.]+$/)?.[0] || ''
  const { original = [], anonymized = [] } = doc.page_sizes || {}
  return FIXED_EXT.test(ext) && original.length > 0
    && original.length === anonymized.length
    && original.every((s, n) => s.width > 0 && s.height > 0
      && Math.abs(s.width - anonymized[n].width) < 0.01
      && Math.abs(s.height - anonymized[n].height) < 0.01)
}

// Hover twins in a fixed layout must share a page and overlap spatially.
// Older previews may lack an OCR box on the left: never shift later twins.
export function pairFixedBoxes(boxesO, boxesA, sizes) {
  const pairs = []
  for (const [page, original] of Object.entries(boxesO || {})) {
    const n = Number(page)
    if (!sizes[n]) continue
    const anon = boxesA?.[n] || []
    original.forEach((o, i) => {
      if (!o.ph || o.sealed) return
      anon.forEach((a, j) => {
        if (a.sealed || a.ph !== o.ph || !!a.ocr !== !!o.ocr) return
        const overlap = Math.max(0, Math.min(o.x1, a.x1) - Math.max(o.x0, a.x0))
          * Math.max(0, Math.min(o.y1, a.y1) - Math.max(o.y0, a.y0))
        const area = Math.min((o.x1 - o.x0) * (o.y1 - o.y0),
          (a.x1 - a.x0) * (a.y1 - a.y0))
        if (area <= 0 || overlap / area < 0.8) return
        pairs.push({
          o: { n, keys: [`${n}:${i}`] },
          a: { n, keys: [`${n}:${j}`] },
        })
      })
    })
  }
  return pairs
}

// Store a document position, not a pixel offset, across resizing, zoom and
// desktop/mobile transitions. Page gaps are represented by fractions > 1.
export function readPagePosition(container) {
  if (!container?.clientWidth) return null
  const line = container.getBoundingClientRect().top + container.clientTop
    + container.clientHeight * 0.3
  const pages = [...container.children].filter((el) => el.dataset.pageIndex != null)
  let current = null
  for (const el of pages) {
    if (current && el.getBoundingClientRect().top > line) break
    current = el
  }
  if (!current) return null
  const rect = current.getBoundingClientRect()
  return { page: Number(current.dataset.pageIndex), fraction: (line - rect.top) / rect.height }
}

export function restorePagePosition(container, position) {
  if (!container?.clientWidth || !position) return
  const el = [...container.children].find((child) => Number(child.dataset.pageIndex) === position.page)
  if (!el) return
  const line = container.getBoundingClientRect().top + container.clientTop
    + container.clientHeight * 0.3
  const rect = el.getBoundingClientRect()
  container.scrollTop += rect.top + position.fraction * rect.height - line
}

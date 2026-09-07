import assert from 'node:assert/strict'
import test from 'node:test'
import { hasFixedLayout, pairFixedBoxes } from './previewLayout.js'

const portrait = { width: 595, height: 842 }
const landscape = { width: 842, height: 595 }
const sizes = [portrait, landscape, portrait]
const doc = { filename: 'example.pdf', page_sizes: { original: sizes, anonymized: sizes } }
const box = (y, extra = {}) => ({ x0: 50, x1: 150, y0: y, y1: y + 20, ph: '[FULLNAME_1]', ...extra })

test('native PDF and image previews preserve mixed page geometry', () => {
  assert.equal(hasFixedLayout(doc), true)
  assert.equal(hasFixedLayout({ ...doc, filename: 'scan.TIFF' }), true)
  assert.equal(hasFixedLayout({ ...doc, filename: 'example.doc', ext: '.pdf' }), true)
})

test('converted formats retain content synchronization even with equal page counts', () => {
  for (const ext of ['.docx', '.txt', '.xlsx', '.pptx']) {
    assert.equal(hasFixedLayout({ ...doc, ext }), false)
  }
  assert.equal(hasFixedLayout({ ...doc, filename: 'example.txt' }), false)
})

test('inconsistent or missing page geometry cannot use paired pages', () => {
  assert.equal(hasFixedLayout({ ...doc, page_sizes: { original: sizes, anonymized: [portrait] } }), false)
  assert.equal(hasFixedLayout({ ...doc, page_sizes: { original: [portrait], anonymized: [landscape] } }), false)
  assert.equal(hasFixedLayout({ filename: 'example.pdf' }), false)
})

test('an extra OCR occurrence in an old preview does not shift native hover twins', () => {
  const original = { 1: [box(100)], 2: [box(100)] }
  const anon = { 0: [box(100, { ocr: true })], 1: [box(100)], 2: [box(100)] }
  const pairs = pairFixedBoxes(original, anon, sizes)
  assert.deepEqual(pairs.map(({ o, a }) => [o.keys, a.keys]), [
    [['1:0'], ['1:0']], [['2:0'], ['2:0']],
  ])
})

test('a missing repeated occurrence on the same page does not shift later twins', () => {
  const original = { 0: [box(100), box(300), box(500)] }
  const anon = { 0: [box(100), box(500)] }
  assert.deepEqual(pairFixedBoxes(original, anon, sizes).map(({ o, a }) => [o.keys[0], a.keys[0]]), [
    ['0:0', '0:0'], ['0:2', '0:1'],
  ])
})

test('clipped redaction boxes retain twins; seals and unrelated boxes do not', () => {
  const original = { 0: [box(100), box(300, { ocr: true }), box(500)] }
  const anon = { 0: [box(103, { y1: 116 }), box(300, { ocr: true }),
    box(500, { sealed: 1 }), box(100, { ph: '[FULLNAME_2]' })] }
  assert.deepEqual(pairFixedBoxes(original, anon, sizes).map(({ o, a }) => [o.keys[0], a.keys[0]]), [
    ['0:0', '0:0'], ['0:1', '0:1'],
  ])
})

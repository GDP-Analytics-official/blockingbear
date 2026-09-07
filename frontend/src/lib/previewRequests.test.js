import test from 'node:test'
import assert from 'node:assert/strict'
import { createPreviewQueue } from './previewRequests.js'

const tick = () => new Promise((resolve) => setImmediate(resolve))

test('many pages share a bounded renderer queue', async () => {
  const queue = createPreviewQueue(4)
  let active = 0, peak = 0
  const pages = Array.from({ length: 1000 }, (_, n) => queue(async () => {
    active++
    peak = Math.max(peak, active)
    await tick()
    active--
    return n
  }, new AbortController().signal))
  assert.deepEqual(await Promise.all(pages), Array.from({ length: 1000 }, (_, n) => n))
  assert.equal(peak, 4)
})

test('leaving a page cancels queued work before a request starts', async () => {
  const queue = createPreviewQueue(1)
  let finish
  const first = queue(() => new Promise((resolve) => { finish = resolve }), new AbortController().signal)
  await tick()
  const controller = new AbortController()
  let started = false
  const skipped = queue(() => { started = true }, controller.signal)
  const rejected = assert.rejects(skipped, { name: 'AbortError' })
  controller.abort()
  finish('first')
  await Promise.all([first, rejected])
  assert.equal(started, false)
  assert.equal(await queue(async () => 'last', new AbortController().signal), 'last')
})

test('active cancellation reaches the fetch and releases the slot', async () => {
  const queue = createPreviewQueue(1)
  const controller = new AbortController()
  const pending = queue((signal) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  }), controller.signal)
  const rejected = assert.rejects(pending, { name: 'AbortError' })
  await tick()
  controller.abort()
  await rejected
  assert.equal(await queue(async () => 'next', new AbortController().signal), 'next')
})

test('a late blob still reaches its owner for cleanup after cancellation', async () => {
  const queue = createPreviewQueue(1)
  const controller = new AbortController()
  let finish
  const late = queue(() => new Promise((resolve) => { finish = resolve }), controller.signal)
  await tick()
  controller.abort()
  finish('blob:late-result')
  assert.equal(await late, 'blob:late-result')
})

test('failed and synchronously throwing loads do not block following pages', async () => {
  const queue = createPreviewQueue(1)
  await assert.rejects(queue(() => { throw new Error('render failed') }, new AbortController().signal), /render failed/)
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(queue(() => assert.fail('aborted load started'), controller.signal), { name: 'AbortError' })
  assert.equal(await queue(async () => 42, new AbortController().signal), 42)
})

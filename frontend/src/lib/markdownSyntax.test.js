import assert from 'node:assert/strict'
import test from 'node:test'
import { splitBlocks, isTableDelimiter, utf16Entities } from './markdownSyntax.js'
import { restorePlaceholders } from './placeholders.js'

test('code fences support actual language identifiers and CRLF', () => {
  for (const lang of ['c++', 'c#', 'objective-c', 'python', '']) {
    assert.deepEqual(splitBlocks('```' + lang + '\r\nexample();\r\n```'), [
      { type: 'code', lang, content: 'example();' },
    ])
  }
  assert.deepEqual(splitBlocks('before\n```c++\nunfinished'), [
    { type: 'text', content: 'before' }, { type: 'code', lang: 'c++', content: 'unfinished' },
  ])
})

test('table delimiters accept alignment and reject malformed cells', () => {
  for (const value of ['| --- | :---: |', ':--- | ---:', '---']) assert.equal(isTableDelimiter(value), true)
  for (const value of ['', '|', '||', '| --- ||', '   x', ' '.repeat(8000) + 'x']) {
    assert.equal(isTableDelimiter(value), false)
  }
})

test('Python entity spans select complete text after astral characters', () => {
  for (const [text, start, end, expected] of [
    ['😀 José', 2, 6, 'José'], ['😀 Jose\u0301', 2, 7, 'Jose\u0301'],
    ['😀👩🏽 Müller', 4, 10, 'Müller'], ['José', 0, 4, 'José'],
  ]) {
    const input = [{ start, end, label: 'FULLNAME' }]
    const [entity] = utf16Entities(text, input)
    assert.equal(text.slice(entity.start, entity.end), expected)
    assert.deepEqual(input, [{ start, end, label: 'FULLNAME' }])
  }
  assert.deepEqual(utf16Entities('abc', [{ start: -1, end: 2 }, { start: 1, end: 9 }]), [])
})

test('historical numeric and underscore tags remain restorable', () => {
  assert.equal(restorePlaceholders('[123_1] [_TAG_1]', { '[123_1]': 'one', '[_TAG_1]': 'two' }), 'one two')
})

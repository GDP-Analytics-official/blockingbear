import assert from 'node:assert/strict'
import test from 'node:test'

import {
  hasRestorablePlaceholder,
  restorePlaceholders,
  splitRestorablePlaceholders,
} from './placeholders.js'

test('restores only placeholders present in the versioned mapping', () => {
  const values = { '[EMAIL_1]': 'mario@example.it', '[EMAIL_10]': 'x@example.it' }
  assert.equal(
    restorePlaceholders('[EMAIL_1] / [EMAIL_10] / [UNKNOWN_1]', values),
    'mario@example.it / x@example.it / [UNKNOWN_1]')
  assert.equal(hasRestorablePlaceholder('[UNKNOWN_1]', values), false)
  assert.equal(hasRestorablePlaceholder('`[EMAIL_1]`', values), true)
})

test('replacement is literal, single-pass and preserves hostile syntax as data', () => {
  const value = '` </code><script>alert(1)</script> | $&\n[ORG_2]'
  const values = { '[FULLNAME_1]': value, '[ORG_2]': 'must not recurse' }
  assert.equal(restorePlaceholders('[FULLNAME_1]', values), value)
  assert.deepEqual(splitRestorablePlaceholders('a [FULLNAME_1] z', values), [
    { text: 'a ' },
    { placeholder: '[FULLNAME_1]', value },
    { text: ' z' },
  ])
})

test('unknown and malformed tokens remain canonical text', () => {
  const values = { '[EMAIL_1]': 'known' }
  assert.deepEqual(splitRestorablePlaceholders('[UNKNOWN_2] [email_1] [EMAIL_x]', values), [
    { text: '[UNKNOWN_2] [email_1] [EMAIL_x]' },
  ])
})

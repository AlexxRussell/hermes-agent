import { describe, expect, it } from 'vitest'

import {
  collapseRepeatedInputArtifacts,
  sanitizeComposerInput,
  stripLeakedBracketedPasteWrappers
} from './composer-input-sanitize'

describe('stripLeakedBracketedPasteWrappers', () => {
  it('strips canonical escape wrappers', () => {
    expect(stripLeakedBracketedPasteWrappers('\x1b[200~hello\x1b[201~')).toBe('hello')
  })

  it('keeps embedded literal bracket forms', () => {
    const text = 'literal[200~tag and literal[201~tag should stay'
    expect(stripLeakedBracketedPasteWrappers(text)).toBe(text)
  })
})

describe('collapseRepeatedInputArtifacts', () => {
  it('removes the desktop corruption tail from #62557', () => {
    const prefix = '需要时随时叫我。'
    const tail = '[e~[[e' + '~[[e'.repeat(20)
    expect(collapseRepeatedInputArtifacts(prefix + tail)).toBe(prefix)
  })

  it('preserves a mid-string marker followed by valid suffix', () => {
    const text = 'notes ~[[e more text here'
    expect(collapseRepeatedInputArtifacts(text)).toBe(text)
  })

  it('preserves trailing punctuation that is not the corruption signature', () => {
    expect(collapseRepeatedInputArtifacts('wait....')).toBe('wait....')
  })

  it('does not strip when fewer than minRepeats markers appear at the tail', () => {
    const text = 'hello~[[e~[[e'
    expect(collapseRepeatedInputArtifacts(text)).toBe(text)
  })
})

describe('sanitizeComposerInput', () => {
  it.each([
    '12:00~12:15',
    '08:00~09:30',
    '12:01~ 12:15',
    '12:01~  12:15',
    'id01~12:00~12:15',
    '12:00~12:15[200~tag',
    '12:01~',
    'Schedule lunch 12:00~12:15.',
    'Meet (12:00~12:01~).'
  ])('preserves time literals in %j', text => {
    expect(sanitizeComposerInput(text)).toBe(text)
  })

  it.each([
    ['00~hello01~', 'hello'],
    ['prefix:00~hello01~ suffix', 'prefix:hello suffix'],
    ['00~12:00~12:15\n12:01~ 12:1501~', '12:00~12:15\n12:01~ 12:15'],
    ['\x1b[200~12:00~12:15\x1b[201~', '12:00~12:15']
  ])('strips paste wrappers without corrupting their content in %j', (text, expected) => {
    expect(sanitizeComposerInput(text)).toBe(expected)
  })

  it('normalizes wrappers and repeated artifact tails together', () => {
    const corrupted = 'hello[' + '~[[e'.repeat(8)
    expect(sanitizeComposerInput(corrupted)).toBe('hello')
  })
})

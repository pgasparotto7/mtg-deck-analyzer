import { describe, expect, it } from 'vitest'
import { parseDecklist } from './parseDecklist'

describe('parseDecklist', () => {
  it('reads quantity and card name', () => {
    expect(parseDecklist('4 Lightning Bolt')).toEqual([
      {
        quantity: 4,
        name: 'Lightning Bolt',
        raw: '4 Lightning Bolt',
        ok: true,
      },
    ])
  })

  it('ignores empty lines, comments, and section headers', () => {
    const input = `
4 Lightning Bolt
1 Sol Ring
// ignore
Sideboard
2 Negate
`

    expect(parseDecklist(input)).toEqual([
      {
        quantity: 4,
        name: 'Lightning Bolt',
        raw: '4 Lightning Bolt',
        ok: true,
      },
      {
        quantity: 1,
        name: 'Sol Ring',
        raw: '1 Sol Ring',
        ok: true,
      },
      {
        quantity: 2,
        name: 'Negate',
        raw: '2 Negate',
        ok: true,
      },
    ])
  })

  it('marks lines that are not quantity plus name as invalid', () => {
    const result = parseDecklist('Lightning Bolt\n0 Sol Ring\n4x Counterspell')

    expect(result).toEqual([
      {
        quantity: 0,
        name: '',
        raw: 'Lightning Bolt',
        ok: false,
      },
      {
        quantity: 0,
        name: '',
        raw: '0 Sol Ring',
        ok: false,
      },
      {
        quantity: 0,
        name: '',
        raw: '4x Counterspell',
        ok: false,
      },
    ])
  })

  it('keeps valid and invalid lines in order', () => {
    const result = parseDecklist('4 Lightning Bolt\n???\n1 Sol Ring')

    expect(result.map((line) => ({ name: line.name, ok: line.ok }))).toEqual([
      { name: 'Lightning Bolt', ok: true },
      { name: '', ok: false },
      { name: 'Sol Ring', ok: true },
    ])
  })
})

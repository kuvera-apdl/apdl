import { describe, expect, test } from 'vitest'

import { toCsv } from '../../src/lib/csv'

describe('toCsv', () => {
  test('escapes quotes, commas, and newlines', () => {
    const csv = toCsv(
      ['name', 'value'],
      [
        ['plain', 1],
        ['with, comma', 2],
        ['with "quotes"', 3],
        ['with\nnewline', null],
      ],
    )
    expect(csv.split('\r\n')).toEqual([
      'name,value',
      'plain,1',
      '"with, comma",2',
      '"with ""quotes""",3',
      '"with\nnewline",',
    ])
  })
})

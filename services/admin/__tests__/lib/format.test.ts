import { describe, expect, test } from 'vitest'

import {
  formatPercent,
  formatRelative,
  initials,
  isPastDate,
  parseServerDate,
  variantSummary,
} from '../../src/lib/format'

describe('parseServerDate', () => {
  test('parses isoformat with T separator', () => {
    expect(parseServerDate('2026-06-10T12:00:00+00:00')?.toISOString()).toBe(
      '2026-06-10T12:00:00.000Z',
    )
  })

  test('parses str(datetime) with space separator (audit entries)', () => {
    expect(parseServerDate('2026-06-10 12:00:00.123456+00:00')?.getTime()).toBeTruthy()
  })

  test('returns null for empty or garbage input', () => {
    expect(parseServerDate(null)).toBeNull()
    expect(parseServerDate('')).toBeNull()
    expect(parseServerDate('not-a-date')).toBeNull()
  })
})

describe('formatRelative', () => {
  const now = new Date('2026-06-10T12:00:00Z')

  test('formats past and future', () => {
    expect(formatRelative('2026-06-08T12:00:00Z', now)).toBe('2d ago')
    expect(formatRelative('2026-06-10T11:58:00Z', now)).toBe('2m ago')
    expect(formatRelative('2026-06-15T12:00:00Z', now)).toBe('in 5d')
    expect(formatRelative('2026-06-10T12:00:05Z', now)).toBe('just now')
    expect(formatRelative(null, now)).toBe('—')
  })
})

describe('isPastDate', () => {
  const today = new Date('2026-06-10T12:00:00Z')

  test('plain YYYY-MM-DD comparison', () => {
    expect(isPastDate('2026-06-09', today)).toBe(true)
    expect(isPastDate('2026-06-10', today)).toBe(false)
    expect(isPastDate('2027-01-01', today)).toBe(false)
    expect(isPastDate(null, today)).toBe(false)
  })
})

describe('variantSummary', () => {
  test('renders keys with the normalized split', () => {
    expect(
      variantSummary([
        { key: 'control', weight: 1 },
        { key: 'treatment', weight: 1 },
      ]),
    ).toBe('control/treatment 50:50')
    expect(
      variantSummary([
        { key: 'a', weight: 1 },
        { key: 'b', weight: 3 },
      ]),
    ).toBe('a/b 25:75')
    expect(variantSummary([])).toBe('—')
  })
})

describe('formatPercent', () => {
  test('trims trailing zeros, keeps one decimal', () => {
    expect(formatPercent(50)).toBe('50%')
    expect(formatPercent(12.5)).toBe('12.5%')
    expect(formatPercent(33.333)).toBe('33.3%')
  })
})

describe('initials', () => {
  test('derives from names and emails', () => {
    expect(initials('Kirill Sukhikh')).toBe('KS')
    expect(initials('kirill.sukhikh@example.com')).toBe('KS')
    expect(initials('admin')).toBe('A')
  })
})

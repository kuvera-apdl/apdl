import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import {
  emptySelector,
  filterToWire,
  lastDays,
  selectorProblem,
  selectorToWire,
  todayUtcIso,
  utcDateRangeForLastHours,
} from '../../src/features/analytics/selectorModel'

describe('filterToWire', () => {
  test('existence operators omit the value key on the wire', () => {
    const wire = filterToWire({ property: 'plan', operator: 'exists', value: '', values: [] })
    expect(JSON.stringify(wire)).toBe('{"property":"plan","operator":"exists"}')
  })

  test('numeric operators coerce to numbers, lists use chips', () => {
    expect(filterToWire({ property: 'age', operator: 'gte', value: '18', values: [] })).toEqual({
      property: 'age',
      operator: 'gte',
      value: 18,
    })
    expect(
      filterToWire({ property: 'country', operator: 'in', value: '', values: ['US', 'CA'] }),
    ).toEqual({ property: 'country', operator: 'in', value: ['US', 'CA'] })
  })
})

describe('selectorProblem', () => {
  test('flags missing event names and invalid filters', () => {
    expect(selectorProblem(emptySelector('$pageview'))).toBeNull()
    expect(selectorProblem(emptySelector(''))).toContain('event_name')
    expect(
      selectorProblem({
        event_name: '$click',
        filters: [{ property: 'age', operator: 'gt', value: 'abc', values: [] }],
      }),
    ).toContain('numeric')
  })
})

describe('selectorToWire', () => {
  test('trims names and converts all filters', () => {
    expect(
      selectorToWire({
        event_name: ' $click ',
        filters: [{ property: 'href', operator: 'eq', value: '/pricing', values: [] }],
      }),
    ).toEqual({
      event_name: '$click',
      filters: [{ property: 'href', operator: 'eq', value: '/pricing' }],
    })
  })
})

describe('lastDays', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  test('uses the UTC date west of UTC after local midnight divergence', () => {
    vi.setSystemTime(new Date('2026-07-30T03:00:00Z'))
    expect(lastDays(7)).toEqual({
      start_date: '2026-07-24',
      end_date: '2026-07-30',
    })
  })

  test('uses the UTC date east of UTC without including a future day', () => {
    vi.setSystemTime(new Date('2026-07-29T10:00:00Z'))
    expect(lastDays(1)).toEqual({
      start_date: '2026-07-29',
      end_date: '2026-07-29',
    })
  })

  test.each([
    ['leap day', '2024-03-01T00:30:00Z', 2, '2024-02-29', '2024-03-01'],
    ['year boundary', '2026-01-01T12:00:00Z', 2, '2025-12-31', '2026-01-01'],
    ['daylight-saving boundary', '2026-03-09T03:00:00Z', 3, '2026-03-07', '2026-03-09'],
  ])('keeps inclusive UTC arithmetic across a %s', (_label, now, days, start, end) => {
    vi.setSystemTime(new Date(now))
    expect(lastDays(days)).toEqual({ start_date: start, end_date: end })
  })

  test('rejects non-positive or fractional day counts', () => {
    expect(() => lastDays(0)).toThrow('days must be a positive integer')
    expect(() => lastDays(1.5)).toThrow('days must be a positive integer')
  })
})

describe('UTC range helpers', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  test('todayUtcIso returns the current UTC date', () => {
    vi.setSystemTime(new Date('2026-06-22T23:30:00Z'))
    expect(todayUtcIso()).toBe('2026-06-22')
  })

  test('utcDateRangeForLastHours spans the UTC dates the window touches', () => {
    // 00:30 UTC: the current hour is 00:00, so a 24h window reaches back into the
    // previous UTC date.
    vi.setSystemTime(new Date('2026-06-22T00:30:00Z'))
    expect(utcDateRangeForLastHours(24)).toEqual({
      start_date: '2026-06-21',
      end_date: '2026-06-22',
    })
  })

  test('utcDateRangeForLastHours stays on one date late in the UTC day', () => {
    // 23:30 UTC: current hour 23:00, window back to 00:00 same date.
    vi.setSystemTime(new Date('2026-06-22T23:30:00Z'))
    expect(utcDateRangeForLastHours(24)).toEqual({
      start_date: '2026-06-22',
      end_date: '2026-06-22',
    })
  })
})

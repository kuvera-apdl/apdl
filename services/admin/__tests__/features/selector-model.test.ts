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
  test('returns an inclusive range ending today', () => {
    const range = lastDays(7)
    expect(range.start_date <= range.end_date).toBe(true)
    expect(range.start_date).toMatch(/^\d{4}-\d{2}-\d{2}$/)
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

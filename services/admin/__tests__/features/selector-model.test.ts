import { describe, expect, test } from 'vitest'

import {
  emptySelector,
  filterToWire,
  lastDays,
  selectorProblem,
  selectorToWire,
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

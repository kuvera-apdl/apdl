// Query-service mirrors: the 11-operator filter vocabulary (distinct from
// flag conditions), request invariants, and exact response row shapes.
import { describe, expect, test } from 'vitest'

import {
  breakdownResponseSchema,
  cohortResponseSchema,
  eventCountRequestSchema,
  eventCountResponseSchema,
  eventPropertyFilterSchema,
  funnelRequestSchema,
  funnelResponseSchema,
  retentionResponseSchema,
  timeseriesResponseSchema,
} from '../../src/api/schemas/query'

describe('eventPropertyFilterSchema', () => {
  test('existence operators must omit value', () => {
    expect(
      eventPropertyFilterSchema.safeParse({ property: 'plan', operator: 'exists' }).success,
    ).toBe(true)
    expect(
      eventPropertyFilterSchema.safeParse({ property: 'plan', operator: 'exists', value: null })
        .success,
    ).toBe(false)
  })

  test('in requires a non-empty scalar list', () => {
    expect(
      eventPropertyFilterSchema.safeParse({ property: 'plan', operator: 'in', value: ['a', 'b'] })
        .success,
    ).toBe(true)
    expect(
      eventPropertyFilterSchema.safeParse({ property: 'plan', operator: 'in', value: [] }).success,
    ).toBe(false)
  })

  test('numeric comparisons require numbers', () => {
    expect(
      eventPropertyFilterSchema.safeParse({ property: 'age', operator: 'gte', value: 18 }).success,
    ).toBe(true)
    expect(
      eventPropertyFilterSchema.safeParse({ property: 'age', operator: 'gte', value: '18' })
        .success,
    ).toBe(false)
  })

  test('property names follow the server pattern', () => {
    expect(
      eventPropertyFilterSchema.safeParse({ property: 'page.path', operator: 'eq', value: '/' })
        .success,
    ).toBe(true)
    expect(
      eventPropertyFilterSchema.safeParse({ property: 'bad name', operator: 'eq', value: 'x' })
        .success,
    ).toBe(false)
  })
})

describe('request schemas', () => {
  test('count requires 1–20 selectors and ordered dates', () => {
    const base = {
      project_id: 'demo',
      start_date: '2026-06-01',
      end_date: '2026-06-10',
      selectors: [{ event_name: '$pageview', filters: [] }],
    }
    expect(eventCountRequestSchema.safeParse(base).success).toBe(true)
    expect(
      eventCountRequestSchema.safeParse({ ...base, start_date: '2026-06-11' }).success,
    ).toBe(false)
    expect(eventCountRequestSchema.safeParse({ ...base, selectors: [] }).success).toBe(false)
  })

  test('funnel requires 2–20 steps and window 1–90', () => {
    const step = { event_name: '$pageview', filters: [] }
    const base = {
      project_id: 'demo',
      start_date: '2026-06-01',
      end_date: '2026-06-10',
      steps: [step, step],
      window_days: 7,
    }
    expect(funnelRequestSchema.safeParse(base).success).toBe(true)
    expect(funnelRequestSchema.safeParse({ ...base, steps: [step] }).success).toBe(false)
    expect(funnelRequestSchema.safeParse({ ...base, window_days: 91 }).success).toBe(false)
  })
})

describe('response schemas (exact SQL alias mirrors)', () => {
  test('count / timeseries / breakdown rows', () => {
    expect(
      eventCountResponseSchema.safeParse({
        results: [{ selector: '$pageview', event_name: '$pageview', event_count: 10, unique_users: 4 }],
        total_events: 10,
        total_users: 4,
      }).success,
    ).toBe(true)
    expect(
      timeseriesResponseSchema.safeParse({
        selector: '$pageview',
        buckets: [
          { selector: '$pageview', bucket: '2026-06-10T00:00:00', event_count: 5, unique_users: 3 },
        ],
      }).success,
    ).toBe(true)
    expect(
      breakdownResponseSchema.safeParse({
        selector: '$click',
        property: 'href',
        results: [{ selector: '$click', property_value: '/pricing', event_count: 7, unique_users: 6 }],
      }).success,
    ).toBe(true)
    // Aliased/wrong row keys must fail (Strict Schema Rule).
    expect(
      breakdownResponseSchema.safeParse({
        selector: '$click',
        property: 'href',
        results: [{ selector: '$click', value: '/pricing', event_count: 7, unique_users: 6 }],
      }).success,
    ).toBe(false)
  })

  test('funnel / retention / cohort envelopes', () => {
    expect(
      funnelResponseSchema.safeParse({
        steps: [
          {
            step: 1,
            event_name: '$pageview',
            selector: '$pageview',
            count: 100,
            conversion_rate: 100,
            overall_rate: 100,
          },
        ],
        overall_conversion: 42.5,
      }).success,
    ).toBe(true)
    expect(
      retentionResponseSchema.safeParse({
        cohort_selector: '$pageview',
        return_selector: '$click',
        cohorts: [{ cohort_date: '2026-06-01', size: 50, retention: [100, 40.5, 22] }],
      }).success,
    ).toBe(true)
    expect(
      cohortResponseSchema.safeParse({
        metric_selector: '$pageview',
        cohort_property: 'plan',
        cohorts: [
          {
            cohort_value: 'pro',
            total_events: 12,
            total_users: 5,
            timeseries: [{ day: '2026-06-01', event_count: 12, unique_users: 5 }],
          },
        ],
      }).success,
    ).toBe(true)
  })
})

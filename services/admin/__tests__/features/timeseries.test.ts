import { describe, expect, test } from 'vitest'

import { densifyBuckets, type TimeBucket } from '../../src/features/analytics/timeseries'

describe('densifyBuckets — hourly (today)', () => {
  test('fills missing hours with zeroed buckets', () => {
    const input: TimeBucket[] = [
      { bucket: '2026-06-21T20:00:00', event_count: 1, unique_users: 1 },
      { bucket: '2026-06-21T23:00:00', event_count: 4, unique_users: 2 },
    ]
    const out = densifyBuckets(input, '2026-06-21', '2026-06-21', 'hour')

    expect(out).toHaveLength(24)
    expect(out[0]).toEqual({ bucket: '2026-06-21T00:00:00', event_count: 0, unique_users: 0 })
    expect(out[20]).toEqual({ bucket: '2026-06-21T20:00:00', event_count: 1, unique_users: 1 })
    expect(out[22]).toEqual({ bucket: '2026-06-21T22:00:00', event_count: 0, unique_users: 0 })
    expect(out[23]).toEqual({ bucket: '2026-06-21T23:00:00', event_count: 4, unique_users: 2 })
  })

  test('today spans exactly 24 hourly candles', () => {
    const out = densifyBuckets([], '2026-06-22', '2026-06-22', 'hour')
    expect(out).toHaveLength(24)
    expect(out[0].bucket).toBe('2026-06-22T00:00:00')
    expect(out[23].bucket).toBe('2026-06-22T23:00:00')
  })
})

describe('densifyBuckets — daily (week / month)', () => {
  test('this week spans 7 daily candles with date-only keys, gaps zeroed', () => {
    const input: TimeBucket[] = [
      { bucket: '2026-06-16T00:00:00', event_count: 4, unique_users: 1 },
      { bucket: '2026-06-18T00:00:00', event_count: 33, unique_users: 1 },
    ]
    const out = densifyBuckets(input, '2026-06-16', '2026-06-22', 'day')

    expect(out.map((bucket) => bucket.bucket)).toEqual([
      '2026-06-16',
      '2026-06-17',
      '2026-06-18',
      '2026-06-19',
      '2026-06-20',
      '2026-06-21',
      '2026-06-22',
    ])
    expect(out[0]).toEqual({ bucket: '2026-06-16', event_count: 4, unique_users: 1 })
    expect(out[1]).toEqual({ bucket: '2026-06-17', event_count: 0, unique_users: 0 })
    expect(out[2]).toEqual({ bucket: '2026-06-18', event_count: 33, unique_users: 1 })
  })

  test('this month spans 30 daily candles', () => {
    const out = densifyBuckets([], '2026-05-24', '2026-06-22', 'day')
    expect(out).toHaveLength(30)
    expect(out[0].bucket).toBe('2026-05-24')
    expect(out[29].bucket).toBe('2026-06-22')
  })

  test('keeps a real bucket that falls outside the requested range', () => {
    const out = densifyBuckets(
      [{ bucket: '2026-07-01T00:00:00', event_count: 9, unique_users: 3 }],
      '2026-06-21',
      '2026-06-21',
      'day',
    )
    expect(out).toHaveLength(2)
    expect(out.at(-1)).toEqual({ bucket: '2026-07-01', event_count: 9, unique_users: 3 })
  })
})

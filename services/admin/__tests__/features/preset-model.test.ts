// presetModel: draft ↔ wire conversion and per-tool validation for the
// wizard's Preset queries step.
import { describe, expect, test } from 'vitest'

import {
  draftFromWire,
  emptyPresetDraft,
  normalizePresetDraft,
  presetProblems,
  presetToWire,
} from '../../src/features/agents/custom/presetModel'

describe('presetToWire', () => {
  test('funnel drafts produce steps + window_days', () => {
    const draft = emptyPresetDraft('query_funnel')
    draft.selectors[0]!.event_name = 'signup'
    draft.selectors[0]!.filters = [
      { property: 'plan', operator: 'eq', value: 'pro', values: [] },
    ]
    draft.selectors[1]!.event_name = 'purchase'
    expect(presetToWire(draft)).toEqual({
      tool: 'query_funnel',
      params: {
        steps: [
          {
            event_name: 'signup',
            filters: [{ property: 'plan', operator: 'eq', value: 'pro' }],
          },
          { event_name: 'purchase', filters: [] },
        ],
        window_days: 7,
      },
    })
  })

  test('no-parameter tools send empty params; ui_configs omits empty component', () => {
    expect(presetToWire(emptyPresetDraft('list_flags'))).toEqual({
      tool: 'list_flags',
      params: {},
    })
    expect(presetToWire(emptyPresetDraft('list_ui_configs'))).toEqual({
      tool: 'list_ui_configs',
      params: {},
    })
    const withComponent = { ...emptyPresetDraft('list_ui_configs'), property: 'hero' }
    expect(presetToWire(withComponent)).toEqual({
      tool: 'list_ui_configs',
      params: { component: 'hero' },
    })
  })

  test('exists filters omit value; in filters send the chip list (wire value rules)', () => {
    const draft = emptyPresetDraft('query_timeseries')
    draft.selector = {
      event_name: 'page',
      filters: [
        { property: 'beta', operator: 'exists', value: '', values: [] },
        { property: 'plan', operator: 'in', value: '', values: ['pro', 'team'] },
        { property: 'count', operator: 'gt', value: '3', values: [] },
      ],
    }
    expect(presetToWire(draft).params).toEqual({
      selector: {
        event_name: 'page',
        filters: [
          { property: 'beta', operator: 'exists' },
          { property: 'plan', operator: 'in', value: ['pro', 'team'] },
          { property: 'count', operator: 'gt', value: 3 },
        ],
      },
      interval: '1 DAY',
    })
  })
})

describe('draftFromWire (edit-mode prefill)', () => {
  test('round-trips every parameterized tool', () => {
    const wires = [
      { tool: 'discover_events', params: { limit: 50 } },
      {
        tool: 'query_events',
        params: { selectors: [{ event_name: 'page', filters: [] }] },
      },
      {
        tool: 'query_timeseries',
        params: { selector: { event_name: 'page', filters: [] }, interval: '1 WEEK' },
      },
      {
        tool: 'query_funnel',
        params: {
          steps: [
            {
              event_name: 'signup',
              filters: [
                { property: 'plan', operator: 'in', value: ['pro'] },
                { property: 'beta', operator: 'not_exists' },
              ],
            },
            { event_name: 'purchase', filters: [] },
          ],
          window_days: 30,
        },
      },
      {
        tool: 'query_retention',
        params: {
          cohort_selector: { event_name: 'signup', filters: [] },
          return_selector: { event_name: 'page', filters: [] },
          period: 'week',
        },
      },
      {
        tool: 'query_cohort',
        params: { cohort_property: 'plan', metric_selector: { event_name: 'page', filters: [] } },
      },
      {
        tool: 'query_breakdown',
        params: { selector: { event_name: 'page', filters: [] }, property_name: 'utm', limit: 10 },
      },
      { tool: 'list_ui_configs', params: { component: 'hero' } },
    ]
    for (const wire of wires) {
      expect(presetToWire(draftFromWire(wire)), wire.tool).toEqual(wire)
    }
  })

  test('degrades unknown params to defaults instead of crashing', () => {
    const draft = draftFromWire({ tool: 'query_funnel', params: { steps: 'garbage' } })
    // Hand-edited rows degrade to an empty two-step funnel form.
    expect(draft.selectors).toHaveLength(2)
    expect(presetProblems(draft, 'P1')).toContain('P1: step 1 — pick an event.')
  })
})

describe('presetProblems', () => {
  test('flags missing events, bad ranges, and missing properties', () => {
    const funnel = emptyPresetDraft('query_funnel')
    funnel.windowDays = 0
    const problems = presetProblems(funnel, 'P1')
    expect(problems).toContain('P1: step 1 — pick an event.')
    expect(problems).toContain('P1: step 2 — pick an event.')
    expect(problems).toContain('P1: conversion window must be 1-90 days.')

    const cohort = emptyPresetDraft('query_cohort')
    cohort.selector.event_name = 'page'
    expect(presetProblems(cohort, 'P2')).toEqual(['P2: cohort property is required.'])

    expect(presetProblems(emptyPresetDraft('list_flags'), 'P3')).toEqual([])
  })

  test('normalizePresetDraft pads funnels up to two steps', () => {
    const draft = { ...emptyPresetDraft('query_events'), tool: 'query_funnel' }
    expect(normalizePresetDraft(draft).selectors).toHaveLength(2)
  })
})

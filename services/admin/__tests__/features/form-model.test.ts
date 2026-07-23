// Wire-format guarantees of the editor's form model — the two server
// contract subtleties (value-key omission for existence operators; changed
// -fields-only updates with derived enabled) live here.
import { describe, expect, test } from 'vitest'

import { flagCreateSchema, flagUpdateSchema } from '../../src/api/schemas/flags'
import {
  emptyFormValues,
  flagFormSchema,
  flagToFormValues,
  formToCreatePayload,
  formToEvaluable,
  formToUpdatePlan,
  type FlagFormValues,
} from '../../src/features/flags/editor/formModel'
import { makeFlag } from '../helpers/fixtures'

function baseValues(): FlagFormValues {
  return {
    ...emptyFormValues(),
    key: 'new-flag',
    name: 'New flag',
  }
}

describe('formToCreatePayload', () => {
  test('derives enabled from state and omits empty review_by', () => {
    const payload = formToCreatePayload(baseValues())
    expect(payload.enabled).toBe(false)
    expect(payload.state).toBe('draft')
    expect(payload.auto_disable).toBe(false)
    expect('review_by' in payload).toBe(false)
    expect(flagCreateSchema.safeParse(payload).success).toBe(true)
  })

  test('keeps automatic disabling unavailable', () => {
    expect(emptyFormValues().auto_disable).toBe(false)
    expect(flagFormSchema.safeParse({ ...baseValues(), auto_disable: true }).success).toBe(false)
  })

  test('rejects guardrail windows longer than 90 days', () => {
    const values = baseValues()
    values.guardrails = [
      {
        metric: 'frontend_error_rate',
        threshold: '2x_baseline',
        scope: '',
        minimum_exposures: 0,
        window_minutes: 129_601,
      },
    ]
    expect(flagFormSchema.safeParse(values).success).toBe(false)
    values.guardrails[0]!.window_minutes = 129_600
    expect(flagFormSchema.safeParse(values).success).toBe(true)
  })

  test('active state derives enabled=true', () => {
    const payload = formToCreatePayload({ ...baseValues(), state: 'active' })
    expect(payload.enabled).toBe(true)
  })

  test('existence conditions omit the value key entirely on the wire', () => {
    const values = baseValues()
    values.rules = [
      {
        id: 'rule_x',
        name: '',
        conditions: [
          { attribute: 'beta', operator: 'exists', value: '', values: [] },
          { attribute: 'plan', operator: 'equals', value: 'pro', values: [] },
          { attribute: 'age', operator: 'gte', value: '18', values: [] },
          { attribute: 'country', operator: 'in', value: '', values: ['US', 'CA'] },
        ],
        rollout: { percentage: 50, bucket_by: 'user_id' },
      },
    ]
    const payload = formToCreatePayload(values)
    const conditions = payload.rules[0]!.conditions
    // Serialized JSON must not contain a value key for `exists` — the server
    // rejects even an explicit null there.
    expect(JSON.stringify(conditions[0])).toBe('{"attribute":"beta","operator":"exists"}')
    expect(conditions[1]).toEqual({ attribute: 'plan', operator: 'equals', value: 'pro' })
    expect(conditions[2]).toEqual({ attribute: 'age', operator: 'gte', value: 18 })
    expect(conditions[3]).toEqual({ attribute: 'country', operator: 'in', value: ['US', 'CA'] })
  })
})

describe('formToUpdatePlan', () => {
  test('an untouched form produces no changes (existence conditions included)', () => {
    const flag = makeFlag()
    const plan = formToUpdatePlan(flagToFormValues(flag), flag, flag.version)
    expect(plan.changedFields).toEqual([])
    expect(plan.payload).toEqual({ version: 3 })
  })

  test('sends only config fields and ignores lifecycle state changes', () => {
    const flag = makeFlag()
    const values = flagToFormValues(flag)
    values.name = 'Renamed'
    values.state = 'draft'
    const plan = formToUpdatePlan(values, flag, flag.version)
    expect(plan.changedFields).toEqual(['name'])
    expect(plan.payload).toEqual({ version: 3, name: 'Renamed' })
    expect('state' in plan.payload).toBe(false)
    expect('enabled' in plan.payload).toBe(false)
  })

  test('variant edits send variants and default_variant together', () => {
    const flag = makeFlag()
    const values = flagToFormValues(flag)
    values.variants = [
      { key: 'control', weight: 1 },
      { key: 'treatment', weight: 3 },
    ]
    const plan = formToUpdatePlan(values, flag, flag.version)
    expect(plan.changedFields.sort()).toEqual(['default_variant', 'variants'])
  })

  test('an emptied review_by is sent as the canonical null clear operation', () => {
    const flag = makeFlag()
    const values = flagToFormValues(flag)
    values.review_by = ''
    const plan = formToUpdatePlan(values, flag, flag.version)
    expect(plan.changedFields).toEqual(['review_by'])
    expect(plan.payload).toEqual({ version: 3, review_by: null })
    expect(flagUpdateSchema.safeParse(plan.payload).success).toBe(true)
  })

  test('review dates reject impossible calendar values', () => {
    expect(flagUpdateSchema.safeParse({ version: 3, review_by: '2027-02-29' }).success).toBe(false)
    expect(flagUpdateSchema.safeParse({ version: 3, review_by: '2028-02-29' }).success).toBe(true)
  })
})

describe('formToEvaluable', () => {
  test('treats the config as active with a preview salt for creates', () => {
    const evaluable = formToEvaluable(baseValues())
    expect(evaluable.enabled).toBe(true)
    expect(evaluable.salt).toBe('preview-salt')
    expect(evaluable.key).toBe('new-flag')
  })
})

// Wire-format guarantees of the editor's form model — the two server
// contract subtleties (value-key omission for existence operators; changed
// -fields-only updates with derived enabled) live here.
import { describe, expect, test } from 'vitest'

import { flagCreateSchema } from '../../src/api/schemas/flags'
import {
  emptyFormValues,
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
    expect('review_by' in payload).toBe(false)
    expect(flagCreateSchema.safeParse(payload).success).toBe(true)
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
    const flag = makeFlag() // has an `exists` condition serialized as value:null
    const plan = formToUpdatePlan(flagToFormValues(flag), flag, flag.version)
    expect(plan.changedFields).toEqual([])
    expect(plan.payload).toEqual({ version: 3 })
  })

  test('sends only the changed fields, never enabled', () => {
    const flag = makeFlag()
    const values = flagToFormValues(flag)
    values.name = 'Renamed'
    values.state = 'draft'
    const plan = formToUpdatePlan(values, flag, flag.version)
    expect(plan.changedFields.sort()).toEqual(['name', 'state'])
    expect(plan.payload).toEqual({ version: 3, name: 'Renamed', state: 'draft' })
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

  test('an emptied review_by is left unchanged (API cannot clear it)', () => {
    const flag = makeFlag()
    const values = flagToFormValues(flag)
    values.review_by = ''
    const plan = formToUpdatePlan(values, flag, flag.version)
    expect(plan.changedFields).toEqual([])
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

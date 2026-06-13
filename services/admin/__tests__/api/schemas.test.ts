// Schema parity (plan §10.2): the zod mirrors must accept canonical payloads
// and reject aliased or extra fields — the Strict Schema Rule as a test.
import { describe, expect, test } from 'vitest'

import {
  flagAuditEntrySchema,
  flagConfigSchema,
  flagsListResponseSchema,
  flagUpdatePayloadSchema,
  guardrailConfigSchema,
  staleFlagSchema,
} from '../../src/api/schemas/flags'
import { makeAuditEntry, makeFlag } from '../helpers/fixtures'

describe('flagConfigSchema', () => {
  test('accepts the canonical serialized flag', () => {
    expect(flagConfigSchema.safeParse(makeFlag()).success).toBe(true)
  })

  test('accepts value: null on existence conditions (serialized form)', () => {
    const flag = makeFlag()
    expect(flag.rules[0]?.conditions[1]).toEqual({
      attribute: 'beta_opt_in',
      operator: 'exists',
      value: null,
    })
    expect(flagConfigSchema.safeParse(flag).success).toBe(true)
  })

  test('rejects aliased field names (default_value instead of default_variant)', () => {
    const { default_variant, ...rest } = makeFlag()
    const aliased = { ...rest, default_value: default_variant }
    expect(flagConfigSchema.safeParse(aliased).success).toBe(false)
  })

  test('rejects unknown extra fields (extra="forbid" mirror)', () => {
    expect(flagConfigSchema.safeParse({ ...makeFlag(), legacy_enabled: true }).success).toBe(false)
  })

  test('rejects a default_variant outside the variant keys', () => {
    expect(flagConfigSchema.safeParse(makeFlag({ default_variant: 'missing' })).success).toBe(false)
  })

  test('rejects zero-sum variant weights', () => {
    const flag = makeFlag({
      variants: [
        { key: 'control', weight: 0 },
        { key: 'treatment', weight: 0 },
      ],
    })
    expect(flagConfigSchema.safeParse(flag).success).toBe(false)
  })

  test('rejects equality conditions without a value', () => {
    const flag = makeFlag()
    const rule = flag.rules[0]!
    rule.conditions = [{ attribute: 'plan', operator: 'equals', value: null }]
    expect(flagConfigSchema.safeParse(flag).success).toBe(false)
  })
})

describe('guardrailConfigSchema', () => {
  test('enforces the metric↔threshold pairing', () => {
    expect(
      guardrailConfigSchema.safeParse({
        metric: 'frontend_error_rate',
        threshold: 'at_least_one',
        scope: '',
        minimum_exposures: 0,
        window_minutes: 10,
      }).success,
    ).toBe(false)
  })
})

describe('response envelopes', () => {
  test('flags list parses', () => {
    const payload = { flags: [makeFlag()], count: 1 }
    expect(flagsListResponseSchema.safeParse(payload).success).toBe(true)
  })

  test('stale flag carries report fields', () => {
    const stale = {
      ...makeFlag(),
      stale_reasons: ['missing_owner', 'fully_rolled_out'],
      cleanup_recommended: true,
      days_since_update: 120,
    }
    expect(staleFlagSchema.safeParse(stale).success).toBe(true)
  })

  test('audit entry parses, including the str(datetime) timestamp form', () => {
    expect(flagAuditEntrySchema.safeParse(makeAuditEntry()).success).toBe(true)
    expect(
      flagAuditEntrySchema.safeParse(
        makeAuditEntry({ action: 'flag_created', previous_version: null, before: null }),
      ).success,
    ).toBe(true)
  })

  test('audit entry rejects unknown actions', () => {
    expect(
      flagAuditEntrySchema.safeParse(makeAuditEntry({ action: 'flag_renamed' as never })).success,
    ).toBe(false)
  })
})

describe('flagUpdatePayloadSchema (SSE)', () => {
  test('parses both broadcast shapes', () => {
    const clientFlag = {
      key: 'checkout-cta',
      enabled: true,
      default_variant: 'control',
      variants: [
        { key: 'control', weight: 1 },
        { key: 'treatment', weight: 1 },
      ],
      salt: 's',
      rules: [],
      fallthrough: { rollout: { percentage: 10, bucket_by: 'user_id' } },
      version: 3,
    }
    expect(
      flagUpdatePayloadSchema.safeParse({ action: 'flag_updated', flag: clientFlag }).success,
    ).toBe(true)
    expect(
      flagUpdatePayloadSchema.safeParse({ action: 'flag_removed', key: 'checkout-cta' }).success,
    ).toBe(true)
    expect(flagUpdatePayloadSchema.safeParse({ action: 'flag_updated' }).success).toBe(false)
  })
})

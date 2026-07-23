// Schema parity (plan §10.2): the zod mirrors must accept canonical payloads
// and reject aliased or extra fields — the Strict Schema Rule as a test.
import { describe, expect, test } from 'vitest'

import {
  experimentUpdatePayloadSchema,
  flagAuditEntrySchema,
  flagCleanupSchema,
  flagConfigSchema,
  flagCreateSchema,
  flagDisableSchema,
  flagsListResponseSchema,
  flagTransitionSchema,
  flagUpdateSchema,
  flagUpdatePayloadSchema,
  gateEvaluateRequestSchema,
  guardrailConfigSchema,
  staleFlagSchema,
} from '../../src/api/schemas/flags'
import { makeAuditEntry, makeFlag } from '../helpers/fixtures'

describe('flagConfigSchema', () => {
  test('accepts the canonical serialized flag', () => {
    expect(flagConfigSchema.safeParse(makeFlag()).success).toBe(true)
  })

  test('requires existence conditions to omit value', () => {
    const flag = makeFlag()
    expect(flag.rules[0]?.conditions[1]).toEqual({
      attribute: 'beta_opt_in',
      operator: 'exists',
    })
    expect(flagConfigSchema.safeParse(flag).success).toBe(true)

    flag.rules[0]!.conditions[1] = {
      attribute: 'beta_opt_in',
      operator: 'exists',
      value: null,
    }
    expect(flagConfigSchema.safeParse(flag).success).toBe(false)
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

  test('rejects the unsupported automatic-disable capability', () => {
    expect(flagConfigSchema.safeParse({ ...makeFlag(), auto_disable: true }).success).toBe(false)
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

  test('caps the query window at 90 days', () => {
    const guardrail = {
      metric: 'frontend_error_rate',
      threshold: '2x_baseline',
      scope: '',
      minimum_exposures: 0,
      window_minutes: 129_600,
    }
    expect(guardrailConfigSchema.safeParse(guardrail).success).toBe(true)
    expect(
      guardrailConfigSchema.safeParse({ ...guardrail, window_minutes: 129_601 }).success,
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

  test('audit entry requires the canonical mutation origin', () => {
    const { origin: _origin, ...withoutOrigin } = makeAuditEntry()
    expect(flagAuditEntrySchema.safeParse(withoutOrigin).success).toBe(false)
    expect(flagAuditEntrySchema.safeParse(makeAuditEntry({ origin: 'experiment' })).success).toBe(true)
  })
})

describe('versioned flag mutation contracts', () => {
  test('generic updates reject lifecycle state and enabled fields', () => {
    expect(flagUpdateSchema.safeParse({ version: 3, name: 'Renamed' }).success).toBe(true)
    expect(flagUpdateSchema.safeParse({ version: 3, state: 'active' }).success).toBe(false)
    expect(flagUpdateSchema.safeParse({ version: 3, enabled: true }).success).toBe(false)
  })

  test('writes only accept the fail-closed automatic-disable value', () => {
    const create = {
      key: 'checkout-cta',
      name: 'Checkout CTA',
      state: 'draft',
      owners: [],
      enabled: false,
      description: '',
      default_variant: 'control',
      variants: [{ key: 'control', weight: 1 }],
      rules: [],
      fallthrough: { rollout: { percentage: 0, bucket_by: 'user_id' } },
      evaluation_mode: 'client',
      guardrails: [],
    }
    expect(flagCreateSchema.safeParse(create).success).toBe(true)
    expect(flagCreateSchema.safeParse({ ...create, auto_disable: false }).success).toBe(true)
    expect(flagCreateSchema.safeParse({ ...create, auto_disable: true }).success).toBe(false)
    expect(flagUpdateSchema.safeParse({ version: 3, auto_disable: false }).success).toBe(true)
    expect(flagUpdateSchema.safeParse({ version: 3, auto_disable: true }).success).toBe(false)
  })

  test('transition, disable, and cleanup require canonical versioned bodies', () => {
    expect(
      flagTransitionSchema.safeParse({ version: 3, target_state: 'active' }).success,
    ).toBe(true)
    expect(
      flagDisableSchema.safeParse({
        version: 3,
        reason: 'guardrail_failed',
        evidence: {},
      }).success,
    ).toBe(true)
    expect(
      flagDisableSchema.safeParse({
        version: 3,
        reason: 'guardrail_failed',
        source: 'admin',
        evidence: {},
      }).success,
    ).toBe(false)
    expect(flagCleanupSchema.safeParse({ version: 3, evidence: {} }).success).toBe(true)
    expect(
      flagCleanupSchema.safeParse({ version: 3, source: 'admin', evidence: {} }).success,
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
      flagUpdatePayloadSchema.safeParse({ action: 'flag_updated', flag: clientFlag, version: 3 }).success,
    ).toBe(true)
    expect(
      flagUpdatePayloadSchema.safeParse({ action: 'flag_removed', key: 'checkout-cta', version: 3 }).success,
    ).toBe(true)
    expect(flagUpdatePayloadSchema.safeParse({ action: 'flag_updated' }).success).toBe(false)
  })

  test('requires aggregate metadata on experiment broadcasts', () => {
    expect(
      experimentUpdatePayloadSchema.safeParse({
        action: 'experiment_updated',
        key: 'checkout-test',
        status: 'scheduled',
        flag_key: 'checkout-flag',
        version: 4,
      }).success,
    ).toBe(true)
    expect(
      experimentUpdatePayloadSchema.safeParse({
        action: 'experiment_deleted',
        key: 'checkout-test',
        status: null,
        flag_key: 'checkout-flag',
        version: 5,
      }).success,
    ).toBe(true)
    expect(
      experimentUpdatePayloadSchema.safeParse({
        action: 'experiment_updated',
        key: 'checkout-test',
        version: 4,
      }).success,
    ).toBe(false)
  })
})

describe('gateEvaluateRequestSchema', () => {
  const request = {
    project_id: 'demo',
    key: 'checkout-cta',
    context: { user_id: 'user-1', attributes: {} },
    log_exposure: false,
  }

  test('allows non-logging evaluation without an exposure message ID', () => {
    expect(gateEvaluateRequestSchema.safeParse(request).success).toBe(true)
  })

  test('requires one stable nonblank message ID when logging exposure', () => {
    expect(
      gateEvaluateRequestSchema.safeParse({
        ...request,
        log_exposure: true,
        message_id: 'eval-checkout-001',
      }).success,
    ).toBe(true)
    for (const message_id of [undefined, '', '   ', ' padded ']) {
      expect(
        gateEvaluateRequestSchema.safeParse({ ...request, log_exposure: true, message_id }).success,
      ).toBe(false)
    }
  })
})

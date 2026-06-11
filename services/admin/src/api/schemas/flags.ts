// Zod mirrors of services/config/app/models/schemas.py (Strict Schema Rule).
// One canonical name per field — no aliases, no tolerant parsing. Every object
// is .strict() to mirror Pydantic's extra="forbid"; drift fails loudly.
import { z } from 'zod'

export const conditionOperatorSchema = z.enum([
  'equals',
  'not_equals',
  'gt',
  'gte',
  'lt',
  'lte',
  'contains',
  'not_contains',
  'starts_with',
  'ends_with',
  'regex',
  'in',
  'not_in',
  'exists',
  'not_exists',
])

export const guardrailMetricSchema = z.enum(['frontend_error_rate', 'frontend_error_count'])
export const guardrailThresholdSchema = z.enum(['2x_baseline', 'at_least_one'])
export const evaluationModeSchema = z.enum(['client', 'server', 'both'])
export const flagStateSchema = z.enum(['draft', 'active', 'disabled', 'archived'])

const EXISTENCE_OPERATORS = new Set<string>(['exists', 'not_exists'])

// Serialized flags carry `value: null` for existence conditions (Pydantic dumps
// the default), while create payloads omit the key entirely — accept both.
export const gateConditionSchema = z
  .object({
    attribute: z.string().min(1),
    operator: conditionOperatorSchema,
    value: z.unknown().optional(),
  })
  .strict()
  .superRefine((condition, ctx) => {
    const hasValue = condition.value !== undefined && condition.value !== null
    if (EXISTENCE_OPERATORS.has(condition.operator) && hasValue) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['value'],
        message: `${condition.operator} conditions must not include value`,
      })
    }
    if (!EXISTENCE_OPERATORS.has(condition.operator) && !hasValue) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['value'],
        message: `${condition.operator} conditions require value`,
      })
    }
  })

export const rolloutConfigSchema = z
  .object({
    percentage: z.number().min(0).max(100),
    bucket_by: z.string().min(1),
  })
  .strict()

export const variantConfigSchema = z
  .object({
    key: z.string().min(1),
    weight: z.number().int().min(0),
  })
  .strict()

export const gateRuleSchema = z
  .object({
    id: z.string().min(1),
    name: z.string(),
    conditions: z.array(gateConditionSchema),
    rollout: rolloutConfigSchema,
  })
  .strict()

export const fallthroughConfigSchema = z
  .object({
    rollout: rolloutConfigSchema,
  })
  .strict()

export const guardrailConfigSchema = z
  .object({
    metric: guardrailMetricSchema,
    threshold: guardrailThresholdSchema,
    scope: z.string(),
    minimum_exposures: z.number().int().min(0),
    window_minutes: z.number().int().min(1),
  })
  .strict()
  .superRefine((guardrail, ctx) => {
    if (guardrail.metric === 'frontend_error_rate' && guardrail.threshold !== '2x_baseline') {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['threshold'],
        message: "frontend_error_rate guardrails require threshold '2x_baseline'",
      })
    }
    if (guardrail.metric === 'frontend_error_count' && guardrail.threshold !== 'at_least_one') {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['threshold'],
        message: "frontend_error_count guardrails require threshold 'at_least_one'",
      })
    }
  })

interface VariantFlagShape {
  default_variant: string
  variants: { key: string; weight: number }[]
}

function variantInvariants(flag: VariantFlagShape, ctx: z.RefinementCtx): void {
  const keys = new Set<string>()
  let total = 0
  for (const variant of flag.variants) {
    if (keys.has(variant.key)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['variants'],
        message: 'variants must contain unique keys',
      })
      return
    }
    keys.add(variant.key)
    total += variant.weight
  }
  if (flag.variants.length === 0 || total <= 0) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['variants'],
      message: 'variants must contain at least one positive weight',
    })
    return
  }
  if (!keys.has(flag.default_variant)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['default_variant'],
      message: 'default_variant must match a variant key',
    })
  }
}

// Field order mirrors serialize_flag() in services/config/app/utils.py.
const flagConfigShape = z
  .object({
    key: z.string().min(1),
    project_id: z.string(),
    name: z.string(),
    state: flagStateSchema,
    owners: z.array(z.string()),
    review_by: z.string().nullable(),
    description: z.string(),
    enabled: z.boolean(),
    default_variant: z.string().min(1),
    variants: z.array(variantConfigSchema),
    rules: z.array(gateRuleSchema),
    fallthrough: fallthroughConfigSchema,
    salt: z.string(),
    evaluation_mode: evaluationModeSchema,
    auto_disable: z.boolean(),
    guardrails: z.array(guardrailConfigSchema),
    disabled_reason: z.string(),
    disabled_by: z.string(),
    disabled_at: z.string().nullable(),
    version: z.number().int().min(1),
    created_at: z.string(),
    updated_at: z.string(),
    archived_at: z.string().nullable(),
  })
  .strict()

export const flagConfigSchema = flagConfigShape.superRefine(variantInvariants)

export const staleReasonSchema = z.enum([
  'missing_owner',
  'missing_review_date',
  'review_overdue',
  'stale_draft',
  'stale_disabled',
  'fully_rolled_out',
])

export const staleFlagSchema = flagConfigShape
  .extend({
    stale_reasons: z.array(staleReasonSchema),
    cleanup_recommended: z.boolean(),
    days_since_update: z.number().int(),
  })
  .strict()
  .superRefine(variantInvariants)

export const flagAuditActionSchema = z.enum([
  'flag_created',
  'flag_updated',
  'flag_disabled',
  'flag_auto_disabled',
  'flag_archived',
  'flag_cleanup_archived',
])

export const flagAuditEntrySchema = z
  .object({
    id: z.number().int(),
    project_id: z.string(),
    flag_key: z.string(),
    action: flagAuditActionSchema,
    actor: z.string(),
    previous_version: z.number().int().nullable(),
    new_version: z.number().int().nullable(),
    before: z.record(z.unknown()).nullable(),
    after: z.record(z.unknown()).nullable(),
    evidence: z.record(z.unknown()),
    reason: z.string().nullable(),
    created_at: z.string(),
  })
  .strict()

export const flagsListResponseSchema = z
  .object({
    flags: z.array(flagConfigSchema),
    count: z.number().int(),
  })
  .strict()

export const staleFlagsResponseSchema = z
  .object({
    flags: z.array(staleFlagSchema),
    count: z.number().int(),
    older_than_days: z.number().int(),
  })
  .strict()

export const flagAuditResponseSchema = z
  .object({
    flag_key: z.string(),
    audit: z.array(flagAuditEntrySchema),
    count: z.number().int(),
  })
  .strict()

// SDK bootstrap representation (SSE `config` event payload entries).
export const clientFlagConfigSchema = z
  .object({
    key: z.string().min(1),
    enabled: z.boolean(),
    default_variant: z.string().min(1),
    variants: z.array(variantConfigSchema),
    salt: z.string(),
    rules: z.array(gateRuleSchema),
    fallthrough: fallthroughConfigSchema,
    version: z.number().int().min(1),
  })
  .strict()
  .superRefine(variantInvariants)

export const flagCollectionSchema = z
  .object({
    schema_version: z.literal(2),
    project_id: z.string(),
    flags: z.array(clientFlagConfigSchema),
  })
  .strict()

// SSE `flag_update` payloads from _broadcast_flag_change().
export const flagUpdatePayloadSchema = z.union([
  z
    .object({
      action: z.enum(['flag_created', 'flag_updated', 'flag_archived']),
      flag: clientFlagConfigSchema,
    })
    .strict(),
  z
    .object({
      action: z.literal('flag_removed'),
      key: z.string(),
    })
    .strict(),
])

// SSE `experiment_update` payloads (status absent on deletes).
export const experimentUpdatePayloadSchema = z
  .object({
    action: z.enum(['experiment_created', 'experiment_updated', 'experiment_deleted']),
    key: z.string(),
    status: z.string().optional(),
  })
  .strict()

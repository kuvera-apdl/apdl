// Zod mirrors of services/config/app/models/schemas.py (Strict Schema Rule).
// One canonical name per field — no aliases, no tolerant parsing. Every object
// is .strict() to mirror Pydantic's extra="forbid"; drift fails loudly.
import { z } from 'zod'
import {
  MAX_CONDITIONS_PER_RULE,
  MAX_IDENTIFIER_LENGTH,
  MAX_RULES,
  MAX_STRING_LENGTH,
  isConditionValueValid,
  isIdentifier,
} from '@/core/evaluator/targetingContract'

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

export const gateConditionSchema = z
  .object({
    attribute: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    operator: conditionOperatorSchema,
    value: z.unknown().optional(),
  })
  .strict()
  .superRefine((condition, ctx) => {
    const hasValue = Object.prototype.hasOwnProperty.call(condition, 'value')
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
      return
    }
    if (
      !EXISTENCE_OPERATORS.has(condition.operator) &&
      !isConditionValueValid(condition.operator, condition.value)
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['value'],
        message: `invalid value for ${condition.operator} condition`,
      })
    }
  })

export const rolloutConfigSchema = z
  .object({
    percentage: z.number().min(0).max(100),
    bucket_by: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
  })
  .strict()

export const variantConfigSchema = z
  .object({
    key: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    weight: z.number().int().min(0),
  })
  .strict()

export const gateRuleSchema = z
  .object({
    id: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    name: z.string().max(MAX_STRING_LENGTH),
    conditions: z.array(gateConditionSchema).max(MAX_CONDITIONS_PER_RULE),
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
    window_minutes: z.number().int().min(1).max(129_600),
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
    key: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    project_id: z.string(),
    name: z.string().max(MAX_STRING_LENGTH),
    state: flagStateSchema,
    owners: z.array(z.string()),
    review_by: z.string().nullable(),
    description: z.string(),
    enabled: z.boolean(),
    default_variant: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    variants: z.array(variantConfigSchema),
    rules: z.array(gateRuleSchema).max(MAX_RULES),
    fallthrough: fallthroughConfigSchema,
    salt: z.string().max(MAX_STRING_LENGTH),
    evaluation_mode: evaluationModeSchema,
    auto_disable: z.literal(false),
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

export const flagAuditOriginSchema = z.enum([
  'manual',
  'automation',
  'experiment',
  'scheduler',
])

export const flagAuditEntrySchema = z
  .object({
    id: z.number().int(),
    project_id: z.string(),
    flag_key: z.string(),
    action: flagAuditActionSchema,
    actor: z.string(),
    origin: flagAuditOriginSchema,
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
    key: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    enabled: z.boolean(),
    default_variant: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    variants: z.array(variantConfigSchema),
    salt: z.string().max(MAX_STRING_LENGTH),
    rules: z.array(gateRuleSchema).max(MAX_RULES),
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
      version: z.number().int().min(1),
    })
    .strict(),
  z
    .object({
      action: z.literal('flag_removed'),
      key: z.string(),
      version: z.number().int().min(1),
    })
    .strict(),
])

// SSE `experiment_update` payloads always carry the changed aggregate version.
const experimentUpdateStatusSchema = z.enum([
  'draft',
  'scheduled',
  'running',
  'completed',
  'stopped',
])

export const experimentUpdatePayloadSchema = z
  .object({
    action: z.enum(['experiment_created', 'experiment_updated', 'experiment_deleted']),
    key: z.string(),
    status: experimentUpdateStatusSchema.nullable(),
    flag_key: z.string(),
    version: z.number().int().min(1),
  })
  .strict()

// ---------- Admin write payloads (FlagCreate / FlagUpdate / …) ----------

export const writableFlagStateSchema = z.enum(['draft', 'active', 'disabled'])

// DB invariant: (state = 'active') = enabled — never two independent toggles.
function stateEnabledInvariant(
  flag: { state?: string; enabled?: boolean },
  ctx: z.RefinementCtx,
): void {
  if (flag.state === undefined || flag.enabled === undefined) return
  const expected = flag.state === 'active'
  if (flag.enabled !== expected) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['enabled'],
      message: `state '${flag.state}' requires enabled=${expected}`,
    })
  }
}

export const flagCreateSchema = z
  .object({
    key: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    name: z.string().min(1).max(MAX_STRING_LENGTH),
    state: writableFlagStateSchema,
    owners: z.array(z.string().min(1)),
    // Omitted entirely when unset — the server create path runs exclude_none.
    review_by: z.string().optional(),
    enabled: z.boolean(),
    description: z.string(),
    default_variant: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    variants: z.array(variantConfigSchema),
    rules: z.array(gateRuleSchema).max(MAX_RULES),
    fallthrough: fallthroughConfigSchema,
    evaluation_mode: evaluationModeSchema,
    auto_disable: z.literal(false).default(false),
    guardrails: z.array(guardrailConfigSchema),
  })
  .strict()
  .superRefine(variantInvariants)
  .superRefine(stateEnabledInvariant)

export const flagUpdateSchema = z
  .object({
    version: z.number().int().min(1),
    owners: z.array(z.string().min(1)).optional(),
    review_by: z.string().optional(),
    name: z.string().min(1).max(MAX_STRING_LENGTH).optional(),
    description: z.string().optional(),
    default_variant: z.string().min(1).max(MAX_IDENTIFIER_LENGTH).optional(),
    variants: z.array(variantConfigSchema).optional(),
    rules: z.array(gateRuleSchema).max(MAX_RULES).optional(),
    fallthrough: fallthroughConfigSchema.optional(),
    evaluation_mode: evaluationModeSchema.optional(),
    auto_disable: z.literal(false).optional(),
    guardrails: z.array(guardrailConfigSchema).optional(),
  })
  .strict()
  .superRefine((update, ctx) => {
    if (update.variants !== undefined && update.default_variant !== undefined) {
      variantInvariants({ default_variant: update.default_variant, variants: update.variants }, ctx)
    } else if (update.variants !== undefined) {
      const total = update.variants.reduce((sum, variant) => sum + variant.weight, 0)
      if (update.variants.length === 0 || total <= 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['variants'],
          message: 'variants must contain at least one positive weight',
        })
      }
    }
  })

export const flagTransitionSchema = z
  .object({
    version: z.number().int().min(1),
    target_state: z.enum(['draft', 'active']),
  })
  .strict()

export const flagDisableSchema = z
  .object({
    version: z.number().int().min(1),
    reason: z.enum(['guardrail_failed', 'experiment_rollback']),
    evidence: z.record(z.unknown()),
  })
  .strict()

export const flagCleanupSchema = z
  .object({
    version: z.number().int().min(1),
    evidence: z.record(z.unknown()),
  })
  .strict()

// Write response envelopes.
export const flagCreateResponseSchema = z
  .object({ created: z.boolean(), flag: flagConfigSchema })
  .strict()
export const flagUpdateResponseSchema = z
  .object({ updated: z.boolean(), flag: flagConfigSchema })
  .strict()
export const flagTransitionResponseSchema = flagUpdateResponseSchema
export const flagDisableResponseSchema = z
  .object({ disabled: z.boolean(), flag: flagConfigSchema })
  .strict()
export const flagArchiveResponseSchema = z
  .object({ archived: z.boolean(), flag: flagConfigSchema })
  .strict()
export const flagCleanupResponseSchema = z
  .object({ cleaned_up: z.boolean(), cleanup_reasons: z.array(z.string()), flag: flagConfigSchema })
  .strict()

// ---------- Server-side evaluation (POST /v1/evaluate) ----------

const evaluationAttributesSchema = z.record(z.unknown()).superRefine((attributes, ctx) => {
  for (const [key, value] of Object.entries(attributes)) {
    if (!isIdentifier(key)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: [key],
        message: `attribute keys must be 1..${MAX_IDENTIFIER_LENGTH} characters`,
      })
    }
    if (typeof value === 'string' && value.length > MAX_STRING_LENGTH) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: [key],
        message: `string attributes must be at most ${MAX_STRING_LENGTH} characters`,
      })
    }
  }
})

export const evalContextSchema = z
  .object({
    user_id: z.string().max(MAX_IDENTIFIER_LENGTH).nullable().optional(),
    anonymous_id: z.string().max(MAX_IDENTIFIER_LENGTH).nullable().optional(),
    attributes: evaluationAttributesSchema,
  })
  .strict()

export const gateEvaluateRequestSchema = z
  .object({
    project_id: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    key: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    context: evalContextSchema,
    log_exposure: z.boolean(),
    session_id: z.string().optional(),
    message_id: z.string().max(MAX_IDENTIFIER_LENGTH).optional(),
    page: z.string().optional(),
    component: z.string().optional(),
  })
  .strict()
  .superRefine((request, ctx) => {
    if (
      request.log_exposure &&
      (request.message_id === undefined ||
        request.message_id === '' ||
        request.message_id !== request.message_id.trim())
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['message_id'],
        message: 'log_exposure requires a stable nonblank message_id',
      })
    }
  })

export const gateEvaluationReasonSchema = z.enum([
  'not_found',
  'invalid_config',
  'disabled',
  'error',
  'rule_match',
  'rule_rollout',
  'fallthrough',
  'fallthrough_rollout',
])

export const gateEvaluateResponseSchema = z
  .object({
    key: z.string(),
    variant: z.string().nullable(),
    reason: gateEvaluationReasonSchema,
    rule_id: z.string().nullable(),
    rollout_bucket: z.number().nullable(),
    variant_bucket: z.number().nullable(),
    rollout_percentage: z.number().nullable(),
    bucket_by: z.string().nullable(),
    config_version: z.number().int().nullable(),
    source: z.enum(['memory', 'initial_fetch', 'sse', 'local_storage', 'server']).nullable(),
  })
  .strict()

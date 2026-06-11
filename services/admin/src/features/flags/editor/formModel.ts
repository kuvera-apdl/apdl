// Editor form model + conversions to the canonical wire payloads. Two wire
// subtleties live here and nowhere else:
//   1. exists/not_exists conditions must OMIT the `value` key entirely — the
//      server rejects an explicit null (Pydantic counts it in model_fields_set).
//   2. PUT sends only changed fields (FlagUpdate is fully optional) plus the
//      optimistic-lock version; `enabled` is never sent — the server derives
//      it from `state`.
import { z } from 'zod'

import {
  conditionOperatorSchema,
  evaluationModeSchema,
  guardrailMetricSchema,
  guardrailThresholdSchema,
  writableFlagStateSchema,
} from '@/api/schemas/flags'
import type {
  ConditionOperator,
  FlagConfig,
  FlagCreate,
  FlagUpdate,
  GateCondition,
  GateRule,
  GuardrailMetric,
  GuardrailThreshold,
} from '@/api/types/flags'
import type { EvaluableFlag } from '@/core/evaluator/evaluate'

export const EXISTENCE_OPERATORS: ReadonlySet<ConditionOperator> = new Set(['exists', 'not_exists'])
export const LIST_OPERATORS: ReadonlySet<ConditionOperator> = new Set(['in', 'not_in'])
export const NUMERIC_OPERATORS: ReadonlySet<ConditionOperator> = new Set(['gt', 'gte', 'lt', 'lte'])

// The enforced metric↔threshold pairing (schemas.py GuardrailConfig).
export const GUARDRAIL_PAIRING: Record<GuardrailMetric, GuardrailThreshold> = {
  frontend_error_rate: '2x_baseline',
  frontend_error_count: 'at_least_one',
}

const conditionFormSchema = z
  .object({
    attribute: z.string().trim().min(1, 'Attribute is required'),
    operator: conditionOperatorSchema,
    /** Raw text for scalar operators (numeric ops are validated as numbers). */
    value: z.string(),
    /** Chip list for in / not_in. */
    values: z.array(z.string()),
  })
  .superRefine((condition, ctx) => {
    if (EXISTENCE_OPERATORS.has(condition.operator)) return
    if (LIST_OPERATORS.has(condition.operator)) {
      if (condition.values.length === 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['values'],
          message: 'Add at least one value',
        })
      }
      return
    }
    if (condition.value.trim() === '') {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ['value'], message: 'Value is required' })
      return
    }
    if (NUMERIC_OPERATORS.has(condition.operator) && !Number.isFinite(Number(condition.value))) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ['value'], message: 'Must be a number' })
    }
    if (condition.operator === 'regex') {
      try {
        new RegExp(condition.value)
      } catch {
        ctx.addIssue({ code: z.ZodIssueCode.custom, path: ['value'], message: 'Invalid regular expression' })
      }
    }
  })

const rolloutFormSchema = z.object({
  percentage: z
    .number({ invalid_type_error: 'Required' })
    .min(0, '0–100')
    .max(100, '0–100'),
  bucket_by: z.string().trim().min(1, 'Required'),
})

const ruleFormSchema = z.object({
  id: z.string().min(1),
  name: z.string(),
  conditions: z.array(conditionFormSchema),
  rollout: rolloutFormSchema,
})

const guardrailFormSchema = z
  .object({
    metric: guardrailMetricSchema,
    threshold: guardrailThresholdSchema,
    scope: z.string(),
    minimum_exposures: z.number({ invalid_type_error: 'Required' }).int('Whole number').min(0, '≥ 0'),
    window_minutes: z.number({ invalid_type_error: 'Required' }).int('Whole number').min(1, '≥ 1'),
  })
  .superRefine((guardrail, ctx) => {
    if (guardrail.threshold !== GUARDRAIL_PAIRING[guardrail.metric]) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['threshold'],
        message: `${guardrail.metric} requires '${GUARDRAIL_PAIRING[guardrail.metric]}'`,
      })
    }
  })

export const flagFormSchema = z
  .object({
    key: z.string().trim().min(1, 'Key is required'),
    name: z.string().trim().min(1, 'Name is required'),
    description: z.string(),
    owners: z.array(z.string().trim().min(1)),
    review_by: z
      .string()
      .regex(/^\d{4}-\d{2}-\d{2}$/, 'YYYY-MM-DD')
      .or(z.literal('')),
    state: writableFlagStateSchema,
    default_variant: z.string().min(1, 'Pick a default variant'),
    variants: z
      .array(
        z.object({
          key: z.string().trim().min(1, 'Variant key required'),
          weight: z.number({ invalid_type_error: 'Required' }).int('Whole number').min(0, '≥ 0'),
        }),
      )
      .min(1, 'At least one variant'),
    rules: z.array(ruleFormSchema),
    fallthrough: z.object({ rollout: rolloutFormSchema }),
    evaluation_mode: evaluationModeSchema,
    auto_disable: z.boolean(),
    guardrails: z.array(guardrailFormSchema),
  })
  .superRefine((flag, ctx) => {
    const keys = new Set<string>()
    let total = 0
    flag.variants.forEach((variant, index) => {
      if (keys.has(variant.key)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['variants', index, 'key'],
          message: 'Duplicate variant key',
        })
      }
      keys.add(variant.key)
      total += variant.weight
    })
    if (flag.variants.length > 0 && total <= 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['variants'],
        message: 'Total weight must be positive',
      })
    }
    if (!keys.has(flag.default_variant)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['default_variant'],
        message: 'Must match a variant key',
      })
    }
  })

export type FlagFormValues = z.infer<typeof flagFormSchema>
export type ConditionFormValues = z.infer<typeof conditionFormSchema>

export function newRuleId(): string {
  return `rule_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`
}

/** Create-mode defaults — mirrors the server's two-variant 1:1 template. */
export function emptyFormValues(): FlagFormValues {
  return {
    key: '',
    name: '',
    description: '',
    owners: [],
    review_by: '',
    state: 'draft',
    default_variant: 'control',
    variants: [
      { key: 'control', weight: 1 },
      { key: 'treatment', weight: 1 },
    ],
    rules: [],
    fallthrough: { rollout: { percentage: 0, bucket_by: 'user_id' } },
    evaluation_mode: 'client',
    auto_disable: true,
    guardrails: [],
  }
}

function conditionToForm(condition: GateCondition): ConditionFormValues {
  if (EXISTENCE_OPERATORS.has(condition.operator)) {
    return { attribute: condition.attribute, operator: condition.operator, value: '', values: [] }
  }
  if (LIST_OPERATORS.has(condition.operator)) {
    const values = Array.isArray(condition.value) ? condition.value.map(String) : []
    return { attribute: condition.attribute, operator: condition.operator, value: '', values }
  }
  return {
    attribute: condition.attribute,
    operator: condition.operator,
    value: condition.value === undefined || condition.value === null ? '' : String(condition.value),
    values: [],
  }
}

export function flagToFormValues(flag: FlagConfig): FlagFormValues {
  return {
    key: flag.key,
    name: flag.name,
    description: flag.description,
    owners: [...flag.owners],
    review_by: flag.review_by ?? '',
    // Archived flags cannot be edited; writable states map 1:1.
    state: flag.state === 'archived' ? 'disabled' : flag.state,
    default_variant: flag.default_variant,
    variants: flag.variants.map((variant) => ({ ...variant })),
    rules: flag.rules.map((rule) => ({
      id: rule.id,
      name: rule.name,
      conditions: rule.conditions.map(conditionToForm),
      rollout: { ...rule.rollout },
    })),
    fallthrough: { rollout: { ...flag.fallthrough.rollout } },
    evaluation_mode: flag.evaluation_mode,
    auto_disable: flag.auto_disable,
    guardrails: flag.guardrails.map((guardrail) => ({ ...guardrail })),
  }
}

/** Wire form: existence operators OMIT the value key (JSON.stringify drops undefined). */
export function conditionToWire(condition: ConditionFormValues): GateCondition {
  if (EXISTENCE_OPERATORS.has(condition.operator)) {
    return { attribute: condition.attribute.trim(), operator: condition.operator }
  }
  if (LIST_OPERATORS.has(condition.operator)) {
    return {
      attribute: condition.attribute.trim(),
      operator: condition.operator,
      value: condition.values,
    }
  }
  if (NUMERIC_OPERATORS.has(condition.operator)) {
    return {
      attribute: condition.attribute.trim(),
      operator: condition.operator,
      value: Number(condition.value),
    }
  }
  return {
    attribute: condition.attribute.trim(),
    operator: condition.operator,
    value: condition.value,
  }
}

export function rulesToWire(values: FlagFormValues): GateRule[] {
  return values.rules.map((rule) => ({
    id: rule.id,
    name: rule.name,
    conditions: rule.conditions.map(conditionToWire),
    rollout: { ...rule.rollout },
  }))
}

/**
 * Form values as an evaluable flag for the pre-save population simulator.
 * `enabled` is forced true (the simulator answers "what would this config do
 * once live"); creates use a preview salt — per-user assignments will differ
 * after the server generates the real salt, but distribution shares hold.
 */
export function formToEvaluable(
  values: FlagFormValues,
  options: { salt?: string; version?: number } = {},
): EvaluableFlag {
  return {
    key: values.key.trim() || 'preview-flag',
    enabled: true,
    default_variant: values.default_variant,
    variants: values.variants.map((variant) => ({ key: variant.key.trim(), weight: variant.weight })),
    salt: options.salt ?? 'preview-salt',
    rules: rulesToWire(values),
    fallthrough: { rollout: { ...values.fallthrough.rollout } },
    version: options.version ?? 0,
  }
}

export function formToCreatePayload(values: FlagFormValues): FlagCreate {
  return {
    key: values.key.trim(),
    name: values.name.trim(),
    state: values.state,
    owners: values.owners,
    ...(values.review_by !== '' ? { review_by: values.review_by } : {}),
    enabled: values.state === 'active',
    description: values.description,
    default_variant: values.default_variant,
    variants: values.variants.map((variant) => ({ key: variant.key.trim(), weight: variant.weight })),
    rules: rulesToWire(values),
    fallthrough: { rollout: { ...values.fallthrough.rollout } },
    evaluation_mode: values.evaluation_mode,
    auto_disable: values.auto_disable,
    guardrails: values.guardrails.map((guardrail) => ({ ...guardrail })),
  }
}

const same = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b)

export interface UpdatePlan {
  payload: FlagUpdate
  changedFields: string[]
}

/**
 * Changed-fields-only FlagUpdate against the loaded base flag. `enabled` is
 * derived server-side from `state`; review_by cannot be cleared through the
 * API today (exclude_none strips null) — an emptied field is left unchanged.
 */
export function formToUpdatePlan(values: FlagFormValues, base: FlagConfig, version: number): UpdatePlan {
  const payload: FlagUpdate = { version }
  const changedFields: string[] = []
  const add = <K extends keyof FlagUpdate>(field: K, value: FlagUpdate[K]) => {
    payload[field] = value
    changedFields.push(field as string)
  }

  if (values.name.trim() !== base.name) add('name', values.name.trim())
  if (values.description !== base.description) add('description', values.description)
  if (values.state !== base.state) add('state', values.state)
  if (!same(values.owners, base.owners)) add('owners', values.owners)
  if (values.review_by !== '' && values.review_by !== (base.review_by ?? '')) {
    add('review_by', values.review_by)
  }

  const variants = values.variants.map((variant) => ({ key: variant.key.trim(), weight: variant.weight }))
  const variantsChanged = !same(variants, base.variants)
  const defaultChanged = values.default_variant !== base.default_variant
  if (variantsChanged || defaultChanged) {
    // Send the pair together so the server validates the merged contract.
    add('variants', variants)
    add('default_variant', values.default_variant)
  }

  // Compare both sides through the same lossy projection: the base's
  // serialized conditions carry value:null for existence operators (omitted on
  // the wire) and original scalar types (the editor round-trips via strings).
  const baseRulesWire = base.rules.map((rule) => ({
    id: rule.id,
    name: rule.name,
    conditions: rule.conditions.map((condition) => conditionToWire(conditionToForm(condition))),
    rollout: { ...rule.rollout },
  }))
  const rules = rulesToWire(values)
  if (!same(rules, baseRulesWire)) add('rules', rules)

  const fallthrough = { rollout: { ...values.fallthrough.rollout } }
  if (!same(fallthrough, base.fallthrough)) add('fallthrough', fallthrough)

  if (values.evaluation_mode !== base.evaluation_mode) add('evaluation_mode', values.evaluation_mode)
  if (values.auto_disable !== base.auto_disable) add('auto_disable', values.auto_disable)

  const guardrails = values.guardrails.map((guardrail) => ({ ...guardrail }))
  if (!same(guardrails, base.guardrails)) add('guardrails', guardrails)

  return { payload, changedFields }
}

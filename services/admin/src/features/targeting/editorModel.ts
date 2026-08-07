import { z } from 'zod'

import { conditionOperatorSchema, gateConditionSchema } from '@/api/schemas/flags'
import type { ConditionOperator, GateCondition } from '@/api/types/flags'
import {
  EQUALITY_OPERATORS,
  MAX_IDENTIFIER_LENGTH,
  MAX_MEMBERSHIP_VALUES,
  MAX_STRING_LENGTH,
  MEMBERSHIP_OPERATORS,
  NUMERIC_OPERATORS,
  PRESENCE_OPERATORS,
  STRING_OPERATORS,
  isScalar,
  parseNumeric,
  type JsonScalar,
} from '@/core/evaluator/targetingContract'

export const TARGETING_OPERATOR_GROUPS: ReadonlyArray<{
  label: string
  operators: readonly ConditionOperator[]
}> = [
  { label: 'Existence', operators: ['exists', 'not_exists'] },
  { label: 'Equality', operators: ['equals', 'not_equals'] },
  { label: 'String', operators: ['contains', 'not_contains', 'starts_with', 'ends_with'] },
  { label: 'Numeric', operators: ['gt', 'gte', 'lt', 'lte'] },
  { label: 'Collection', operators: ['in', 'not_in'] },
]

export const COMMON_TARGETING_ATTRIBUTES = [
  'user_id',
  'anonymous_id',
  'plan',
  'country',
  'email',
  'device',
  'beta_opt_in',
] as const

export const TARGETING_ATTRIBUTES_DATALIST_ID = 'apdl-targeting-attributes'

export type ScalarValueType = 'string' | 'number' | 'boolean'

const targetingScalarSchema = z.union([
  z.string().max(MAX_STRING_LENGTH, `At most ${MAX_STRING_LENGTH} characters`),
  z.number().finite('Use a finite number'),
  z.boolean(),
])

/** Editor-only schema shared by flag and experiment condition forms. */
export const targetingConditionFormSchema = z
  .object({
    attribute: z
      .string()
      .trim()
      .min(1, 'Attribute is required')
      .max(MAX_IDENTIFIER_LENGTH, `At most ${MAX_IDENTIFIER_LENGTH} characters`),
    operator: conditionOperatorSchema,
    valueType: z.enum(['string', 'number', 'boolean']),
    value: targetingScalarSchema,
    values: z
      .array(targetingScalarSchema)
      .max(MAX_MEMBERSHIP_VALUES, `At most ${MAX_MEMBERSHIP_VALUES} membership values`),
  })
  .superRefine((condition, ctx) => {
    if (PRESENCE_OPERATORS.has(condition.operator)) return

    if (MEMBERSHIP_OPERATORS.has(condition.operator)) {
      if (condition.values.length === 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['values'],
          message: `Add 1–${MAX_MEMBERSHIP_VALUES} scalar values`,
        })
      }
      return
    }

    if (
      NUMERIC_OPERATORS.has(condition.operator) ||
      (EQUALITY_OPERATORS.has(condition.operator) && condition.valueType === 'number')
    ) {
      if (parseNumeric(condition.value) === null) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['value'],
          message: 'Use a finite number or canonical decimal',
        })
      }
      return
    }

    if (EQUALITY_OPERATORS.has(condition.operator) && condition.valueType === 'boolean') {
      if (typeof condition.value !== 'boolean') {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['value'],
          message: 'Expected a boolean value',
        })
      }
      return
    }

    if (
      (STRING_OPERATORS.has(condition.operator) ||
        (EQUALITY_OPERATORS.has(condition.operator) && condition.valueType === 'string')) &&
      typeof condition.value !== 'string'
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['value'],
        message: 'Expected a string value',
      })
    }
  })

/**
 * Editor-only condition shape. `valueType` selects the JSON scalar written to
 * the canonical GateCondition `value`; it is never part of the wire contract.
 */
export type TargetingConditionFormValue = z.input<typeof targetingConditionFormSchema>

function compactUuid(): string {
  return crypto.randomUUID().replace(/-/g, '').slice(0, 12)
}

export function newTargetingRuleId(): string {
  return `rule_${compactUuid()}`
}

export function newTargetingConditionUiId(): string {
  return `condition_${compactUuid()}`
}

export function scalarValueType(value: JsonScalar): ScalarValueType {
  if (typeof value === 'number') return 'number'
  if (typeof value === 'boolean') return 'boolean'
  return 'string'
}

export function targetingConditionToFormValue(
  condition: GateCondition,
): TargetingConditionFormValue {
  if (PRESENCE_OPERATORS.has(condition.operator)) {
    return {
      attribute: condition.attribute,
      operator: condition.operator,
      valueType: 'string',
      value: '',
      values: [],
    }
  }
  if (MEMBERSHIP_OPERATORS.has(condition.operator)) {
    return {
      attribute: condition.attribute,
      operator: condition.operator,
      valueType:
        Array.isArray(condition.value) && isScalar(condition.value[0])
          ? scalarValueType(condition.value[0])
          : 'string',
      value: '',
      values: Array.isArray(condition.value) ? condition.value.filter(isScalar) : [],
    }
  }
  const value = isScalar(condition.value) ? condition.value : ''
  return {
    attribute: condition.attribute,
    operator: condition.operator,
    valueType: NUMERIC_OPERATORS.has(condition.operator) ? 'number' : scalarValueType(value),
    value,
    values: [],
  }
}

function projectValidatedTargetingCondition(
  condition: z.output<typeof targetingConditionFormSchema>,
): GateCondition {
  const base = {
    attribute: condition.attribute,
    operator: condition.operator,
  }
  if (PRESENCE_OPERATORS.has(condition.operator)) return base
  if (MEMBERSHIP_OPERATORS.has(condition.operator)) {
    return { ...base, value: [...condition.values] }
  }
  if (NUMERIC_OPERATORS.has(condition.operator) || condition.valueType === 'number') {
    const numeric = parseNumeric(condition.value)
    if (numeric === null) {
      throw new Error('Validated numeric targeting value could not be projected')
    }
    return { ...base, value: numeric }
  }
  if (condition.valueType === 'boolean') {
    return { ...base, value: condition.value as boolean }
  }
  return { ...base, value: condition.value as string }
}

/** Validate and project editor state to the one strict GateCondition wire shape. */
export const targetingConditionWireSchema = targetingConditionFormSchema
  .transform(projectValidatedTargetingCondition)
  .pipe(gateConditionSchema)

export function targetingConditionToWire(
  condition: TargetingConditionFormValue,
): GateCondition {
  return targetingConditionWireSchema.parse(condition)
}

export function emptyTargetingCondition(): TargetingConditionFormValue {
  return {
    attribute: '',
    operator: 'equals',
    valueType: 'string',
    value: '',
    values: [],
  }
}

export function targetingConditionForOperator(
  condition: TargetingConditionFormValue,
  operator: ConditionOperator,
): TargetingConditionFormValue {
  if (PRESENCE_OPERATORS.has(operator)) {
    return {
      ...condition,
      operator,
      valueType: 'string',
      value: '',
      values: [],
    }
  }
  if (MEMBERSHIP_OPERATORS.has(operator)) {
    return { ...condition, operator, value: '', values: condition.values }
  }
  if (NUMERIC_OPERATORS.has(operator)) {
    return {
      ...condition,
      operator,
      valueType: 'number',
      value: parseNumeric(condition.value) === null ? '' : condition.value,
      values: [],
    }
  }
  if (!EQUALITY_OPERATORS.has(operator)) {
    return {
      ...condition,
      operator,
      valueType: 'string',
      value: typeof condition.value === 'string' ? condition.value : '',
      values: [],
    }
  }
  return {
    ...condition,
    operator,
    value:
      MEMBERSHIP_OPERATORS.has(condition.operator) && condition.valueType === 'boolean'
        ? false
        : condition.value,
    values: [],
  }
}
